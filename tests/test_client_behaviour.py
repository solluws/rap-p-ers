"""Client behaviour regression tests.

Covers the subtle bugs: duplicated message delivery, a frozen List cursor,
sweeping non-text channels, and the exact MessageList request shape.
"""

import asyncio
import struct
import time
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

        # Poll rather than sleeping a fixed 0.35s. Two intervals is plenty of
        # wall clock right up until the machine is busy, and a flaky gate is
        # worse than a slow one. Absence still gets a fixed sleep above,
        # because polling for "nothing happened" is not a thing.
        deadline = time.monotonic() + 5.0
        while not seen and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

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


class TestSequenceContentionPause:
    """A run of 4016 closes means a second client, not a bad network.

    Both clients advance the hub's sequence cursor, so whichever resumes
    second is always "out of range". Reconnecting harder is exactly wrong --
    each attempt is another cursor fight -- and because the ordinary backoff
    caps at ``reconnect_max`` it settles into a permanent once-a-minute retry
    that never recovers. Observed in the wild as twelve identical failures
    over twelve minutes with no progress between them.
    """

    CLOSE_4016 = (
        "received 4016 (private use) Sequence out of range; "
        "then sent 4016 (private use) Sequence out of range"
    )

    @staticmethod
    def _gateway(monkeypatch, failures, **kwargs):
        """A gateway whose connection attempt fails with whatever we say.

        ``failures`` is one exception (or None for a clean, empty cycle) per
        attempt; the loop stops when the script runs out. Sleeps are recorded
        instead of taken, so the test does not spend five minutes proving it
        waits five minutes.
        """
        from rootpy.gateway import Gateway

        events = []
        slept = []

        async def dispatch(name, payload=None):
            events.append((name, payload))

        gateway = Gateway(hub_url="w", token="t", device_id="d",
                          dispatch=dispatch, reconnect_base=0.0,
                          reconnect_max=0.0, **kwargs)
        script = list(failures)

        async def run_once():
            if not script:
                gateway._stopping = True
                return
            problem = script.pop(0)
            gateway._got_data = False
            if problem is None:
                gateway._last_close_code = 1000
                return
            # Only a real 4016 carries the code; anything else closed for
            # its own reasons. Stamping every failure with it was a bug in
            # this fake, not in the gateway.
            gateway._last_close_code = (
                4016 if "4016" in str(problem) else 1006)
            raise problem

        async def no_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(gateway, "_run_once", run_once)
        monkeypatch.setattr("rootpy.gateway.asyncio.sleep", no_sleep)
        return gateway, events, slept

    def _closed(self, n):
        return [ConnectionError(self.CLOSE_4016) for _ in range(n)]

    def test_it_stands_off_after_a_run_of_them(self, monkeypatch):
        gateway, events, slept = self._gateway(
            monkeypatch, self._closed(3), contention_pause=300)
        asyncio.run(gateway._run())
        assert 300 in slept, "it never paused"
        assert ("gateway_paused", 300.0) in events
        assert ("gateway_resumed", None) in events

    def test_one_is_not_enough(self, monkeypatch):
        """A lone 4016 is ordinary: the cursor went stale over a quiet spell,
        _run_once drops it, and the next attempt works. Pausing five minutes
        for that would cost more than the bug."""
        gateway, events, slept = self._gateway(
            monkeypatch, self._closed(1), contention_pause=300)
        asyncio.run(gateway._run())
        assert 300 not in slept
        assert not [e for e in events if e[0] == "gateway_paused"]

    def test_the_threshold_is_consecutive_not_cumulative(self, monkeypatch):
        """Two, a success, then two more is a flaky link -- not contention."""
        script = self._closed(2) + [None] + self._closed(2)
        gateway, events, slept = self._gateway(
            monkeypatch, script, contention_pause=300)
        asyncio.run(gateway._run())
        assert 300 not in slept

    def test_other_failures_do_not_trigger_it(self, monkeypatch):
        gateway, events, slept = self._gateway(
            monkeypatch, [ConnectionError("connection reset by peer")] * 5,
            contention_pause=300)
        asyncio.run(gateway._run())
        assert 300 not in slept

    def test_it_is_off_unless_asked_for(self, monkeypatch):
        """Default 0.0: a library should not impose this on a caller that
        would rather see every error as it happens."""
        gateway, events, slept = self._gateway(monkeypatch, self._closed(6))
        assert gateway.contention_pause == 0.0
        asyncio.run(gateway._run())
        assert not [e for e in events if e[0] == "gateway_paused"]

    def test_it_resumes_without_the_stale_cursor(self, monkeypatch):
        """Five minutes of someone else using the account guarantees whatever
        cursor we were holding is worthless."""
        gateway, _, _ = self._gateway(
            monkeypatch, self._closed(3), contention_pause=300)
        gateway.current_sequence = 4321
        gateway.last_non_ping_sequence = 4320
        asyncio.run(gateway._run())
        assert gateway.current_sequence is None
        assert gateway.last_non_ping_sequence is None

    def test_the_threshold_is_configurable(self, monkeypatch):
        gateway, _, slept = self._gateway(
            monkeypatch, self._closed(2),
            contention_pause=42, contention_threshold=2)
        asyncio.run(gateway._run())
        assert 42 in slept

    def test_the_close_code_alone_is_enough(self, monkeypatch):
        """websockets only fills close_code once the closing handshake
        finishes, so detection reads both it and the message text. Here the
        text is useless and only the code identifies it."""
        from rootpy.gateway import Gateway
        gateway = Gateway(hub_url="w", token="t", device_id="d",
                          dispatch=lambda *a: None)
        gateway._last_close_code = 4016
        assert gateway._is_contention(ConnectionError("boom"))

    def test_the_message_alone_is_enough(self, monkeypatch):
        from rootpy.gateway import Gateway
        gateway = Gateway(hub_url="w", token="t", device_id="d",
                          dispatch=lambda *a: None)
        gateway._last_close_code = None
        assert gateway._is_contention(ConnectionError(self.CLOSE_4016))

    def test_the_client_passes_it_down(self):
        client = RootClient(token="t", contention_pause=300,
                            contention_threshold=2)
        assert client.contention_pause == 300.0
        assert client.contention_threshold == 2


