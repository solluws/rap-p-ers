"""Which service operations cost the most requests for the work they do?

Wall clock on a live API is mostly noise -- it moves with the network. The
metric that is *deterministic*, and the one that actually predicts cost, is
**how many round trips an operation makes**, because a round trip is ~190 ms
almost regardless of payload. Every performance bug this project has found was
a request-count bug: `get_members_detailed` making 316 requests where 64 would
do, `clone_community` spending 32 of 85 requests re-reading objects whose ids
it already had, `watch_unread` fetching every community twice per sweep.

So this counts requests per operation, reports the ratio against the minimum
that operation could plausibly need, and ranks by waste.

    python devscripts/perfmap.py              # read-only operations
    python devscripts/perfmap.py --write      # also the sandbox-mutating ones

Read-only by default. ``--write`` creates one throwaway community named
``rootpy perf <tag>`` and deletes it in a finally.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _paths import read_token  # noqa: E402

from rootpy import RootClient  # noqa: E402


class Probe:
    """Counts the requests one operation makes."""

    def __init__(self, client):
        self.client = client
        self.rows = []

    async def run(self, service, name, coro_factory, *, expected=1, note=""):
        self.client.transport.stats.reset()
        started = time.perf_counter()
        try:
            result = await coro_factory()
            failed = None
        except Exception as exc:                      # noqa: BLE001
            result, failed = None, f"{type(exc).__name__}: {exc}"[:60]
        seconds = time.perf_counter() - started
        calls = self.client.transport.stats.totals()["calls"]
        size = _size_of(result)
        self.rows.append({
            "service": service,
            "op": name,
            "calls": calls,
            "seconds": seconds,
            "size": size,
            "expected": expected,
            "note": note,
            "failed": failed,
        })
        return result


def _size_of(result) -> int:
    try:
        return len(result)
    except TypeError:
        return 1 if result is not None else 0


def report(rows):
    rows = [r for r in rows if not r["failed"]]
    for r in rows:
        r["waste"] = r["calls"] / max(1, r["expected"])
    rows.sort(key=lambda r: (-r["waste"], -r["calls"]))

    print(f"\n{'service':<18}{'operation':<26}{'reqs':>6}{'min':>5}"
          f"{'ratio':>7}{'items':>7}{'sec':>7}")
    print("-" * 82)
    for r in rows:
        flag = "  <-- " + r["note"] if r["waste"] >= 2 and r["note"] else ""
        print(f"{r['service']:<18}{r['op']:<26}{r['calls']:>6}"
              f"{r['expected']:>5}{r['waste']:>7.1f}{r['size']:>7}"
              f"{r['seconds']:>7.2f}{flag}")

    worst = [r for r in rows if r["waste"] >= 2]
    print(f"\n{len(worst)} operation(s) make at least twice the requests they need.")
    for r in worst:
        extra = r["calls"] - r["expected"]
        print(f"  {r['service']}.{r['op']}: {extra} avoidable request(s)"
              f"  (~{extra * 0.19:.1f}s at 190 ms each)")


async def read_only(probe, client):
    communities = await client.list_communities()
    if not communities:
        print("no communities on this account"); return
    target = max(
        communities,
        key=lambda c: 0,
    )
    cid = communities[0].id

    await probe.run("client", "list_communities",
                    lambda: client.list_communities(refresh=True), expected=1)
    await probe.run("community", "community_detail",
                    lambda: client.community_detail(cid, refresh=True), expected=1)
    await probe.run("client", "whoami",
                    lambda: client.whoami(refresh=True), expected=1)
    await probe.run("members", "get_members",
                    lambda: client.get_members(cid, refresh=True), expected=1)

    members = await client.get_members(cid)
    n = len(members)
    # profiles come back in one request however many ids -- laddered to 4000
    await probe.run(
        "members", f"get_members_detailed({n})",
        lambda: client.get_members_detailed(cid, refresh=True),
        expected=2, note="members + profiles",
    )
    await probe.run("roles", "list",
                    lambda: client.roles.list(cid), expected=1)
    await probe.run("directories", "list",
                    lambda: client.directories.list(community_id=cid), expected=1)
    await probe.run("notifications", "list",
                    lambda: client.notifications.list(), expected=1)
    await probe.run("notifications", "count_unviewed",
                    lambda: client.notifications.count_unviewed(), expected=1)
    await probe.run("friends", "list",
                    lambda: client.friends.list(), expected=1)
    await probe.run("friend_requests", "pending",
                    lambda: client.friend_requests.pending(), expected=1)
    await probe.run("friend_groups", "list",
                    lambda: client.friend_groups.list(), expected=1)
    await probe.run("blocks", "list",
                    lambda: client.blocks.list(), expected=1)
    await probe.run("direct_messages", "list",
                    lambda: client.direct_messages.list(), expected=1)
    await probe.run("emojis", "list",
                    lambda: client.emojis.list(cid), expected=1)
    await probe.run("invites", "list",
                    lambda: client.invites.list(cid), expected=1)
    await probe.run("user_settings", "get",
                    lambda: client.user_settings.get(), expected=1)

    detail = await client.community_detail(cid)
    channels = list(getattr(detail, "text_channels", ()))
    if channels:
        ch = channels[0]
        await probe.run("messages", "list (one page)",
                        lambda: client.messages.list(ch.id, community_id=cid),
                        expected=1)
        await probe.run(
            "messages", "history(limit=50)",
            lambda: _drain(client.messages.history(ch.id, community_id=cid,
                                                   limit=50, page_size=50)),
            expected=1, note="one page of 50",
        )

    # assets: the batching path
    uris = []
    for m in await client.get_members_detailed(cid):
        for attr in ("profile_picture_uri", "banner_uri"):
            v = getattr(m, attr, None)
            if isinstance(v, str) and v.startswith("root://"):
                uris.append(v)
    uris = uris[:100]
    if uris:
        await probe.run(
            "assets", f"resolve({len(uris)} uris)",
            lambda: client.assets.resolve(uris),
            expected=max(1, len(uris) // 5),
            note="chunked; bad uris force retries",
        )


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


async def write_ops(probe, client):
    tag = uuid.uuid4().hex[:8]
    community = await client.community.create(f"rootpy perf {tag}")
    try:
        group = await client.community.create_channel_group(
            community.id, "test area"
        )
        for i in range(3):
            await client.community.create_text_channel(
                community.id, group.id, f"perf-{tag}-{i}"
            )
        for i in range(2):
            await client.community.create_role(community.id, f"perf-{tag}-{i}")

        await probe.run(
            "community_admin", "clone_community",
            lambda: client.admin.clone_community(community.id,
                                                 name=f"rootpy perf c{tag}"),
            expected=1 + 1 + 2 + 3,      # create + group + roles + channels
            note="one GetExtended per created object",
        )
    finally:
        for c in await client.list_communities(refresh=True):
            if str(c.name).startswith(f"rootpy perf"):
                try:
                    await client.community.delete(c.id)
                except Exception as exc:                 # noqa: BLE001
                    print(f"  !! cleanup failed for {c.id}: {exc}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="also measure sandbox-mutating operations")
    args = ap.parse_args()

    async with RootClient(token=read_token("root_token")) as client:
        await client.login_token()
        probe = Probe(client)
        await read_only(probe, client)
        if args.write:
            await write_ops(probe, client)
        report(probe.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
