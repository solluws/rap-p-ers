"""The unread reader: Root's own definition of "bold", and acting on it.

``watch_unread`` answers a different question -- "did this channel's activity
increase since I started watching?" -- and never reads ``user_last_viewed_at``.
That misses anything already unread when it starts, and does not clear
anything. ``UnreadReader`` implements the state a person actually sees: Root's
``Channel.HasActivity``, cleared by opening the channel.

The predicate is taken from the decompiled client
(``RootApp.Client.CoreDomain/Models/Community/Channel.cs``)::

    (lastActivityAt.HasValue & userLastViewedAt.HasValue)
        && lastActivityAt > userLastViewedAt
        && Type != ChannelType.Voice

Every branch of that is pinned below, because the "both must be present" half
is the one that is easy to get wrong and fails in the most misleading way: it
makes every channel in every community you have ever joined look permanently
unread.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rootpy.unread import ReaderStats, UnreadChannel, UnreadReader, is_unread


def channel(activity, viewed, *, channel_type=1, cid="c1", name="general"):
    return SimpleNamespace(
        id=cid, name=name, channel_type=channel_type,
        last_activity_at=activity, user_last_viewed_at=viewed,
    )


class TestTheUnreadPredicate:
    """Root's rule, branch by branch."""

    def test_activity_after_viewing_is_unread(self):
        assert is_unread(channel(20, 10))

    def test_viewing_after_activity_is_read(self):
        assert not is_unread(channel(10, 20))

    def test_equal_timestamps_are_read(self):
        """Strictly greater, not >=. Marking read sets viewed to activity."""
        assert not is_unread(channel(10, 10))

    def test_never_opened_is_not_unread(self):
        """The half that is easy to get wrong.

        A channel with no ``user_last_viewed_at`` has never been opened. Root
        does not call that unread, and treating it as unread makes every
        channel in every community bold forever -- so the reader would open
        and 'read' the entire account on its first sweep.
        """
        assert not is_unread(channel(20, None))

    def test_no_activity_is_not_unread(self):
        assert not is_unread(channel(None, 10))

    def test_neither_is_not_unread(self):
        assert not is_unread(channel(None, None))

    def test_voice_channels_are_excluded(self):
        """Voice carries activity that is not unread messages."""
        assert not is_unread(channel(20, 10, channel_type=4))

    @pytest.mark.parametrize("kind", [1, 2])
    def test_text_kinds_are_included(self, kind):
        assert is_unread(channel(20, 10, channel_type=kind))

    def test_uncomparable_timestamps_do_not_raise(self):
        """A shape we did not expect is 'not unread', never a crash."""
        assert not is_unread(channel(20, "yesterday"))


class _FakeCommunityManager:
    """The attach/detach half of ``client.community``."""

    def __init__(self, client):
        self._client = client

    async def attach(self, community_id):
        self._client.calls.append(f"attach:{community_id}")
        self._client.attached.append(community_id)

    async def detach(self, community_id):
        self._client.calls.append(f"detach:{community_id}")
        self._client.detached.append(community_id)

    async def detach_many(self, community_ids):
        ids = list(community_ids)
        self._client.calls.append(f"detach_many:{len(ids)}")
        self._client.detached.extend(ids)


class _FakeClient:
    """Enough client to drive a sweep without a network."""

    def __init__(self, communities, details):
        self._communities = communities
        self._details = details
        self.calls = []
        self.marked = []
        self.listeners = {}
        self.attached = []
        self.detached = []
        self.is_connected = True
        self.communities = {c.id: c for c in communities}
        self._community_extended = dict(details)
        self.community = _FakeCommunityManager(self)

    async def list_communities(self, *, refresh=False):
        self.calls.append("list_communities")
        return self._communities

    async def community_detail(self, community_id, *, refresh=False):
        self.calls.append(f"detail:{community_id}")
        return self._details[community_id]

    def add_listener(self, name, fn):
        self.listeners.setdefault(name, []).append(fn)

    def remove_listener(self, name, fn):
        self.listeners.get(name, []).remove(fn)

    @property
    def messages(self):
        client = self

        class _Messages:
            async def list(self, container_id, **kwargs):
                client.calls.append(f"history:{container_id}")
                return [SimpleNamespace(content="hello", id="m1")]

            async def set_view_time(self, container_id, *, community_id=None):
                client.calls.append(f"mark:{container_id}")
                client.marked.append(container_id)

        return _Messages()


def build(*channels):
    community = SimpleNamespace(id="cm1", name="Test")
    detail = SimpleNamespace(text_channels=list(channels))
    return _FakeClient([community], {"cm1": detail})