class TestNothingEscapesTheProxy:
    """`proxy=` must cover every request, not merely most of them.

    A proxy believed total that quietly isn't is worse than none: the caller
    acts as though the address is hidden while a few requests carry the real
    one, on the same account, seconds from the calls that did tunnel. Two
    fetches used to do exactly that -- the remote-image download behind
    change_profile_picture/change_banner, and assets.download()/.save() off
    Root's CDN -- both built a bare httpx.AsyncClient.
    """

    PROXY = "socks5h://127.0.0.1:9"

    @staticmethod
    def _spy(monkeypatch):
        import httpx
        built = []
        base = httpx.AsyncClient

        class Spy(base):
            def __init__(self, *a, **kw):
                built.append(kw)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", Spy)
        return built

    def _client(self):
        return RootClient(token="t", proxy=self.PROXY)

    @staticmethod
    def _routed(kwargs):
        return kwargs.get("proxy") or kwargs.get("proxies")

    def test_the_grpc_client_is_proxied(self, monkeypatch):
        built = self._spy(monkeypatch)
        self._client().transport._get_client()
        assert self._routed(built[-1]) == self.PROXY

    def test_a_remote_avatar_is_fetched_through_the_proxy(self, monkeypatch):
        """change_profile_picture(URL) downloads before it uploads."""
        built = self._spy(monkeypatch)
        client = self._client()

        async def go():
            with pytest.raises(Exception):
                await client.users._resolve_asset_source(
                    "https://example.invalid/a.png")

        asyncio.run(go())
        assert built, "no client was built at all"
        assert all(self._routed(kw) == self.PROXY for kw in built)

    def test_asset_download_is_proxied(self, monkeypatch):
        """Root's CDN is still Root: a direct fetch there ties the real
        address to the account whose API call produced the URL."""
        built = self._spy(monkeypatch)
        client = self._client()

        async def go():
            try:
                await client.assets.download("root://asset/x")
            except Exception:
                pass

        asyncio.run(go())
        assert built and all(self._routed(kw) == self.PROXY for kw in built)

    def test_the_email_classmethods_are_proxied(self, monkeypatch):
        """`verify_email` and `send_email_verification` have no client behind
        them, so they build their own transport -- and used to build it bare.
        That sent the one request proving you own an address around whatever
        proxy the signup itself used, which is the worst possible request to
        leak from a provisioning run."""
        built = self._spy(monkeypatch)

        async def go():
            for coro in (
                RootClient.verify_email("ABC123", token="t", proxy=self.PROXY),
                RootClient.send_email_verification(token="t", proxy=self.PROXY),
            ):
                with pytest.raises(Exception):
                    await coro

        asyncio.run(go())
        assert built, "no client was built at all"
        assert all(self._routed(kw) == self.PROXY for kw in built)

    def test_a_supplied_transport_is_not_closed_underneath_the_caller(self):
        """They accept `transport=` too, and a caller who passes one still
        owns it -- closing it would break the next call they make on it."""
        from rootpy.transport import GrpcWebTransport

        shared = GrpcWebTransport(proxy=self.PROXY)

        async def go():
            with pytest.raises(Exception):
                await RootClient.verify_email("ABC123", token="t",
                                              transport=shared)
            return shared

        asyncio.run(go())
        assert shared._client is None or not shared._client.is_closed

    def test_no_proxy_means_no_proxy(self):
        """The default must not acquire one from anywhere."""
        transport = RootClient(token="t").transport
        assert transport._apply_proxy({}) == {}

    def test_every_httpx_client_in_the_package_is_built_by_the_transport(self):
        """The structural guarantee behind the tests above.

        Auditing three call sites is easy; noticing a fourth appear in six
        months is not. If this fails, some module grew its own client -- give
        it `transport.open_plain_client()` instead.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1] / "rootpy"
        pattern = re.compile(r"httpx\.(AsyncClient|Client)\s*\(")
        offenders = [
            f"{path.relative_to(root)}:{n}"
            for path in root.rglob("*.py")
            for n, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line) and path.name != "transport.py"
        ]
        assert offenders == [], (
            "these build their own httpx client and will ignore proxy=: "
            + ", ".join(offenders)
        )

    def test_the_proxy_kwarg_survives_a_wrapped_httpx(self, monkeypatch):
        """httpx renamed proxies -> proxy in 0.26 and dropped the old name in
        0.28, so the right one has to be chosen at runtime -- but reading the
        signature of whatever `httpx.AsyncClient` currently is breaks when
        something has subclassed it with (*args, **kwargs), which test doubles
        and tracing wrappers routinely do. That sent it down the `proxies`
        branch on an httpx that had already removed it, and every request
        raised TypeError."""
        import httpx
        from rootpy.transport import _proxy_kwarg

        expected = _proxy_kwarg()

        class Wrapped(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", Wrapped)
        assert _proxy_kwarg() == expected
