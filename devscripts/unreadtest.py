"""Three servers, three channels each, two accounts: is unread instant?

The scenario the reader exists for. One account builds three communities with
three text channels apiece and invites the other; the second account runs an
:class:`~rootpy.unread.UnreadReader` and nothing else. Then the first account
posts into all nine channels in a shuffled order and we measure how long each
post took to be noticed and cleared.

What makes this a real test rather than a demo is the *counting*. A reader that
polled would show requests climbing with time; this one should make its
requests up front -- one ListMine, one Attach per community, one GetExtended
per community -- and then effectively none, however many messages arrive.

    python devscripts/unreadtest.py
    python devscripts/unreadtest.py --keep     # leave the communities behind

Needs both tokens in tokens.txt (``root_token``, ``root_token2``).
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import statistics
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from _paths import read_token                                    # noqa: E402

from rootpy import RootClient                                    # noqa: E402
from rootpy.unread import UnreadReader                           # noqa: E402

COMMUNITIES = 3
CHANNELS = 3
#: Long enough that a failure is a failure, short enough to notice one.
ARRIVAL_TIMEOUT = 20.0


async def public_group_id(owner, community):
    """The channel group a plain member can see, found rather than assumed.

    `community.default_channel_id` names the template's public channel; the
    group it lives in is the one the default role can view. Everything else --
    "Admin", and any group created later -- grants nothing until an access rule
    says so, and a channel built there is invisible to the peer, which makes
    every post into it look like a push failure.

    Neither shortcut works. `groups[0]` is not stable: `GetExtended` returned
    `["General", "Admin"]` and `["Admin", "General"]` on consecutive runs. Nor
    is "the first text channel", because that ordering puts `Admin-Text` first
    just as often. The default channel is the only unambiguous handle.
    """
    detail = await owner.community_detail(community.id, refresh=True)
    default_id = getattr(community, "default_channel_id", None)
    for channel in detail.text_channels or ():
        if default_id and channel.id == default_id:
            return channel.channel_group_id
    return None


async def build(owner, peer, tag: str) -> list:
    """Three communities, three text channels each, peer joined to all."""
    built = []
    for index in range(COMMUNITIES):
        community = await owner.community.create(f"unread {tag} {index + 1}")
        # The group the template's own public "Text" channel lives in -- not
        # a fresh one, and not `groups[0]`. A channel group created here
        # grants nothing to the default role, so channels inside it are
        # invisible to the peer and every post into them would produce a
        # negative that means nothing. `groups[0]` is not safe either: the
        # order GetExtended returns is not stable, and it came back
        # ["Admin", "General"] often enough to make this script look like a
        # push failure. Locating the group by the channel a plain member can
        # already see is the only version that does not depend on luck.
        group_id = await public_group_id(owner, community)
        assert group_id, f"no public channel group in {community.name!r}"
        channels = []
        for slot in range(CHANNELS):
            channel = await owner.community.create_text_channel(
                community.id,
                group_id,
                f"room-{index + 1}-{slot + 1}",
                use_channel_group_permission=True,
            )
            channels.append(channel)
        invite = await owner.invites.create(community.id, max_uses=5)
        code = getattr(invite, "code", None) or getattr(invite, "id", None)
        joined = await join_with_retry(peer, community, code)
        built.append((community, channels))
        print(f"  built {community.name!r} with {len(channels)} channel(s)"
              f"{'' if joined else '  -- PEER DID NOT JOIN'}")
    return built


async def join_with_retry(peer, community, code, attempts: int = 3) -> bool:
    """Join, then confirm membership -- ``link_join`` returning is not proof.

    A join that reports success but leaves no membership is the failure mode
    that made this script look like a push problem: the peer sat in a
    community it was not in, saw no channels, and every post into it produced
    a silent negative.
    """
    for attempt in range(attempts):
        try:
            await peer.invites.join(code)
        except Exception as exc:
            if "already" not in str(exc).lower():
                print(f"  join {community.name!r} attempt {attempt + 1}: {exc}")
        for _ in range(10):
            mine = {c.id for c in await peer.list_communities(refresh=True)}
            if community.id in mine:
                return True
            await asyncio.sleep(2)
    return False


async def wait_for_membership(peer, built) -> bool:
    """The peer must see every channel before any of this means anything.

    Joining is not instantaneous and permission propagation is eventually
    consistent, so posting into a channel the listener does not yet know about
    would produce a negative that says nothing. This waits per community and
    names the one that did not arrive, because "9 channels missing" and "one
    community missing" are different problems and the first message hid that.
    """
    ok = True
    for community, channels in built:
        wanted = {c.id for c in channels}
        deadline = time.monotonic() + 90
        seen: set = set()
        while time.monotonic() < deadline:
            try:
                detail = await peer.community_detail(community.id, refresh=True)
                seen = {c.id for c in (detail.text_channels or ())}
            except Exception as exc:
                print(f"  waiting on {community.name!r}: {exc}")
            if wanted <= seen:
                break
            await asyncio.sleep(3)
        else:
            print(f"  peer never saw {len(wanted - seen)} channel(s) in "
                  f"{community.name!r}")
            ok = False
    return ok


async def run(keep: bool) -> int:
    owner = RootClient(token=read_token("root_token"))
    peer = RootClient(token=read_token("root_token2"))
    await owner.login_token()
    await peer.login_token()
    tag = uuid.uuid4().hex[:4]
    built: list = []
    failures = 0

    try:
        print(f"building {COMMUNITIES} communities x {CHANNELS} channels")
        built = await build(owner, peer, tag)
        if not await wait_for_membership(peer, built):
            return 1

        arrivals: dict = {}
        loop_ready: dict = {}

        async def on_message(message) -> None:
            content = getattr(message, "content", None)
            if content in loop_ready:
                arrivals[content] = time.monotonic() - loop_ready[content]

        await peer.connect()
        reader = UnreadReader(peer, on_message=on_message)
        async with reader:
            after_start = reader.stats.requests
            print(f"\nreader up: {reader.stats}")
            print(f"  {after_start} request(s) spent subscribing\n")

            targets = [
                (community, channel)
                for community, channels in built
                for channel in channels
            ]
            # Interleave communities rather than draining one at a time, so a
            # subscription that only half works cannot hide behind ordering.
            targets.sort(key=lambda pair: pair[1].name[-1])

            for community, channel in targets:
                marker = f"unread-{tag}-{channel.name}"
                loop_ready[marker] = time.monotonic()
                await owner.messages.send(
                    channel.id, marker, community_id=community.id
                )
                deadline = time.monotonic() + ARRIVAL_TIMEOUT
                while marker not in arrivals and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if marker in arrivals:
                    print(f"  {channel.name:<12} +{arrivals[marker]:.2f}s")
                else:
                    failures += 1
                    print(f"  {channel.name:<12} NEVER ARRIVED")

            during = reader.stats.requests - after_start
            latencies = sorted(arrivals.values())
            print(f"\n{len(arrivals)}/{len(targets)} arrived")
            if latencies:
                print(f"  median {statistics.median(latencies):.2f}s  "
                      f"worst {latencies[-1]:.2f}s")
            print(f"  {during} request(s) while {len(targets)} message(s) "
                  f"arrived  ({during / max(1, len(targets)):.1f} per message)")
            print(f"  final: {reader.stats}")
    finally:
        if built and not keep:
            for community, _ in built:
                try:
                    await owner.community.delete(community.id)
                except Exception as exc:
                    print(f"  cleanup failed for {community.id}: {exc}")
            print("\nthrowaway communities deleted")
        await owner.close()
        await peer.close()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the communities afterwards")
    args = parser.parse_args()
    return asyncio.run(run(args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
