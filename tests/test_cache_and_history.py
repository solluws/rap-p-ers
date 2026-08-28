"""Tests for the caching layer, lazy init, and the history iterator."""

import asyncio
from types import SimpleNamespace

import pytest

from rootpy import RootClient
from rootpy.cache import LRUCache, StateCache
from rootpy.events import MessageAction, MessageEvent
from rootpy.models import Community, CurrentUser, Message


def _community(index=0):
    return Community(
        id=f"00000000-0000-00aa-0000-{index:012d}",
        name=f"c{index}",
        owner_user_id="me",
        default_channel_id=None,
    )


# --- cache ---------------------------------------------------------------- #
def test_lru_evicts_least_recently_used():
    cache = LRUCache(maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")          # 'a' is now the most recently used
    cache.set("c", 3)       # evicts 'b'
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_state_cache_remembers_and_reports():
    cache = StateCache()
    cache.remember_user("u1", "alice")
    assert cache.username("u1") == "alice"
    assert cache.username("missing") is None
    stats = cache.stats()
    assert stats["users"] == 1
    assert stats["hits"] >= 1 and stats["misses"] >= 1


def test_state_cache_merges_partial_records():
    cache = StateCache()
    cache.remember_user("u1")                 # id only
    cache.remember_user("u1", "alice")        # later we learn the name
    assert cache.username("u1") == "alice"


def test_cache_never_fetches_on_miss():
    """A miss returns None -- event handling must never block on a request."""
    cache = StateCache()
    assert cache.user("nobody") is None
    assert cache.member("comm", "nobody") is None


def test_typed_events_resolve_names_from_cache():
    async def run():
        client = RootClient()
        client.cache.remember_user("user-a", "alice")
        seen = {}

        @client.event
        async def on_friend_request(event):
            seen["event"] = event

        from rootpy.packets import PacketType, SocketPacket

        await client.dispatch("packet", SocketPacket(
            sequence=1, type=PacketType.FRIENDSHIP_CREATED, case=0,
            data={"fields": {"friend_user_id": "user-a"}}, raw=b"",
        ))
        await asyncio.sleep(0.05)
        await client.close()
        return seen["event"]

    event = asyncio.run(run())
    assert event.username == "alice"


# --- lazy init ------------------------------------------------------------ #
def test_login_does_not_expand_every_community():
    """Eager expansion cost one GetExtended per community before login finished."""
    async def run(expand):
        calls = {"fetch": 0}
        client = RootClient(expand_communities=expand)

        async def list_mine():
            return tuple(_community(i) for i in range(10))

        async def fetch(community_id):
            calls["fetch"] += 1
            return SimpleNamespace(
                community=_community(), channel_groups=(), members=(), roles=(),
            )

        async def get_self():
            return CurrentUser(id="me", username="tester")

        client.community_service.list_mine = list_mine
        client.fetch_community = fetch
        client.users.get_self = get_self
        await client._initialize_authenticated_state()
        await client.close()
        return calls["fetch"]

    assert asyncio.run(run(False)) == 0     # lazy (new default)
    assert asyncio.run(run(True)) == 10     # eager (opt in)


def test_community_detail_caches():
    async def run():
        client = RootClient()
        calls = {"n": 0}

        async def fetch(community_id):
            calls["n"] += 1
            result = SimpleNamespace(
                community=_community(), channel_groups=(), members=(), roles=(),
            )
            client._community_extended[community_id] = result
            return result

        client.fetch_community = fetch
        cid = _community().id
        for _ in range(3):
            await client.community_detail(cid)
        first = calls["n"]
        await client.community_detail(cid, refresh=True)
        await client.close()
        return first, calls["n"]

    first, after_refresh = asyncio.run(run())
    assert first == 1
    assert after_refresh == 2


# --- history iterator ----------------------------------------------------- #
def test_history_paginates_and_dedupes():
    # page_size is MessageList's Limit, which Root range-checks to 10..50, so
    # the pages here are 10 long. This used to use 3 -- a value the server
    # refuses outright, so the test was exercising a walk that could never
    # happen against Root.
    async def run():
        client = RootClient()
        client.messages._token_getter = lambda: "token"
        pages = [
            [Message(id=f"m{i}", container_id="c", user_id="u",
                     content=str(i), community_id=None)
             for i in range(start, start + 10)]
            for start in (0, 10, 20)
        ]
        # the last page repeats -- the iterator must stop, not loop
        pages.append(pages[-1])

        async def fake_list(container_id, **kwargs):
            return pages.pop(0) if pages else []

        client.messages.list = fake_list
        collected = [
            message.content
            async for message in client.history("00000000-0000-0003-0000-000000000004",
                                                page_size=10, limit=None)
        ]
        await client.close()
        return collected

    collected = asyncio.run(run())
    assert len(collected) == 30
    assert len(set(collected)) == 30     # no duplicates across pages


def test_history_respects_limit():
    async def run():
        client = RootClient()
        client.messages._token_getter = lambda: "token"

        async def fake_list(container_id, **kwargs):
            return [
                Message(id=f"m{i}", container_id="c", user_id="u",
                        content=str(i), community_id=None)
                for i in range(50)
            ]

        client.messages.list = fake_list
        collected = [
            m async for m in client.history("00000000-0000-0003-0000-000000000004",
                                            limit=5, page_size=50)
        ]
        await client.close()
        return collected

    assert len(asyncio.run(run())) == 5


def test_login_overlaps_independent_calls():
    """GetSelf and ListMine don't depend on each other -- they must overlap."""
    async def run():
        client = RootClient()
        active = {"now": 0, "peak": 0}

        async def slow(result):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.15)
            active["now"] -= 1
            return result

        async def get_self():
            return await slow(CurrentUser(id="me", username="tester"))

        async def list_mine():
            return await slow((_community(0), _community(1)))

        client.users.get_self = get_self
        client.community_service.list_mine = list_mine
        await client._initialize_authenticated_state()
        await client.close()
        return active["peak"]

    assert asyncio.run(run()) == 2, "login calls ran sequentially"


