"""Client behaviour regression tests.

Covers the subtle bugs: duplicated message delivery, a frozen List cursor,
sweeping non-text channels, and the exact MessageList request shape.
"""

import asyncio
import struct
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from rootpy import RootClient
from rootpy.events import MessageAction, MessageEvent
from rootpy.models import Channel, ChannelGroup, CommunityExtended, Message
from rootpy.protocol import iter_fields, unwrap_grpc_web

COMMUNITY = "00000000-0000-00aa-0000-000000000002"
CHANNEL = "00000000-0000-00c1-0000-000000000003"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _client():
    client = RootClient()
    client.messages._token_getter = lambda: "token"
    try:
        client.user_id = "me"
    except Exception:
        client._user_id = "me"
    return client


def _stub_community(client, activity, channel_type=1):
    async def get_communities(**_kwargs):
        return (SimpleNamespace(id=COMMUNITY, name="server"),)

    async def fetch_community(community_id):
        channel = Channel(
            id=CHANNEL,
            community_id=community_id,
            channel_group_id="group",
            name="general",
            last_activity_at=activity["value"],
            user_last_viewed_at=EPOCH,
            channel_type=channel_type,
        )
        group = ChannelGroup(
            "group", community_id, "Group", 0.0,
            channel.permissions, (), (channel,), b"",
        )
        return CommunityExtended(
            SimpleNamespace(id=community_id, name="server"), (group,), (), (), b"",
        )

    client.get_communities = get_communities
    client.fetch_community = fetch_community

    async def no_dms():
        return ()

    client.direct_messages.list = no_dms


def test_message_dispatch_is_deduplicated():
    """A mention arrives pushed AND via the sweep -- handlers must fire once."""
    async def run():
        client = _client()
        seen = []

        async def listener(message):
            seen.append(message.content)

        client.add_message_listener(listener)
        message = Message(
            id="dup-1", container_id=CHANNEL, user_id="u",
            content="hello", community_id=COMMUNITY,
        )
        for _ in range(3):
            await client.dispatch(
                "message", MessageEvent(None, message, MessageAction.CREATE, b"")
            )
        await asyncio.sleep(0.05)
        await client.close()
        return seen

    assert asyncio.run(run()) == ["hello"]


def test_list_cursor_advances_between_calls():
    """Each call must carry its own cursor -- never a cached/frozen timestamp."""
    async def run():
        client = _client()
        stamps = []

        class _Response:
            content = b""

        async def fake_unary(**kwargs):
            # The transport is stubbed here, so the body arrives unframed --
            # framing itself is covered by tests/test_wire.py.
            for number, wire, value in iter_fields(kwargs["body"]):
                if number == 13 and wire == 2:
                    for sub, _w, val in iter_fields(bytes(value)):
                        if sub == 1:
                            stamps.append(val)
            return _Response()

        client.messages.transport.unary = fake_unary
        await client.messages.list(CHANNEL, community_id=COMMUNITY, after=1000.0)
        await client.messages.list(CHANNEL, community_id=COMMUNITY, after=2000.0)
        await client.close()
        return stamps

    assert asyncio.run(run()) == [1000, 2000]


def test_list_request_matches_reference_client_shape():
    """ContainerId(10), CommunityId(11), direction(12), DateAt(13) -- no Limit."""
    async def run():
        client = _client()
        captured = {}

        class _Response:
            content = b""

        async def fake_unary(**kwargs):
            captured["body"] = kwargs["body"]
            return _Response()

        client.messages.transport.unary = fake_unary
        await client.messages.list(
            CHANNEL, community_id=COMMUNITY, direction="both",
            after=1786819972.435, limit=None,
        )
        await client.close()
        return captured["body"]

    body = asyncio.run(run())
    numbers = sorted({n for n, _w, _v in iter_fields(body)})
    assert numbers == [10, 11, 12, 13], f"unexpected fields: {numbers}"


def test_sweep_skips_non_text_channels():
    """Voice/app channels have no message container -- never fetch them."""
    async def run():
        client = _client()
        activity = {"value": EPOCH}
        _stub_community(client, activity, channel_type=4)  # VOICE
        calls = []

        async def fake_list(container_id, **kwargs):
            calls.append(container_id)
            return []

        client.messages.list = fake_list
        task = client.watch_unread(interval=0.15, include_dms=False)
        await asyncio.sleep(0.25)
        activity["value"] = EPOCH + timedelta(minutes=1)
        await asyncio.sleep(0.35)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await client.close()
        return calls

    assert asyncio.run(run()) == []


def test_sweep_surfaces_first_new_message():
    """No silent priming: the first message after startup must be delivered."""
    async def run():
        client = _client()
        activity = {"value": EPOCH}
        _stub_community(client, activity)
        seen = []

        async def listener(message):
            seen.append(message.content)

        client.add_message_listener(listener)

        async def fake_list(container_id, **kwargs):
            assert kwargs.get("direction") == "newer"
            return [
                Message(
                    id="m1", container_id=CHANNEL, user_id="u",
                    content="first hello", community_id=COMMUNITY,
                )
            ]

        client.messages.list = fake_list
        task = client.watch_unread(interval=0.15, include_dms=False)
        await asyncio.sleep(0.25)          # baseline
        assert seen == [], "baseline must not replay history"
        activity["value"] = EPOCH + timedelta(minutes=1)
        await asyncio.sleep(0.35)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await client.close()
        return seen

    assert asyncio.run(run()) == ["first hello"]


def test_watch_unread_waits_for_login():
    """No API calls before the client is authenticated."""
    async def run():
        client = RootClient()
        calls = []

        async def get_communities(**_kwargs):
            calls.append(1)
            return ()

        client.get_communities = get_communities
        task = client.watch_unread(interval=0.05)
        await asyncio.sleep(0.25)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await client.close()
        return calls

    assert asyncio.run(run()) == []
