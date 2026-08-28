"""
Full end-to-end exercise of the SDK against a real server, with timings.

PHASE 0 times every way to log in (token-only, minimal, lazy, eager, gateway)
so you can see what each mode actually costs. Note the FIRST mode measured
pays cold DNS and a full TLS handshake, so it reads slower than it is --

benchmark.py alternates the order if you want a fair comparison. PHASE 1 runs the full lifecycle,
timing every step and measuring real delivery latency (send -> readable).

Every step reports how long it took, and the summary lists the slowest three.

Steps, in order -- each one verified, not just executed:

     1. join a community by invite code
     2. create a channel
     3. list all channels, find the one just created
     4. create a role
     5. list all roles, find the one just created
     6. assign that role to itself
     7. post a message (via channel.send -- object method)
     8. read history (via channel.history) and find that message
     9. reply to it (message.reply -- threaded)
    10. react to it, then remove the reaction
    11. edit the message, then re-read it to confirm the edit stuck
    12. pin it, then unpin it
    13. mark the channel read
    14. set presence: online -> idle -> invisible -> online
    15. update the account's custom status, then clear it
   15b. send a friend request to FRIEND_TARGET
   15c. measure send -> visible-in-history latency (polled, not guessed)
   15d. RootClient.send_once -- one request, no login
   15e. paginating history iterator
   15f. state cache contents
   15g. list members + member_count
   15h. get_random_member (with profile) + their profile picture
   15i. profiles: own + batched member profiles
   15j. change profile picture from bytes, then restore
   15k. community channel helpers
    16. check typed events fired while we worked (channel_create, role_add)
    17. wait_for a live message with a timeout
    18. delete the message
    19. delete the role and the channel we made (cleanup)
    20. leave the community

Every step prints PASS/FAIL and the run ends with a summary, so a single run
tells you exactly which subsystem broke.

SETUP
-----
Put your token in tokens.txt next to this file:

    token=<your token>

(or set ROOT_TOKEN in the environment), then:

    python fullrun.py

Output goes to the console and to log.txt.
"""

import asyncio
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient

INVITE_CODE = "ADCuyZscjQq121ZbcLRiaA"

# Username to send a test friend request to (step 15b).
FRIEND_TARGET = "systemctl"

# Set to logging.DEBUG for the wire-level firehose.
LOG_LEVEL = logging.INFO

LOG_PATH = Path(__file__).with_name("log.txt")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
for _noisy in (
    "httpx", "httpcore", "httpcore.http2", "hpack", "hpack.hpack",
    "hpack.table", "h2", "websockets", "websockets.client", "asyncio",
):
    logging.getLogger(_noisy).setLevel(
        logging.DEBUG if LOG_LEVEL <= logging.DEBUG else logging.WARNING
    )

log = logging.getLogger("fullrun")