def test_login_still_raises_on_auth_failure():
    """Overlapping must not swallow a GetSelf failure."""
    async def run():
        client = RootClient()

        async def get_self():
            raise RuntimeError("bad token")

        async def list_mine():
            return ()

        client.users.get_self = get_self
        client.community_service.list_mine = list_mine
        try:
            await client._initialize_authenticated_state()
            return False
        except RuntimeError:
            return True
        finally:
            await client.close()

    assert asyncio.run(run())


# --- fire-and-forget send ------------------------------------------------- #
def test_send_needs_only_a_token():
    """A token alone is enough for API calls -- no session/login round-trips."""
    async def run():
        calls = []
        client = RootClient(token="test-token")

        class _Response:
            content = b"\x00\x00\x00\x00\x00"

        async def fake_unary(**kwargs):
            calls.append(kwargs["endpoint"].rsplit("/", 1)[-1])
            return _Response()

        client.messages.transport.unary = fake_unary
        await client.messages.send(
            "00308801-dfec-8404-bb84-bf060b2169ba", "hello",
            community_id="0030735e-ddbf-8d02-99de-255bc3fc5dc5",
        )
        await client.close()
        return calls

    assert asyncio.run(run()) == ["Create"]


def test_no_token_and_no_session_still_errors():
    async def run():
        client = RootClient()
        try:
            client._require_token()
            return False
        except RuntimeError:
            return True
        finally:
            await client.close()

    assert asyncio.run(run())


def test_gateway_still_requires_login():
    """Token-only is fine for RPCs, but the gateway needs real session state."""
    async def run():
        client = RootClient(token="test-token")
        try:
            await client.connect()
            return False
        except RuntimeError:
            return True
        finally:
            await client.close()

    assert asyncio.run(run())


def test_cache_records_ids_without_names():
    """Root's member/message objects carry only ids -- seeing a user must
    still register them, so a name can be merged in later."""
    cache = StateCache()
    cache.remember_user("u1")                  # id only, no name available
    assert cache.stats()["users"] == 1
    assert cache.username("u1") is None
    cache.remember_user("u1", "alice")         # name learned later
    assert cache.username("u1") == "alice"


def test_notification_teaches_cache_a_username():
    """Notifications embed the author's display name -- the one live source."""
    import struct

    from rootpy.cache import StateCache
    from rootpy.gateway import Gateway
    from rootpy.packets import PacketType, SocketPacket

    def varint(value):
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    def tag(field, wire):
        return varint((field << 3) | wire)

    def text(field, value):
        raw = value.encode()
        return tag(field, 2) + varint(len(raw)) + raw

    def msg(field, data):
        return tag(field, 2) + varint(len(data)) + bytes(data)

    def uuid(high, low):
        return tag(1, 1) + struct.pack("<Q", high) + tag(2, 1) + struct.pack("<Q", low)

    profile = msg(14, msg(10, msg(10, uuid(0xA0, 0xA1)) + text(11, "alice")))
    embedded = tag(1, 0) + varint(5805) + msg(5, uuid(0xA0, 0xA1)) \
        + text(10, "hey there") + profile
    payload = msg(6, msg(4, embedded) + text(5, "community"))

    gateway = Gateway(hub_url="w", token="t", device_id="d", dispatch=lambda *a: None)
    gateway.client_cache = StateCache()
    packet = SocketPacket(
        sequence=1, type=PacketType.NOTIFICATION, case=180,
        data={"fields": {"payload": payload, "user_id": "u-me"}}, raw=b"",
    )
    message = gateway._message_from_notification(packet)
    assert message.content == "hey there"
    assert gateway.client_cache.username(message.user_id) == "alice"