class TestSweep:
    """One pass, driven directly -- which is why ``sweep()`` is public."""

    def test_it_finds_the_unread_channel(self):
        client = build(channel(20, 10, cid="a"), channel(10, 20, cid="b"))
        reader = UnreadReader(client)
        found = asyncio.run(reader.sweep())
        assert [f.channel_id for f in found] == ["a"]

    def test_it_marks_what_it_opened(self):
        client = build(channel(20, 10, cid="a"))
        asyncio.run(UnreadReader(client).sweep())
        assert client.marked == ["a"]

    def test_mark_read_false_reads_without_clearing(self):
        client = build(channel(20, 10, cid="a"))
        asyncio.run(UnreadReader(client, mark_read=False).sweep())
        assert client.marked == []

    def test_history_is_only_fetched_when_wanted(self):
        """No on_message handler means nobody wants the text."""
        client = build(channel(20, 10, cid="a"))
        asyncio.run(UnreadReader(client).sweep())
        assert not any(c.startswith("history:") for c in client.calls)

    def test_messages_reach_the_handler(self):
        client = build(channel(20, 10, cid="a"))
        seen = []

        async def on_message(message):
            seen.append(message.content)

        asyncio.run(UnreadReader(client, on_message=on_message).sweep())
        assert seen == ["hello"]

    def test_a_channel_is_opened_once_per_burst(self):
        """Same last_activity_at twice is the same bold state, not new news."""
        client = build(channel(20, 10, cid="a"))
        reader = UnreadReader(client)

        async def twice():
            first = await reader.sweep()
            second = await reader.sweep()
            return first, second

        first, second = asyncio.run(twice())
        assert len(first) == 1
        assert second == [], "the same activity was reported twice"

    def test_new_activity_reopens_the_channel(self):
        client = build(channel(20, 10, cid="a"))
        reader = UnreadReader(client)

        async def run():
            await reader.sweep()
            client._details["cm1"].text_channels = [channel(30, 10, cid="a")]
            return await reader.sweep()

        assert len(asyncio.run(run())) == 1

    def test_one_request_per_community_not_per_channel(self):
        """The whole point: GetExtended answers for every channel at once."""
        client = build(*(channel(10, 20, cid=f"c{i}") for i in range(12)))
        asyncio.run(UnreadReader(client).sweep())
        details = [c for c in client.calls if c.startswith("detail:")]
        assert len(details) == 1, (
            f"12 channels cost {len(details)} GetExtended calls; it should be "
            "one per community"
        )


class TestPacing:
    def test_the_community_list_is_cached_between_sweeps(self):
        client = build(channel(10, 20, cid="a"))
        reader = UnreadReader(client, community_refresh=60.0)

        async def run():
            await reader.sweep()
            await reader.sweep()

        asyncio.run(run())
        listings = [c for c in client.calls if c == "list_communities"]
        assert len(listings) == 1, "re-listed communities on every sweep"

    def test_backoff_is_off_by_default(self):
        """Backing off makes the first message after a lull slow."""
        reader = UnreadReader(build())
        assert reader.idle_interval == reader.interval

    def test_quiet_sweeps_slow_it_down_when_enabled(self):
        client = build(channel(10, 20, cid="a"))     # nothing unread
        reader = UnreadReader(client, interval=1.0, idle_interval=9.0,
                              idle_after=2)

        async def run():
            paces = [reader.current_interval]
            for _ in range(3):
                await reader.sweep()
                paces.append(reader.current_interval)
            return paces

        paces = asyncio.run(run())
        assert paces[0] == 1.0
        assert paces[-1] == 9.0

    def test_finding_something_snaps_back_to_fast(self):
        client = build(channel(10, 20, cid="a"))
        reader = UnreadReader(client, interval=1.0, idle_interval=9.0,
                              idle_after=1)

        async def run():
            await reader.sweep()                       # quiet -> slow
            slow = reader.current_interval
            client._details["cm1"].text_channels = [channel(20, 10, cid="a")]
            await reader.sweep()                       # found -> fast
            return slow, reader.current_interval

        slow, fast = asyncio.run(run())
        assert slow == 9.0 and fast == 1.0


class TestStats:
    def test_it_counts_what_it_did(self):
        client = build(channel(20, 10, cid="a"))
        reader = UnreadReader(client)
        asyncio.run(reader.sweep())
        assert reader.stats.sweeps == 1
        assert reader.stats.channels_opened == 1
        assert reader.stats.requests >= 2      # list + detail
        assert "sweep" in str(reader.stats)

    def test_a_fresh_stats_object_does_not_divide_by_zero(self):
        assert ReaderStats().requests_per_minute >= 0.0


class TestUnreadChannel:
    def test_it_reads_like_a_place(self):
        unread = UnreadChannel("c", "general", "cm", "My Server")
        assert str(unread) == "#general in My Server"