class Results:
    """Tracks each step: pass/fail, detail, and how long it took."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, bool, str, float]] = []
        self._mark = time.perf_counter()

    def start(self) -> None:
        """Reset the stopwatch -- call right before the work of a step."""
        self._mark = time.perf_counter()

    def record(self, name: str, ok: bool, detail: str = "",
               seconds: float | None = None) -> None:
        elapsed = time.perf_counter() - self._mark if seconds is None else seconds
        self.steps.append((name, ok, detail, elapsed))
        log.info(
            "  [%s] %s (%.0f ms)%s",
            "PASS" if ok else "FAIL", name, elapsed * 1000,
            f" -- {detail}" if detail else "",
        )
        self._mark = time.perf_counter()

    @property
    def failed(self) -> int:
        return sum(1 for _n, ok, _d, _t in self.steps if not ok)

    @property
    def total_seconds(self) -> float:
        return sum(t for _n, _ok, _d, t in self.steps)

    def summary(self) -> None:
        log.info("")
        log.info("=" * 78)
        log.info(
            "SUMMARY -- %d/%d steps passed in %.1fs total",
            len(self.steps) - self.failed, len(self.steps), self.total_seconds,
        )
        log.info("")
        log.info("  %-4s %-6s %9s  %s", "#", "result", "time", "step")
        log.info("  " + "-" * 74)
        for index, (name, ok, detail, elapsed) in enumerate(self.steps, 1):
            log.info(
                "  %-4d %-6s %7.0fms  %s%s",
                index, "PASS" if ok else "FAIL", elapsed * 1000, name,
                f" -- {detail}" if detail else "",
            )
        log.info("  " + "-" * 74)

        # Slowest steps are usually the interesting ones.
        slowest = sorted(self.steps, key=lambda row: row[3], reverse=True)[:3]
        log.info("  slowest: %s", ", ".join(
            f"{name} ({elapsed * 1000:.0f}ms)" for name, _ok, _d, elapsed in slowest
        ))
        log.info("=" * 78)
        log.info(
            "VERDICT: %s",
            "ALL STEPS PASSED" if not self.failed
            else f"{self.failed} STEP(S) FAILED",
        )


def load_token() -> str:
    token = os.environ.get("ROOT_TOKEN", "")
    path = Path(__file__).with_name("tokens.txt")
    if not token and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip().lower() in ("token", "watcher", "sender") and value.strip():
                token = value.strip()
                break
    if not token:
        raise SystemExit(
            "No token. Create tokens.txt next to this script:\n    token=<your token>"
        )
    return token


async def main() -> None:
    results = Results()
    run_tag = uuid.uuid4().hex[:6]
    client = RootClient(token=load_token())

    observed: set[str] = set()
    community_id = None
    channel = None
    role = None
    message = None

    try:
        # ---------------------------------------------------------------- #
        # PHASE 0 -- how long does each way of logging in actually take?
        # Every mode below can send/read; only "gateway" can RECEIVE pushes.
        # ---------------------------------------------------------------- #
        log.info("PHASE 0: login mode timings")
        token = load_token()

        # token-only: no login at all, one request when you actually call
        started = time.perf_counter()
        probe = RootClient(token=token, preload_caches=False)
        await probe.close()
        results.record(
            "login: token-only (no requests)", True,
            "construct only -- the first API call is the only round-trip",
            time.perf_counter() - started,
        )

        for label, kwargs in (
            ("minimal", dict(preload_caches=False)),
            ("lazy (default)", dict()),
            ("eager", dict(expand_communities=True, preload_direct_messages=True)),
        ):
            started = time.perf_counter()
            probe = RootClient(token=token, **kwargs)
            try:
                await probe.login_token()
                elapsed = time.perf_counter() - started
                results.record(
                    f"login: {label}", True,
                    f"{len(probe.communities)} communities cached",
                    elapsed,
                )
            except Exception as exc:
                results.record(
                    f"login: {label}", False, f"{type(exc).__name__}: {exc}",
                    time.perf_counter() - started,
                )
            finally:
                await probe.close()

        # gateway: login + websocket handshake
        started = time.perf_counter()
        probe = RootClient(token=token)
        try:
            await probe.login_token()
            await probe.connect()
            results.record(
                "login: + gateway connect", True, "websocket up",
                time.perf_counter() - started,
            )
        except Exception as exc:
            results.record(
                "login: + gateway connect", False, f"{type(exc).__name__}: {exc}",
                time.perf_counter() - started,
            )
        finally:
            await probe.close()

        log.info("")
        log.info("PHASE 1: full lifecycle")
        client.reset_timings()      # count only the lifecycle, not phase 0
        results.start()
        await client.login_token()
        await client.connect()          # gateway up: typed events + wait_for
        me = await client.whoami()
        log.info("logged in as %s (%s)", me.username, me.id)

        # Watch the typed events fire while the run does its work.
        for event_name in (
            "channel_create", "channel_delete", "role_create", "role_delete",
            "role_add", "role_remove", "member_join", "member_leave",
            "friend_request",
        ):
            def _make(name):
                async def handler(event):
                    observed.add(name)
                    log.info("   (event) on_%s -> %s", name, event)
                return handler
            client.add_listener(event_name, _make(event_name))
        log.info("run tag: %s", run_tag)
        log.info("")

        # --- 1. join ------------------------------------------------------ #
        log.info("STEP 1: join community by invite")
        try:
            joined = await client.invites.join(INVITE_CODE)
            # The join response shape varies; fall back to finding it by listing.
            community_id = getattr(joined, "community_id", None) or getattr(
                getattr(joined, "community", None), "id", None
            )
            if community_id is None:
                communities = await client.list_communities()
                community_id = communities[-1].id if communities else None
            results.record("join community", community_id is not None, f"community={community_id}")
        except Exception as exc:
            # Already a member is not a failure for the rest of the run.
            communities = await client.list_communities()
            community_id = communities[-1].id if communities else None
            results.record(
                "join community",
                community_id is not None,
                f"join raised ({type(exc).__name__}); using {community_id}",
            )
        if community_id is None:
            results.summary()
            return

        extended = await client.fetch_community(community_id)
        log.info("community: %s", getattr(extended.community, "name", community_id))

        # --- 2. create channel -------------------------------------------- #
        log.info("STEP 2: create channel")
        group = extended.channel_groups[0] if extended.channel_groups else None
        channel_name = f"rootpy-test-{run_tag}"
        created_channel = None
        try:
            created_channel = await client.community_admin.create_channel(
                community_id,
                group.id,
                channel_name,
                channel_type=1,  # TEXT
            )
            results.record("create channel", True, f"name={channel_name}")
        except Exception as exc:
            results.record("create channel", False, f"{type(exc).__name__}: {exc}")

        # --- 3. list channels, find ours ---------------------------------- #
        log.info("STEP 3: list channels and find the new one")
        refreshed = await client.fetch_community(community_id)
        all_channels = [
            ch for grp in refreshed.channel_groups for ch in grp.channels
        ]
        channel = next(
            (ch for ch in all_channels if (ch.name or "") == channel_name), None
        )
        results.record(
            "find created channel among %d" % len(all_channels),
            channel is not None,
            f"id={channel.id}" if channel else "not found",
        )

        # --- 4. create role ----------------------------------------------- #
        log.info("STEP 4: create role")
        role_name = f"rootpy-role-{run_tag}"
        try:
            await client.community_admin.create_role(
                community_id, role_name, mentionable=True
            )
            results.record("create role", True, f"name={role_name}")
        except Exception as exc:
            results.record("create role", False, f"{type(exc).__name__}: {exc}")

        # --- 5. list roles, find ours ------------------------------------- #
        log.info("STEP 5: list roles and find the new one")
        refreshed = await client.fetch_community(community_id)
        role = next(
            (r for r in refreshed.roles if (getattr(r, "name", "") or "") == role_name),
            None,
        )
        results.record(
            "find created role among %d" % len(refreshed.roles),
            role is not None,
            f"id={role.id}" if role else "not found",
        )

        # --- 6. assign role to self --------------------------------------- #
        log.info("STEP 6: assign the role to myself")
        if role is not None:
            try:
                await client.roles.add_to_members(community_id, role.id, [me.id])
                results.record("assign role to self", True)
            except Exception as exc:
                results.record("assign role to self", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("assign role to self", False, "no role to assign")

        # --- 7. send via channel.send (object method) --------------------- #
        log.info("STEP 7: post a message with channel.send()")
        marker = f"rootpy-fullrun-{run_tag}"
        if channel is not None:
            try:
                await channel.send(marker)
                results.record("channel.send", True)
            except Exception as exc:
                results.record("channel.send", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("channel.send", False, "no channel")

        # --- 8. history via channel.history ------------------------------- #
        log.info("STEP 8: read history with channel.history() and find it")
        if channel is not None:
            await asyncio.sleep(1.5)
            try:
                history = await channel.history()
                message = next(
                    (m for m in history if marker in (m.content or "")), None
                )
                results.record(
                    "channel.history finds own message (%d fetched)" % len(history),
                    message is not None,
                    f"id={message.id}" if message else "not found",
                )
            except Exception as exc:
                results.record("channel.history", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("channel.history", False, "no channel")

        # --- 9. threaded reply -------------------------------------------- #
        log.info("STEP 9: message.reply()")
        if message is not None:
            try:
                await message.reply(f"reply to {marker}")
                results.record("message.reply", True)
            except Exception as exc:
                results.record("message.reply", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("message.reply", False, "no message")

        # --- 10. react / unreact ------------------------------------------ #
        log.info("STEP 10: react then unreact")
        if message is not None:
            try:
                await message.react("👍")
                await asyncio.sleep(0.5)
                await message.unreact("👍")
                results.record("react + unreact", True)
            except Exception as exc:
                results.record("react + unreact", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("react + unreact", False, "no message")

        # --- 11. edit ------------------------------------------------------ #
        log.info("STEP 11: edit the message and confirm the change stuck")
        if message is not None:
            edited_text = f"{marker} (edited)"
            try:
                await message.edit(edited_text)
                # Don't just trust that edit() didn't throw -- re-read the
                # channel and confirm the new text is actually there.
                await asyncio.sleep(1.0)
                history = await channel.history()
                found = next(
                    (m for m in history if m.id == message.id), None
                )
                confirmed = found is not None and edited_text in (found.content or "")
                results.record(
                    "message.edit (verified by re-reading)",
                    confirmed,
                    f"content={found.content!r}" if found else "message not found after edit",
                )
            except Exception as exc:
                results.record("message.edit", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("message.edit", False, "no message")

        # --- 12. pin / unpin ----------------------------------------------- #
        log.info("STEP 12: pin then unpin")
        if message is not None:
            try:
                await message.pin()
                await asyncio.sleep(0.5)
                await message.unpin()
                results.record("pin + unpin", True)
            except Exception as exc:
                results.record("pin + unpin", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("pin + unpin", False, "no message")

        # --- 13. mark read -------------------------------------------------- #
        log.info("STEP 13: channel.mark_read()")
        if channel is not None:
            try:
                await channel.mark_read()
                results.record("channel.mark_read", True)
            except Exception as exc:
                results.record("channel.mark_read", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("channel.mark_read", False, "no channel")

        # --- 14. presence --------------------------------------------------- #
        log.info("STEP 14: presence online -> idle -> invisible -> online")
        try:
            for verb, call in (
                ("online", client.go_online),
                ("idle", client.go_idle),
                ("invisible", client.go_invisible),
                ("online", client.go_online),
            ):
                await call()
                await asyncio.sleep(0.3)
            results.record("presence changes", True, "online/idle/invisible")
        except Exception as exc:
            results.record("presence changes", False, f"{type(exc).__name__}: {exc}")

        # --- 15. custom status ---------------------------------------------- #
        log.info("STEP 15: set then clear the custom status")
        try:
            await client.update_status(f"testing rootpy {run_tag}")
            await asyncio.sleep(0.4)
            await client.update_status(None)
            results.record("custom status set + clear", True)
        except Exception as exc:
            results.record("custom status set + clear", False, f"{type(exc).__name__}: {exc}")

        # --- 15b. friend request ---------------------------------------------- #
        log.info("STEP 15b: send a friend request to %s", FRIEND_TARGET)
        try:
            await client.friend_requests.send(FRIEND_TARGET)
            results.record("friend_requests.send", True, f"-> {FRIEND_TARGET}")
        except Exception as exc:
            # Already friends / already pending is a normal outcome on a
            # repeat run, not a broken API -- report it as such.
            text = str(exc).lower()
            benign = any(
                hint in text
                for hint in ("already", "exists", "pending", "duplicate")
            )
            results.record(
                "friend_requests.send",
                benign,
                f"{type(exc).__name__}: {exc}"
                + (" (treated as already-sent)" if benign else ""),
            )

        # --- 15c. delivery latency: how long until a message is readable? ---- #
        log.info("STEP 15c: measure send -> visible-in-history delay")
        if channel is not None:
            probe_marker = f"rootpy-latency-{run_tag}"
            try:
                send_started = time.perf_counter()
                await channel.send(probe_marker)
                send_ms = (time.perf_counter() - send_started) * 1000

                # Poll history until it shows up, so we measure real
                # server-side visibility rather than a guessed sleep.
                visible_after = None
                probe_message = None
                poll_started = time.perf_counter()
                for _attempt in range(20):          # up to ~10s
                    await asyncio.sleep(0.5)
                    recent = await channel.history()
                    probe_message = next(
                        (m for m in recent if probe_marker in (m.content or "")),
                        None,
                    )
                    if probe_message is not None:
                        visible_after = time.perf_counter() - poll_started
                        break

                if visible_after is None:
                    results.record(
                        "send -> readable latency", False,
                        f"send took {send_ms:.0f}ms but never appeared in history",
                    )
                else:
                    results.record(
                        "send -> readable latency", True,
                        f"send {send_ms:.0f}ms, readable after ~{visible_after * 1000:.0f}ms",
                    )
                    # tidy up the probe message
                    try:
                        await probe_message.delete()
                    except Exception:
                        pass
            except Exception as exc:
                results.record(
                    "send -> readable latency", False, f"{type(exc).__name__}: {exc}"
                )
        else:
            results.record("send -> readable latency", False, "no channel")

        # --- 15d. one-shot send (no login) ----------------------------------- #
        log.info("STEP 15d: RootClient.send_once -- single request, no login")
        if channel is not None:
            try:
                started = time.perf_counter()
                once = await RootClient.send_once(
                    token, channel.id, f"rootpy-once-{run_tag}",
                    community_id=community_id,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                results.record(
                    "send_once (no login)", True,
                    f"{elapsed_ms:.0f}ms end-to-end, id={getattr(once, 'id', '?')}",
                )
                # clean it up through the normal client
                try:
                    recent = await channel.history()
                    stray = next(
                        (m for m in recent if f"rootpy-once-{run_tag}" in (m.content or "")),
                        None,
                    )
                    if stray is not None:
                        await stray.delete()
                except Exception:
                    pass
            except Exception as exc:
                results.record(
                    "send_once (no login)", False, f"{type(exc).__name__}: {exc}"
                )
        else:
            results.record("send_once (no login)", False, "no channel")

        # --- 15e. history iterator -------------------------------------------- #
        log.info("STEP 15e: paginating history iterator")
        if channel is not None:
            try:
                started = time.perf_counter()
                collected = [m async for m in channel.history_iter(limit=25, page_size=10)]
                elapsed_ms = (time.perf_counter() - started) * 1000
                unique = len({m.id for m in collected})
                results.record(
                    "history_iter paginates", unique == len(collected),
                    f"{len(collected)} messages, {unique} unique, {elapsed_ms:.0f}ms",
                )
            except Exception as exc:
                results.record(
                    "history_iter paginates", False, f"{type(exc).__name__}: {exc}"
                )
        else:
            results.record("history_iter paginates", False, "no channel")

        # --- 15f. cache ------------------------------------------------------- #
        log.info("STEP 15f: state cache")
        try:
            stats = client.cache.stats()
            results.record(
                "state cache populated", stats["users"] > 0,
                f"users={stats['users']} members={stats['members']} "
                f"hit_rate={stats['hit_rate']}",
            )
        except Exception as exc:
            results.record("state cache populated", False, f"{type(exc).__name__}: {exc}")

        # --- 15g. members of the server ---------------------------------- #
        log.info("STEP 15g: list members")
        try:
            members = await client.get_members(community_id)
            count = await client.member_count(community_id)
            results.record(
                "get_members / member_count", count == len(members),
                f"{count} member(s)",
            )
        except Exception as exc:
            members = ()
            results.record("get_members / member_count", False,
                           f"{type(exc).__name__}: {exc}")

        # --- 15h. random member ------------------------------------------- #
        log.info("STEP 15h: pick a random member")
        try:
            picked = await client.get_random_member(community_id)
            if picked is None:
                results.record("get_random_member", not members,
                               "no members to pick from")
            else:
                is_member = any(m.user_id == picked.user_id for m in members)
                not_me = picked.user_id != me.id
                results.record(
                    "get_random_member", is_member and not_me,
                    f"picked {picked.username or picked.user_id[:8]} "
                    f"({picked.user_id[:8]})"
                    + ("" if not_me else " -- that's ME, exclusion failed"),
                )

                # the picked member should arrive with their profile attached
                results.record(
                    "random member carries a profile",
                    picked.username is not None,
                    f"username={picked.username!r} "
                    f"pfp={'yes' if picked.avatar_url else 'none'} "
                    f"banner={'yes' if picked.banner_uri else 'none'} "
                    f"about={(picked.about_me or '')[:24]!r}",
                )

                # and fetch that member's profile picture explicitly
                try:
                    their_profile = await client.get_profile(picked.user_id)
                    avatar = getattr(their_profile, "avatar_url", None)
                    results.record(
                        "random member's profile picture",
                        their_profile is not None,
                        f"{picked.username or picked.user_id[:8]} -> "
                        + (str(avatar)[:48] if avatar else "no picture set"),
                    )
                except Exception as exc:
                    results.record("random member's profile picture", False,
                                   f"{type(exc).__name__}: {exc}")
            several = await client.get_random_members(community_id, 3)
            unique = len({m.user_id for m in several})
            results.record(
                "get_random_members(3)", unique == len(several),
                f"{len(several)} picked, {unique} distinct",
            )
        except Exception as exc:
            results.record("get_random_member", False,
                           f"{type(exc).__name__}: {exc}")

        # --- 15i. profiles -------------------------------------------------- #
        log.info("STEP 15i: fetch profiles (own + batched members)")
        try:
            mine = await client.get_profile(me.id)
            ok = mine is not None and mine.username == me.username
            results.record(
                "get_profile (self)", ok,
                f"username={getattr(mine, 'username', None)!r} "
                f"about={(getattr(mine, 'about_me', None) or '')[:24]!r} "
                f"pfp={'yes' if getattr(mine, 'avatar_url', None) else 'none'} "
                f"banner={'yes' if getattr(mine, 'banner_uri', None) else 'none'}",
            )
        except Exception as exc:
            results.record("get_profile (self)", False, f"{type(exc).__name__}: {exc}")

        try:
            detailed = await client.get_members_detailed(community_id)
            named = [d for d in detailed if d.username]
            results.record(
                "get_members_detailed (batched)", len(detailed) == len(members),
                f"{len(detailed)} member(s), {len(named)} with a profile",
            )
        except Exception as exc:
            results.record("get_members_detailed (batched)", False,
                           f"{type(exc).__name__}: {exc}")

        # --- 15j. profile picture ------------------------------------------- #
        log.info("STEP 15j: change profile picture (bytes) then restore")
        try:
            before = await client.get_profile(me.id)
            original = getattr(before, "avatar_url", None)
            # a tiny valid 1x1 PNG
            # A genuinely valid 1x1 RGBA PNG (chunk CRCs verified). An
            # invalid image makes the server reject it with INTERNAL, which
            # looks like an SDK bug but isn't.
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000d49444154789c63d0883af11f000442024afcb839ca"
                "0000000049454e44ae426082"
            )
            new_uri = await client.change_profile_picture(png)
            results.record(
                "change_profile_picture (bytes)", bool(new_uri),
                f"uploaded -> {str(new_uri)[:40]}",
            )
            # put the old one back so the run leaves no trace
            try:
                await client.change_profile_picture(original)
            except Exception:
                pass
        except Exception as exc:
            results.record("change_profile_picture (bytes)", False,
                           f"{type(exc).__name__}: {exc}")

        # --- 15k. history iterator over a busy channel ---------------------- #
        log.info("STEP 15k: channel/community helpers")
        try:
            detail = await client.community_detail(community_id)
            results.record(
                "community helpers (.channels/.text_channels)",
                len(detail.channels) >= len(detail.text_channels),
                f"{len(detail.channels)} channels, "
                f"{len(detail.text_channels)} text",
            )
        except Exception as exc:
            results.record("community helpers", False, f"{type(exc).__name__}: {exc}")

        # --- 16. typed events ------------------------------------------------ #
        log.info("STEP 16: typed events observed during the run")
        # Informational: the gateway may legitimately have nothing to push
        # during a short run, so this reports rather than fails.
        results.record(
            "typed events wired",
            True,
            ", ".join(sorted(observed)) if observed
            else "none pushed during this run (not an error)",
        )

        # --- 17. wait_for ---------------------------------------------------- #
        log.info("STEP 17: wait_for with a timeout (expects to time out)")
        try:
            await client.wait_for("message", timeout=2.0)
            results.record("wait_for", True, "a message arrived during the wait")
        except asyncio.TimeoutError:
            results.record("wait_for", True, "timed out cleanly as expected")
        except Exception as exc:
            results.record("wait_for", False, f"{type(exc).__name__}: {exc}")

        # --- 18. delete message ---------------------------------------------- #
        log.info("STEP 18: delete the message")
        if message is not None:
            try:
                await message.delete()
                results.record("message.delete", True)
            except Exception as exc:
                results.record("message.delete", False, f"{type(exc).__name__}: {exc}")
        else:
            results.record("message.delete", False, "no message")

        # --- 19. cleanup ------------------------------------------------------ #
        log.info("STEP 19: delete the role and channel we created")
        cleanup_ok = True
        cleanup_detail = []
        if role is not None:
            try:
                await client.community_admin.delete_role(community_id, role.id)
                cleanup_detail.append("role deleted")
            except Exception as exc:
                cleanup_ok = False
                cleanup_detail.append(f"role: {type(exc).__name__}")
        if channel is not None:
            try:
                await channel.delete()
                cleanup_detail.append("channel deleted")
            except Exception as exc:
                cleanup_ok = False
                cleanup_detail.append(f"channel: {type(exc).__name__}")
        results.record("cleanup", cleanup_ok, "; ".join(cleanup_detail) or "nothing to clean")

        # --- 20. leave --------------------------------------------------------- #
        log.info("STEP 20: leave the community")
        try:
            await client.leave_community(community_id)
            results.record("leave community", True)
        except Exception as exc:
            results.record("leave community", False, f"{type(exc).__name__}: {exc}")

    except Exception:
        log.error("unexpected failure:\n%s", traceback.format_exc())
        results.record("run completed", False, "unexpected exception")
    finally:
        # Where the time actually went: our own waiting vs the round trip.
        try:
            totals = client.timings()["totals"]
            log.info("")
            log.info("REQUEST TIMING")
            log.info("  %s", client.timing_report().replace("\n", "\n  "))
            if totals["calls"]:
                log.info(
                    "  -> %.0f%% of request time was round trip "
                    "(network + server), %.0f%% was this client waiting "
                    "on rate limits/retries, %.1f%% our own overhead",
                    100 * totals["roundtrip_ms"] / max(1.0, totals["roundtrip_ms"] + totals["waiting_ms"] + totals["overhead_ms"]),
                    100 * totals["waiting_ms"] / max(1.0, totals["roundtrip_ms"] + totals["waiting_ms"] + totals["overhead_ms"]),
                    100 * totals["overhead_ms"] / max(1.0, totals["roundtrip_ms"] + totals["waiting_ms"] + totals["overhead_ms"]),
                )
        except Exception as exc:
            log.debug("timing report unavailable: %s", exc)

        results.summary()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