# --- per-frame cost ------------------------------------------------------- #
def _frame():
    """A frame shaped like a real notification: container nested in message."""
    from rootpy.protocol import length_field, string_field

    def varint(value):
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    payload = bytearray()
    payload += string_field(10, "0030bf26-999d-8101-82fd-66b280480024")
    payload += string_field(12, "hello " * 20)
    for number in range(14, 22):
        payload += string_field(number, f"field-{number}-{'x' * 20}")

    container = bytes(length_field(3, bytes(payload)))
    message = bytes(length_field(2, container) + varint(1 << 3) + varint(42))
    return message, container


class TestSocketResponseDebugTreesAreOptIn:
    """The two exploratory trees are a debugging aid, and they were not free.

    ``_socket_response`` eagerly built ``data["notification"]`` and
    ``data["packet_container"]`` on **every** frame, keepalive pings included.
    ``_bytes_to_json`` hexes and UTF-8-decodes every length-delimited field and
    recurses six levels, and ``packet_container``'s tree is a strict subtree of
    ``notification``'s -- recomputed from scratch. Measured on a 556-byte
    frame: 107.7 us of 169.1 us, 64% of the function, for output no library
    code, test, devscript or example reads.

    They are now opt-in. Both keys still exist, so the response shape is
    unchanged; they simply carry ``None`` until asked for.
    """

    def _both(self):
        from rootpy.gateway import Gateway

        message, container = _frame()
        off = Gateway._socket_response(message, 42, 3, container)
        on = Gateway._socket_response(
            message, 42, 3, container, debug_trees=True
        )
        return off, on

    def test_the_keys_still_exist_either_way(self):
        """Opting out must not change the shape, only the cost."""
        off, on = self._both()
        assert set(off) == set(on)
        assert "notification" in off and "packet_container" in off

    def test_they_are_empty_by_default(self):
        off, _ = self._both()
        assert off["notification"] is None
        assert off["packet_container"] is None

    def test_asking_for_them_still_works(self):
        _, on = self._both()
        assert on["notification"], "debug_trees=True must still build the tree"
        assert on["packet_container"]

    def test_everything_callers_actually_read_is_unchanged(self):
        """``fields`` and ``packet`` are the keys with real consumers."""
        off, on = self._both()
        for key in ("fields", "packet", "raw_hex", "packet_raw_hex",
                    "sequence", "packet_case"):
            assert off[key] == on[key], f"{key} changed with debug_trees"

    def test_the_default_is_off_on_a_real_gateway(self):
        from rootpy.gateway import Gateway

        gateway = Gateway(
            hub_url="w", token="t", device_id="d", dispatch=lambda *a: None
        )
        assert gateway.decode_debug_trees is False

    def test_skipping_them_is_actually_cheaper(self):
        """The point of the change, asserted rather than assumed.

        A loose bound -- the measured gap is ~2.9x -- so this fails only if
        the trees are being built regardless of the flag.
        """
        import time

        from rootpy.gateway import Gateway

        message, container = _frame()

        def timed(**kwargs):
            for _ in range(50):            # warm
                Gateway._socket_response(message, 42, 3, container, **kwargs)
            started = time.perf_counter()
            for _ in range(300):
                Gateway._socket_response(message, 42, 3, container, **kwargs)
            return time.perf_counter() - started

        off = timed()
        on = timed(debug_trees=True)
        assert off < on / 1.5, (
            f"debug trees off took {off*1000:.1f} ms vs on {on*1000:.1f} ms "
            "-- the flag is not skipping the work"
        )


# --- resource bounds ------------------------------------------------------ #
def test_message_cache_is_bounded():
    """An always-on watcher would otherwise retain every message it ever saw."""
    async def run():
        client = RootClient(message_cache_size=100)
        for index in range(5000):
            message = Message(
                id=f"m{index}", container_id="c", user_id="u",
                content="x", community_id=None,
            )
            await client.dispatch(
                "message", MessageEvent(None, message, MessageAction.CREATE, b"")
            )
        size = len(client.message_cache)
        newest = client.message_cache.get("m4999")
        oldest = client.message_cache.get("m0")
        await client.close()
        return size, newest, oldest

    size, newest, oldest = asyncio.run(run())
    assert size == 100
    assert newest is not None      # recent entries survive
    assert oldest is None          # old ones are evicted


