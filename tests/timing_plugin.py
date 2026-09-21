"""Per-test timing, and where the time actually goes.

``--durations`` reports seconds and only the slowest few, which answers "which
test is slow" but not "slow doing what". Where a test does touch the network,
the useful question is how much of it is round-trip time, how much is waiting
on rate limits, and how much is client-side work -- and the SDK already tracks
exactly that in :class:`rootpy.stats.TransportStats`.

This records setup/call/teardown per test in milliseconds, reads the transport
counters around each one, and prints a summary. Fixture cost shows up in the
setup column of whichever test triggered it, which is worth knowing before
optimising the wrong thing.

Enabled with ``--timing``; writes ``test-timings.json`` with ``--timing-json``
so runs can be compared.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

import pytest

_RESULTS: list[dict] = []
_PHASE_START: dict[str, float] = {}


def pytest_addoption(parser):
    group = parser.getgroup("timing")
    group.addoption(
        "--timing",
        action="store_true",
        default=False,
        help="print a per-test timing breakdown in milliseconds",
    )
    group.addoption(
        "--timing-json",
        action="store",
        default=None,
        metavar="PATH",
        help="also write the timing breakdown to PATH as JSON",
    )


def _transport_totals(item):
    """Transport counters for whichever client fixture this test used.

    Reading them off the client itself rather than a global keeps the numbers
    honest when several clients are in play; each has its own transport.
    """
    totals = {"calls": 0, "roundtrip_ms": 0.0, "waiting_ms": 0.0,
              "overhead_ms": 0.0, "retries": 0, "rate_limited": 0}
    seen = set()
    for name in ("client", "gateway_client"):
        obj = item.funcargs.get(name) if hasattr(item, "funcargs") else None
        transport = getattr(obj, "transport", None)
        stats = getattr(transport, "stats", None)
        if stats is None or id(stats) in seen:
            continue
        seen.add(id(stats))
        try:
            snapshot = stats.totals()
        except Exception:
            continue
        for key in totals:
            totals[key] += snapshot.get(key, 0) or 0
    return totals


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    key = item.nodeid

    entry = next((r for r in _RESULTS if r["nodeid"] == key), None)
    if entry is None:
        entry = {
            "nodeid": key,
            "file": key.split("::")[0],
            "cls": key.split("::")[1] if key.count("::") > 1 else "",
            "name": key.rsplit("::", 1)[-1],
            "setup_ms": 0.0,
            "call_ms": 0.0,
            "teardown_ms": 0.0,
            "outcome": "",
            "rpc_calls": 0,
            "roundtrip_ms": 0.0,
            "waiting_ms": 0.0,
        }
        _RESULTS.append(entry)

    entry[f"{report.when}_ms"] = round(report.duration * 1000, 1)
    if report.when == "call":
        entry["outcome"] = report.outcome
        after = _transport_totals(item)
        before = _PHASE_START.get(key + ":rpc") or {}
        if before:
            entry["rpc_calls"] = after["calls"] - before.get("calls", 0)
            entry["roundtrip_ms"] = round(
                after["roundtrip_ms"] - before.get("roundtrip_ms", 0.0), 1
            )
            entry["waiting_ms"] = round(
                after["waiting_ms"] - before.get("waiting_ms", 0.0), 1
            )
    elif report.when == "setup" and report.outcome == "passed":
        _PHASE_START[key + ":rpc"] = _transport_totals(item)


def _bar(value, peak, width=18):
    if peak <= 0:
        return ""
    return "#" * max(1, int(round(value / peak * width))) if value > 0 else ""


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not config.getoption("--timing") or not _RESULTS:
        return

    write = terminalreporter.write_line
    rows = [r for r in _RESULTS if r["outcome"]]
    for row in rows:
        row["total_ms"] = round(
            row["setup_ms"] + row["call_ms"] + row["teardown_ms"], 1
        )

    write("")
    write("=" * 78)
    write("TIMING  (milliseconds)")
    write("=" * 78)

    slowest = sorted(rows, key=lambda r: -r["total_ms"])[:25]
    peak = slowest[0]["total_ms"] if slowest else 0
    write(f"{'test':<46}{'total':>8}{'setup':>8}{'call':>8}{'rpc':>5}")
    write("-" * 78)
    for row in slowest:
        label = f"{row['cls']}::{row['name']}" if row["cls"] else row["name"]
        write(
            f"{label[:45]:<46}{row['total_ms']:>8.0f}{row['setup_ms']:>8.0f}"
            f"{row['call_ms']:>8.0f}{row['rpc_calls'] or '':>5}"
        )

    # per file, so it is obvious which suite dominates
    by_file = defaultdict(lambda: {"total": 0.0, "n": 0, "rpc": 0})
    for row in rows:
        bucket = by_file[row["file"].rsplit("/", 1)[-1]]
        bucket["total"] += row["total_ms"]
        bucket["n"] += 1
        bucket["rpc"] += row["rpc_calls"]
    write("")
    write(f"{'file':<46}{'total':>8}{'tests':>8}{'avg':>8}{'rpc':>5}")
    write("-" * 78)
    top = max((b["total"] for b in by_file.values()), default=0)
    for name, bucket in sorted(by_file.items(), key=lambda kv: -kv[1]["total"]):
        avg = bucket["total"] / bucket["n"] if bucket["n"] else 0
        write(
            f"{name[:45]:<46}{bucket['total']:>8.0f}{bucket['n']:>8}"
            f"{avg:>8.0f}{bucket['rpc'] or '':>5}  {_bar(bucket['total'], top)}"
        )

    grand = sum(r["total_ms"] for r in rows)
    setup = sum(r["setup_ms"] for r in rows)
    roundtrip = sum(r["roundtrip_ms"] for r in rows)
    waiting = sum(r["waiting_ms"] for r in rows)
    rpc = sum(r["rpc_calls"] for r in rows)
    write("")
    write(f"  {len(rows)} tests, {grand / 1000:.1f}s total")
    write(f"  fixture setup:  {setup:>9.0f} ms  ({setup / grand * 100:.0f}%)"
          if grand else "")
    if rpc:
        write(f"  {rpc} RPC calls: {roundtrip:>9.0f} ms round trip"
              f"  ({roundtrip / grand * 100:.0f}% of wall time)")
        write(f"  rate-limit waiting: {waiting:>4.0f} ms")
        unexplained = grand - roundtrip - setup
        write(
            f"  not RPC or setup: {unexplained:>7.0f} ms "
            f"({unexplained / grand * 100:.0f}%) -- sleeps and client work"
        )

    path = config.getoption("--timing-json")
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(sorted(rows, key=lambda r: -r["total_ms"]), handle, indent=2)
        write(f"  written to {path}")
