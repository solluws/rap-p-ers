#!/usr/bin/env python3
"""Run two clients for hours and check that what one sends, the other gets.

The first version of this only *observed* -- cache size, reconnects, RSS. Its
own output showed why that is not enough: ``events: 0`` for the whole run. That
number is unreadable. Nothing was sent, so nothing arriving proves nothing, and
a gateway that had silently stopped delivering would look identical.

With a second account it becomes a verification loop. Each round the peer sends
a DM carrying a nonce and a sequence number, and the primary must receive it
within a deadline. Now every sample answers a real question:

* **Did it arrive?** A miss is a delivery failure, not an absence of traffic.
* **How long did it take?** Latency drift over hours is what short tests cannot
  see -- a gateway degrading from 300 ms to 8 s at 4am still "works".
* **Did it arrive once?** Duplicates after a reconnect are a real failure mode
  for a hub that resyncs by replaying a batch.
* **Did the cache stay bounded, tasks stay flat, RSS stay put?** As before.

Usage::

    set ROOT_TOKEN=...            # the receiver
    set ROOT_TOKEN2=...           # the sender
    python devscripts/soak.py --hours 24

    python devscripts/soak.py --minutes 10 --interval 30    # smoke test

With only ROOT_TOKEN it degrades to observe-only and says so, so a single-token
run still works -- just far less informative.

Writes one JSON object per round to ``soak.jsonl``; a run that dies keeps
everything up to that point. Ctrl-C prints the summary and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rootpy import RootClient  # noqa: E402

#: Probe messages carry this so the receiver can tell them from real traffic.
NONCE_PREFIX = "rootpy-soak-"


# --------------------------------------------------------------------------
# Memory reading. Everything here is built once at import: the previous
# version defined the ctypes Structure *inside* rss_mb(), so a new class was
# created on every sample -- 300 per stress run, on Windows only. The type
# histogram pointed straight at it (`getset_descriptor +1268`, which is class
# creation debris), and rss_mb is the only place in the project that makes a
# class at runtime.
# --------------------------------------------------------------------------
_WIN_MEMORY_READER = None


def _build_windows_reader():
    """Resolve the Win32 memory call once; returns a zero-argument callable."""
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    handle = kernel32.GetCurrentProcess()

    # K32GetProcessMemoryInfo lives in kernel32 on Windows 7+; psapi.dll has
    # the original. Resolve whichever exists, once.
    candidates = [(kernel32, "K32GetProcessMemoryInfo")]
    try:
        candidates.append(
            (ctypes.WinDLL("psapi", use_last_error=True),
             "GetProcessMemoryInfo")
        )
    except Exception:
        pass

    for library, name in candidates:
        function = getattr(library, name, None)
        if function is None:
            continue
        function.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        function.restype = wintypes.BOOL
        size = ctypes.sizeof(ProcessMemoryCounters)

        def read(_function=function, _handle=handle, _size=size):
            counters = ProcessMemoryCounters()
            counters.cb = _size
            if _function(_handle, ctypes.byref(counters), _size):
                return counters.WorkingSetSize / 1024 / 1024
            return -1.0

        return read
    return None


if sys.platform == "win32":
    try:
        _WIN_MEMORY_READER = _build_windows_reader()
    except Exception:
        _WIN_MEMORY_READER = None


def rss_mb() -> float:
    """Resident memory in MB; -1.0 when it cannot be read.

    Allocates nothing per call beyond one Structure instance, which matters:
    this is sampled every round, and a sampler that grows the heap makes its
    own reading meaningless.
    """
    if sys.platform == "win32":
        return _WIN_MEMORY_READER() if _WIN_MEMORY_READER else -1.0

    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except Exception:
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024
    except Exception:
        return -1.0


async def calibrate(rounds: int = 200) -> dict:
    """Run the instrumentation against nothing, and report its own drift.

    Every memory finding this tool has produced so far was the tool: a
    listener stacked on each reconnect, an RSS reader that built a class per
    sample, a zero that meant "unmeasured". Each was found only after a live
    run had already been spent on it.

    The mistake underneath was structural -- there was never a control. A
    counter that drifts on its own makes every reading it produces
    uninterpretable, and that cannot be established by looking at the numbers
    it reports about something else.

    So: sample repeatedly with no client activity whatsoever, and measure what
    the sampling alone costs. Runs offline in about a second.
    """
    probe = Soak(_NullClient(), interval=0, path=Path(os.devnull))
    for _ in range(20):            # warm lazy imports before baselining
        probe.observe()
    gc.collect(); gc.collect()
    before_objects = len(gc.get_objects())
    before_rss = rss_mb()

    for _ in range(rounds):
        probe.observe()

    gc.collect(); gc.collect()
    drift = len(gc.get_objects()) - before_objects
    rss_drift = rss_mb() - before_rss
    return {
        "rounds": rounds,
        "objects_per_round": drift / rounds,
        "objects_total": drift,
        "rss_drift_mb": rss_drift,
        "clean": abs(drift) <= max(50, rounds * 0.05),
    }


class _NullClient:
    """Enough surface for observe() to run against no SDK at all."""

    token = "calibration"
    is_connected = True
    _background_tasks = ()

    class _Cache:
        members = {}
        users = ()

    class _Transport:
        class stats:
            @staticmethod
            def totals():
                return {}

    cache = _Cache()
    transport = _Transport()

    def add_listener(self, *args, **kwargs):
        return None

    def remove_listener(self, *args, **kwargs):
        return None


class Soak:
    """One receiver, optionally one sender, and a record of what happened."""

    def __init__(self, receiver, sender=None, *, interval=60.0,
                 path=Path("soak.jsonl"), timeout=45.0):
        self.receiver = receiver
        self.sender = sender
        self.interval = interval
        self.path = Path(path)
        self.timeout = timeout
        self.started = time.monotonic()
        self.samples = []
        self.stopping = False

        self.round = 0
        self.delivered = 0
        self.missed = 0
        self.duplicated = 0
        self.latencies = []
        self.reconnects = 0
        self.forced_reconnects = 0
        self.events = 0
        self.errors = []
        self._seen = {}
        self._handlers = []
        self.calibration = None

    # -- instrumentation ---------------------------------------------------
    def attach(self) -> None:
        """(Re)register the listeners, detaching any previous ones first.

        ``close()`` does not clear listeners, so calling this again after a
        forced reconnect stacked a second copy -- and every message then
        incremented the counter twice. The first stress run read that as "275
        probes arrived more than once: the resync is replaying" and it was
        nothing of the sort, it was this. Duplicates began at round 27, one
        past the first forced reconnect at 25, which is what gave it away.
        """
        self.detach()

        async def on_message(event):
            self.events += 1
            message = getattr(event, "message", None)
            content = getattr(message, "content", "") or ""
            if content.startswith(NONCE_PREFIX):
                self._seen[content] = self._seen.get(content, 0) + 1

        async def on_reconnect(event):
            self.reconnects += 1

        self._handlers = [("message", on_message),
                          ("gateway_reconnecting", on_reconnect)]
        for name, handler in self._handlers:
            self.receiver.add_listener(name, handler)

    def detach(self) -> None:
        for name, handler in getattr(self, "_handlers", ()):
            try:
                self.receiver.remove_listener(name, handler)
            except Exception:
                pass
        self._handlers = []

    async def cycle_gateway(self) -> bool:
        """Close and reopen the socket, so reconnects can be forced.

        Waiting for natural reconnects is the slow part of a soak: the hub
        cycles every few minutes, so seeing the 500th one takes a day. The
        *behaviour* under repeated reconnects is volume-dependent, not
        time-dependent -- handle accumulation, backoff state, listeners not
        being reattached -- so it can be compressed by doing them on purpose.
        """
        try:
            await self.receiver.close()
            await self.receiver.login_token(self.receiver.token)
            await self.receiver.connect()
            self.forced_reconnects += 1
            self.attach()          # listeners do not survive a close
            return True
        except Exception as exc:
            self.errors.append(f"forced reconnect failed: {exc}")
            return False

    async def probe(self, peer_id):
        """Send one nonce and wait for it. Returns what happened."""
        self.round += 1
        nonce = f"{NONCE_PREFIX}{self.round}:{uuid.uuid4().hex[:8]}"
        sent_at = time.monotonic()
        try:
            await self.sender.direct_message(peer_id, nonce)
        except Exception as exc:
            self.errors.append(f"round {self.round}: send failed: {exc}")
            return {"probe": "send_failed", "latency_ms": None}

        deadline = sent_at + self.timeout
        while time.monotonic() < deadline:
            if nonce in self._seen:
                latency = (time.monotonic() - sent_at) * 1000
                self.latencies.append(latency)
                self.delivered += 1
                if self._seen[nonce] > 1:
                    self.duplicated += 1
                return {"probe": "delivered", "latency_ms": round(latency, 1)}
            await asyncio.sleep(0.25)

        self.missed += 1
        self.errors.append(
            f"round {self.round}: {nonce} never arrived within "
            f"{self.timeout:.0f}s"
        )
        return {"probe": "missed", "latency_ms": None}

    def observe(self):
        cache = getattr(self.receiver, "cache", None)
        stats = getattr(getattr(self.receiver, "transport", None), "stats", None)

        communities = members = 0
        if cache is not None:
            try:
                communities = len(cache.members)
                members = sum(len(bucket) for bucket in cache.members.values())
            except Exception as exc:
                self.errors.append(f"cache read failed: {exc}")

        totals = {}
        if stats is not None:
            try:
                totals = stats.totals()
            except Exception as exc:
                self.errors.append(f"stats read failed: {exc}")

        return {
            "t": round(time.monotonic() - self.started, 1),
            "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "connected": bool(getattr(self.receiver, "is_connected", False)),
            "round": self.round,
            "delivered": self.delivered,
            "missed": self.missed,
            "duplicated": self.duplicated,
            "events": self.events,
            "reconnects": self.reconnects,
            "forced_reconnects": self.forced_reconnects,
            "cached_communities": communities,
            "cached_members": members,
            "cached_users": len(getattr(cache, "users", ()) or ()),
            "background_tasks": len(
                getattr(self.receiver, "_background_tasks", ()) or ()
            ),
            "rpc_calls": totals.get("calls", 0),
            "roundtrip_ms": totals.get("roundtrip_ms", 0),
            "retries": totals.get("retries", 0),
            "rate_limited": totals.get("rate_limited", 0),
            "transport_errors": totals.get("errors", 0),
            "rss_mb": round(rss_mb(), 1),
            # RSS alone cannot tell a leak from heap fragmentation: CPython
            # does not reliably return freed arenas to the OS, so RSS can
            # climb while nothing is actually retained. Live object counts
            # can. If these are flat and RSS climbs, it is fragmentation; if
            # both climb, something is being held.
            "gc_objects": len(gc.get_objects()),
            "gc_types": self._type_histogram(),
        }

    @staticmethod
    def _type_histogram(limit: int = 12) -> dict:
        """The most common live object types, by name.

        A count on its own says something is retained but not what, which
        leaves whoever reads it guessing -- and guessing has been the
        expensive part of this whole exercise. Comparing the histogram at the
        start and end names the class that is actually accumulating.
        """
        counts: dict = {}
        for obj in gc.get_objects():
            name = type(obj).__name__
            counts[name] = counts.get(name, 0) + 1
        return dict(
            sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        )

    # -- main loop ---------------------------------------------------------
    async def run(self, deadline, peer_id=None, *, reconnect_every=0,
                  max_rounds=0) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            while not self.stopping and time.monotonic() < deadline:
                if max_rounds and self.round >= max_rounds:
                    break
                if reconnect_every and self.round and \
                        self.round % reconnect_every == 0:
                    await self.cycle_gateway()
                row = self.observe()
                if peer_id and self.sender is not None:
                    row.update(await self.probe(peer_id))
                self.samples.append(row)
                handle.write(json.dumps(row) + "\n")
                handle.flush()
                self._print(row)
                await asyncio.sleep(self.interval)

    def _print(self, row) -> None:
        mark = {
            "delivered": "ok", "missed": "MISS", "send_failed": "SEND-FAIL",
        }.get(row.get("probe", ""), "--")
        latency = row.get("latency_ms")
        print(
            f"  [{row['t']:>7.0f}s] {'up ' if row['connected'] else 'DOWN'} "
            f"{mark:<9} {(f'{latency:.0f}ms' if latency else ''):>8} "
            f"deliv={row['delivered']}/{row['round']} "
            f"cache={row['cached_communities']}c/{row['cached_members']}m "
            f"tasks={row['background_tasks']:<3} rss={row['rss_mb']:.0f}MB"
        )

    # -- verdict -----------------------------------------------------------
    def summarise(self) -> int:
        if len(self.samples) < 2:
            print("\nnot enough samples to say anything.")
            return 0

        first, last = self.samples[0], self.samples[-1]
        hours = max(last["t"] / 3600, 1e-9)
        print("\n" + "=" * 74)
        print(f"SOAK SUMMARY  --  {last['t'] / 60:.0f} minutes, "
              f"{len(self.samples)} rounds")
        print("=" * 74)

        if self.round:
            rate = self.delivered / self.round * 100
            print(f"  delivery       {self.delivered}/{self.round} "
                  f"({rate:.1f}%)  missed={self.missed} "
                  f"duplicated={self.duplicated}")
            if self.latencies:
                ordered = sorted(self.latencies)
                print(f"  latency        median {ordered[len(ordered) // 2]:.0f} ms,"
                      f" worst {ordered[-1]:.0f} ms,"
                      f" first {self.latencies[0]:.0f} ms,"
                      f" last {self.latencies[-1]:.0f} ms")
        else:
            print("  delivery       not measured (observe-only, one token)")

        # Per-hour is the wrong denominator for a stress run: 200 reconnects
        # in 90 seconds is not "8000 per hour", it is 200 reconnects. Report
        # growth against whatever actually drove it.
        rounds = max(self.round, 1)
        stressing = last["t"] < 600 and self.round >= 20

        def line(label, before, after):
            delta = after - before
            if stressing:
                rate = f"{delta / rounds * 1000:+.1f}/1000 rounds"
            else:
                rate = f"{delta / hours:+.1f}/hour"
            print(f"  {label:<15}{before:>10} -> {after:<10} "
                  f"{delta:+.0f}  ({rate})")

        line("communities", first["cached_communities"], last["cached_communities"])
        line("members", first["cached_members"], last["cached_members"])
        line("tasks", first["background_tasks"], last["background_tasks"])
        line("RSS MB", first["rss_mb"], last["rss_mb"])
        line("gc objects", first.get("gc_objects", 0), last.get("gc_objects", 0))
        print(f"  {'reconnects':<15}{last['reconnects']} natural, "
              f"{self.forced_reconnects} forced")

        problems = []
        notes = []
        if self.round and self.missed:
            problems.append(
                f"{self.missed}/{self.round} probes never arrived -- delivery "
                "is dropping messages"
            )
        if self.duplicated:
            problems.append(
                f"{self.duplicated} probes arrived more than once -- the resync "
                "is replaying without deduplicating"
            )
        if len(self.latencies) >= 6:
            half = len(self.latencies) // 2
            early = sum(self.latencies[:half]) / half
            late = sum(self.latencies[half:]) / (len(self.latencies) - half)
            if late > early * 3 and late - early > 1000:
                problems.append(
                    f"delivery latency degraded: {early:.0f} ms early, "
                    f"{late:.0f} ms late"
                )
        cap = getattr(getattr(self.receiver.cache, "members", None), "maxsize", None)
        if cap and last["cached_communities"] > cap:
            problems.append(
                f"cached communities ({last['cached_communities']}) exceeded "
                f"the LRU cap ({cap})"
            )
        task_growth = last["background_tasks"] - first["background_tasks"]
        if task_growth > 50:
            problems.append(f"background tasks grew by {task_growth}")

        # A rate check, because the absolute one is useless for a stress run:
        # 16 MB over 300 rounds looks small and is 52 MB per thousand.
        if self.round >= 50 and last["rss_mb"] > 0 and first["rss_mb"] > 0:
            per_thousand = (
                (last["rss_mb"] - first["rss_mb"]) / self.round * 1000
            )
            if per_thousand > 20:
                problems.append(
                    f"RSS grew {per_thousand:.0f} MB per 1000 rounds"
                    + (
                        f" ({self.forced_reconnects} forced reconnects: "
                        f"~{(last['rss_mb'] - first['rss_mb']) / self.forced_reconnects:.2f} MB each)"
                        if self.forced_reconnects
                        else ""
                    )
                )
        # Name what grew, not just how much.
        before_types = first.get("gc_types") or {}
        after_types = last.get("gc_types") or {}
        grew = sorted(
            (
                (after_types.get(name, 0) - count, name)
                for name, count in before_types.items()
            ),
            reverse=True,
        )
        grew += sorted(
            (
                (count, name)
                for name, count in after_types.items()
                if name not in before_types
            ),
            reverse=True,
        )
        grew = [(delta, name) for delta, name in grew if delta > 0][:5]
        if grew:
            print("  types that grew: "
                  + ", ".join(f"{name} {delta:+}" for delta, name in grew))

        objects = last.get("gc_objects", 0) - first.get("gc_objects", 0)

        # Subtract what the instrument costs on its own. Without this the
        # sampler's own allocations are indistinguishable from the SDK's, and
        # they have been mistaken for them three times.
        calibration = getattr(self, "calibration", None)
        if calibration:
            expected = calibration["objects_per_round"] * max(self.round, 1)
            if expected:
                objects -= expected
                print(f"  instrument drift  {expected:+.0f} objects over "
                      f"{self.round} rounds (subtracted)")
            if not calibration["clean"]:
                problems.append(
                    "the instrument itself drifts "
                    f"({calibration['objects_per_round']:+.2f} objects/round) "
                    "-- retention findings below are not trustworthy until "
                    "that is fixed"
                )
        if self.round >= 50 and objects > 5000:
            leading = f" (mostly {grew[0][1]})" if grew else ""
            problems.append(
                f"{objects} more live objects than at the start{leading} -- a "
                "real retention leak, not heap fragmentation"
            )
        elif self.round >= 50 and objects <= 5000 and last["rss_mb"] > 0 and (
            last["rss_mb"] - first["rss_mb"] > 10
        ):
            notes.append(
                f"RSS rose {last['rss_mb'] - first['rss_mb']:.0f} MB but live "
                f"objects only by {objects} -- looks like heap fragmentation "
                "rather than retention"
            )
        if last["rss_mb"] < 0:
            # A note, not a finding. Losing one diagnostic signal is worth
            # saying out loud, but it is not drift and must not turn every
            # run on that platform into a failure.
            notes.append(
                "RSS could not be measured here, so memory growth is not "
                "being watched"
            )
        elif last["rss_mb"] - first["rss_mb"] > 200:
            problems.append(f"RSS grew {last['rss_mb'] - first['rss_mb']:.0f} MB")
        down = sum(1 for s in self.samples if not s.get("connected", True))
        if down > len(self.samples) * 0.2:
            problems.append(f"disconnected in {down}/{len(self.samples)} samples")
        if last["transport_errors"]:
            problems.append(
                f"{last['transport_errors']} transport error(s) recorded"
            )
        problems.extend(self.errors[:10])

        print()
        if notes:
            for note in notes:
                print(f"  note: {note}")
            print()
        if problems:
            print("  FINDINGS:")
            for problem in problems:
                print(f"    - {problem}")
            return 1
        print("  nothing drifted. Delivery clean, cache bounded, tasks flat.")
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="seconds to wait for each probe (default 45)")
    parser.add_argument("--out", default="soak.jsonl")
    parser.add_argument("--watch", action="append", default=[],
                        metavar="CHANNEL_ID")
    parser.add_argument("--stress", type=int, default=0, metavar="ROUNDS",
                        help="run ROUNDS probes back to back instead of on a "
                             "timer -- compresses the volume-dependent signals "
                             "(leaks, handle growth) into a minute or two")
    parser.add_argument("--selftest", action="store_true",
                        help="calibrate the instrument and exit, without "
                             "touching the network")
    parser.add_argument("--reconnect-every", type=int, default=0,
                        metavar="N",
                        help="force a full close/reconnect every N rounds")
    args = parser.parse_args()

    if args.selftest:
        result = await calibrate(500)
        print(f"instrument over {result['rounds']} idle rounds: "
              f"{result['objects_total']:+} objects "
              f"({result['objects_per_round']:+.3f}/round), "
              f"RSS {result['rss_drift_mb']:+.2f} MB")
        print("clean -- readings from this build are trustworthy"
              if result["clean"] else
              "NOT CLEAN -- fix the sampler before trusting any finding")
        return 0 if result["clean"] else 1

    token = os.environ.get("ROOT_TOKEN")
    token2 = os.environ.get("ROOT_TOKEN2")
    if not token:
        print("set ROOT_TOKEN", file=sys.stderr)
        return 2

    seconds = (args.hours or 0) * 3600 + (args.minutes or 0) * 60 or 3600
    if args.stress:
        # A stress run is bounded by rounds, not the clock; the deadline is
        # only a safety net.
        args.interval = min(args.interval, 0.2)
        seconds = max(seconds, args.stress * 5)

    receiver = RootClient(token=token)
    await receiver.login_token(token)
    await receiver.connect()

    sender = None
    peer_id = None
    if token2:
        sender = RootClient(token=token2)
        await sender.login_token(token2)
        me = await receiver.whoami()
        peer_id = me.id
        who = await sender.whoami()
        print(f"two-account mode: {who.username} -> {me.username}, "
              f"one probe per round")
    else:
        print("ROOT_TOKEN2 not set -- observe-only. Delivery is not checked, "
              "so an events count of 0 will be unreadable.")

    soak = Soak(receiver, sender, interval=args.interval,
                path=Path(args.out), timeout=args.timeout)

    # Calibrate first. A counter that drifts on its own cannot be used to
    # measure anything, and that has to be established before the run rather
    # than argued about afterwards.
    soak.calibration = await calibrate()
    if soak.calibration["clean"]:
        print(f"instrument calibrated: "
              f"{soak.calibration['objects_per_round']:+.3f} objects/round "
              f"over {soak.calibration['rounds']} idle rounds")
    else:
        print(f"WARNING: the instrument drifts "
              f"{soak.calibration['objects_per_round']:+.2f} objects/round on "
              f"its own. Retention findings will not be trustworthy.")

    soak.attach()
    for channel_id in args.watch:
        receiver.watch_channel(channel_id, interval=30.0)

    loop = asyncio.get_running_loop()

    def stop():
        soak.stopping = True

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), stop)
            except NotImplementedError:
                pass  # Windows has no signal handlers on the loop

    print(f"soaking for {seconds / 3600:.2f}h, every {args.interval:.0f}s "
          f"-> {args.out}")
    try:
        await soak.run(
            time.monotonic() + seconds,
            peer_id,
            reconnect_every=args.reconnect_every,
            max_rounds=args.stress,
        )
    except KeyboardInterrupt:
        soak.stopping = True
    finally:
        receiver.unwatch_all()
        await receiver.close()
        if sender is not None:
            await sender.close()

    return soak.summarise()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