def test_sent_message_ids_are_bounded():
    """Ids are cleared when the gateway echoes them; a missing echo must not
    leak forever."""
    client = RootClient()
    for index in range(3000):
        client._sent_message_ids.add(f"s{index}")
        client._sent_message_order.append(f"s{index}")
        if len(client._sent_message_order) > 1000:
            stale = client._sent_message_order.popleft()
            client._sent_message_ids.discard(stale)
    assert len(client._sent_message_ids) <= 1000


def test_lru_cache_supports_dict_access():
    from rootpy.cache import LRUCache

    cache = LRUCache(maxsize=2)
    cache["a"] = 1
    assert cache["a"] == 1
    assert "a" in cache
    cache["b"] = 2
    cache["c"] = 3           # evicts "a"
    assert cache.get("a") is None
    assert sorted(cache.keys()) == ["b", "c"]


class TestMessageListLimitWindow:
    """``Limit`` is optional, and when present must be 10..50.

    LLMS.md said "MessageList must not include Limit; sending it gets the
    request rejected" -- while ``history()``, the paging API the same document
    recommends, sends ``Limit=page_size`` on every page and works. Both could
    not be true, so it was laddered against the live API and Root answered in
    its own words:

        Limit: Must be between 10 and 50 [InclusiveBetweenValidator]

    Measured: omitted -> OK (50 back); 1, 2, 5, 9 -> INVALID_ARGUMENT; 10-50 ->
    OK returning exactly that many; 51, 55, 60, 75, 99, 100 -> INVALID_ARGUMENT.

    Checked locally now, because the alternative is a round trip per page that
    fails identically every time.
    """

    def _client(self):
        client = RootClient()
        client.messages._token_getter = lambda: "token"
        return client

    def test_the_window_matches_what_root_said(self):
        from rootpy.services.messages import MessageService

        assert MessageService.LIMIT_MIN == 10
        assert MessageService.LIMIT_MAX == 50

    @pytest.mark.parametrize("limit", [0, 1, 2, 5, 9, 51, 60, 100, 500])
    def test_an_out_of_range_limit_raises_locally(self, limit):
        async def run():
            client = self._client()
            sent = {"n": 0}

            async def fake_unary(**kwargs):
                sent["n"] += 1
                raise AssertionError("a request should never have been made")

            client.messages.transport.unary = fake_unary
            try:
                with pytest.raises(ValueError, match="between 10 and 50"):
                    await client.messages.list("00000000-0000-0003-0000-000000000004",
                                               limit=limit)
            finally:
                await client.close()
            return sent["n"]

        assert asyncio.run(run()) == 0, "it made a round trip to learn this"

    @pytest.mark.parametrize("limit", [None, 10, 25, 50])
    def test_an_in_range_limit_is_accepted(self, limit):
        async def run():
            client = self._client()
            seen = {}

            async def fake_list(container_id, **kwargs):
                seen["limit"] = kwargs.get("limit")
                return []

            client.messages.list = fake_list
            # go through history so the page_size path is covered too
            got = [m async for m in client.history(
                "00000000-0000-0003-0000-000000000004",
                page_size=limit or 50, limit=None,
            )]
            await client.close()
            return got, seen

        got, seen = asyncio.run(run())
        assert got == []
        assert seen["limit"] == (limit or 50)

    @pytest.mark.parametrize("page_size", [1, 3, 9, 51, 200])
    def test_history_rejects_an_out_of_range_page_size_before_the_first_page(
        self, page_size
    ):
        """Every page would fail the same way, so fail once, up front."""
        async def run():
            client = self._client()
            calls = {"n": 0}

            async def fake_list(container_id, **kwargs):
                calls["n"] += 1
                return []

            client.messages.list = fake_list
            try:
                with pytest.raises(ValueError, match="between 10 and 50"):
                    async for _ in client.history(
                        "00000000-0000-0003-0000-000000000004",
                        page_size=page_size,
                    ):
                        pass
            finally:
                await client.close()
            return calls["n"]

        assert asyncio.run(run()) == 0