class TestAttach:
    """The subscription, without which the socket stays silent.

    Membership does not subscribe a connection: until ``Attach`` is called the
    hub sends no packet for a community at all. Everything below exists so a
    regression that quietly drops the attach shows up as a failing test rather
    than as a bot that looks connected and never reacts.
    """

    def test_starting_attaches_to_every_community(self):
        client = build(channel(20, 10, cid="a"))

        async def run():
            reader = UnreadReader(client)
            await reader.start()
            await reader.stop()

        asyncio.run(run())
        assert client.attached == ["cm1"]

    def test_it_detaches_again_on_stop(self):
        client = build(channel(10, 20, cid="a"))

        async def run():
            reader = UnreadReader(client)
            await reader.start()
            await reader.stop()

        asyncio.run(run())
        assert client.detached == ["cm1"]

    def test_detach_can_be_left_alone(self):
        client = build(channel(10, 20, cid="a"))

        async def run():
            reader = UnreadReader(client, detach_on_stop=False)
            await reader.start()
            await reader.stop()

        asyncio.run(run())
        assert client.detached == []

    def test_attach_can_be_declined(self):
        """``attach=False`` keeps the account out of member presence."""
        client = build(channel(10, 20, cid="a"))

        async def run():
            reader = UnreadReader(client, attach=False)
            await reader.start()
            await reader.stop()

        asyncio.run(run())
        assert client.attached == []

    def test_one_bad_community_does_not_stop_the_others(self):
        good = SimpleNamespace(id="cm1", name="Good")
        bad = SimpleNamespace(id="cm2", name="Bad")
        client = _FakeClient(
            [good, bad],
            {"cm1": SimpleNamespace(text_channels=[]),
             "cm2": SimpleNamespace(text_channels=[])},
        )
        original = client.community.attach

        async def attach(community_id):
            if community_id == "cm2":
                raise RuntimeError("PERMISSION_DENIED")
            await original(community_id)

        client.community.attach = attach

        async def run():
            reader = UnreadReader(client)
            await reader.start()
            await reader.stop()
            return reader.stats.attached

        assert asyncio.run(run()) == 1
        assert client.attached == ["cm1"]

    def test_startup_reconciles_before_going_quiet(self):
        """A channel that went bold while the reader was down is still read."""
        client = build(channel(20, 10, cid="a"))

        async def run():
            reader = UnreadReader(client)
            await reader.start()
            await reader.stop()

        asyncio.run(run())
        assert client.marked == ["a"]


class TestPushIsTheMechanism:
    """No timer in the steady state -- the socket is the mechanism."""

    def test_there_is_no_poll_by_default(self):
        assert UnreadReader(build()).interval == 0.0

    def test_a_poll_can_still_be_asked_for(self):
        assert UnreadReader(build(), interval=5.0).interval == 5.0

    def test_a_pushed_channel_message_is_read_at_once(self):
        client = build()

        async def run():
            reader = UnreadReader(client)
            await reader.start()
            await reader._on_pushed(SimpleNamespace(
                message=SimpleNamespace(
                    content="hi", container_id="ch9", community_id="cm1",
                )
            ))
            await reader.stop()
            return reader.stats

        stats = asyncio.run(run())
        assert "ch9" in client.marked
        assert stats.pushed_messages == 1
        assert stats.channels_opened == 1

    def test_a_pushed_dm_is_not_a_channel_going_unread(self):
        """A DM carries no community, and is not a bold channel."""
        client = build()
        seen = []

        async def on_unread(unread):
            seen.append(unread)

        async def run():
            reader = UnreadReader(client, on_unread=on_unread)
            await reader.start()
            await reader._on_pushed(SimpleNamespace(
                message=SimpleNamespace(
                    content="hi", container_id="dm1", community_id=None,
                )
            ))
            await reader.stop()

        asyncio.run(run())
        assert seen == []

    def test_a_pushed_channel_is_named_from_cache_not_a_request(self):
        client = build(channel(10, 20, cid="ch9"))
        client._details["cm1"].text_channels[0].name = "general"
        seen = []

        async def on_unread(unread):
            seen.append(str(unread))

        async def run():
            reader = UnreadReader(client, on_unread=on_unread)
            await reader.start()
            before = len(client.calls)
            await reader._on_pushed(SimpleNamespace(
                message=SimpleNamespace(
                    content="hi", container_id="ch9", community_id="cm1",
                )
            ))
            during = client.calls[before:]
            await reader.stop()
            return during

        during = asyncio.run(run())
        assert seen == ["#general in Test"]
        # Naming a channel costs nothing: the only call the push made is the
        # click itself. A lookup that re-fetched the community to read a name
        # would put a `detail:` call in here.
        assert during == ["mark:ch9"]