def test_history_stops_on_a_short_page():
    """A page shorter than requested means there's nothing older -- don't
    spend another round-trip discovering that."""
    async def run(total, page_size):
        client = RootClient()
        client.messages._token_getter = lambda: "token"
        calls = {"n": 0}
        remaining = [
            Message(id=f"m{i}", container_id="c", user_id="u",
                    content=str(i), community_id=None)
            for i in range(total)
        ]

        async def fake_list(container_id, **kwargs):
            calls["n"] += 1
            page = remaining[:page_size]
            del remaining[:page_size]
            return page

        client.messages.list = fake_list
        collected = [
            m async for m in client.history(
                "00000000-0000-0003-0000-000000000004",
                page_size=page_size, limit=None,
            )
        ]
        await client.close()
        return len(collected), calls["n"]

    # 6 messages with a page size of 10 is a single short page
    count, requests = asyncio.run(run(6, 10))
    assert count == 6
    assert requests == 1

    # 25 over pages of 10: two full pages then a short one
    count, requests = asyncio.run(run(25, 10))
    assert count == 25
    assert requests == 3


# --- history paging across real time --------------------------------------- #
def _message_at(when, index):
    """A message whose id encodes ``when``, the way Root's really do."""
    from rootpy.identifiers import ROOT_GUID_START_DATE, format_root_guid

    millis = int((when - ROOT_GUID_START_DATE).total_seconds() * 1000)
    high64 = (millis << 16) + 0x8000 + 3          # 3 = ROOT_GUID_TYPE_MESSAGE
    return Message(
        id=format_root_guid(high64, index + 1),
        container_id="c", user_id="u", content=str(index), community_id=None,
    )


class TestHistoryWalksPastTheFirstPage:
    """The bug this class exists for truncated every busy channel silently.

    ``history`` stepped its cursor back by ``page_size`` *seconds* between
    pages, on the theory that the model carried no timestamp. A page of 50
    messages spanning more than 50 seconds therefore put the next cursor back
    inside the page just read; the server returned the same rows; the
    duplicate check saw nothing fresh and returned, believing it had reached
    the end.

    Measured against a real channel before the fix: 50 messages walked, 1024
    actually there. The existing paging tests missed it because their fake
    ``list`` ignored the cursor and their ids were not Root GUIDs, so both
    halves of the faulty step were invisible.
    """

    @staticmethod
    def _server(total, span_seconds):
        """A fake MessageList that honours ``after`` as an older-than cursor."""
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 8, 1, tzinfo=timezone.utc)
        gap = span_seconds / max(1, total - 1)
        every = [
            _message_at(base + timedelta(seconds=gap * i), i)
            for i in range(total)
        ]

        async def fake_list(container_id, *, after=None, limit=50, **kwargs):
            from rootpy.identifiers import root_guid_datetime

            older = [
                m for m in every
                if after is None
                or root_guid_datetime(m.id).timestamp() < after
            ]
            # Root returns oldest-first, newest page last.
            return older[-limit:]

        return fake_list, every

    def _walk(self, total, span_seconds, page_size=50):
        async def run():
            client = RootClient()
            client.messages._token_getter = lambda: "token"
            client.messages.list, _every = self._server(total, span_seconds)
            collected = [
                m.content
                async for m in client.history(
                    "00000000-0000-0003-0000-000000000004",
                    page_size=page_size, limit=None,
                )
            ]
            await client.close()
            return collected

        return asyncio.run(run())

    def test_it_reaches_the_oldest_message(self):
        """120 messages over an hour: three pages, not one."""
        collected = self._walk(total=120, span_seconds=3600)
        assert len(collected) == 120
        assert set(collected) == {str(i) for i in range(120)}

    def test_a_page_spanning_days_still_advances(self):
        """The failing shape: each page covers far more than page_size seconds."""
        collected = self._walk(total=200, span_seconds=86400 * 7)
        assert len(collected) == 200

    def test_messages_closer_together_than_a_second_are_not_skipped(self):
        """The other end: a burst inside one second must not lose rows.

        The cursor steps to just *before* the page's oldest message, so
        messages sharing a millisecond are still all returned -- the id
        de-duplication, not the cursor, is what stops the repeat.
        """
        collected = self._walk(total=120, span_seconds=1)
        assert len(collected) == 120

    def test_it_still_terminates_when_ids_carry_no_time(self):
        """Unparseable ids fall back to the fixed step rather than spinning."""
        async def run():
            client = RootClient()
            client.messages._token_getter = lambda: "token"
            pages = [[Message(id=f"x{i}", container_id="c", user_id="u",
                              content=str(i), community_id=None)
                      for i in range(start, start + 50)]
                     for start in (0, 50)]

            async def fake_list(container_id, **kwargs):
                return pages.pop(0) if pages else []

            client.messages.list = fake_list
            collected = [
                m.content
                async for m in client.history(
                    "00000000-0000-0003-0000-000000000004",
                    page_size=50, limit=None,
                )
            ]
            await client.close()
            return collected

        assert len(asyncio.run(run())) == 100
