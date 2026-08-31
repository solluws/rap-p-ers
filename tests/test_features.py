"""Tests for the convenience layer: typed events, wait_for, presence,
object methods, and rate-limit awareness."""

import asyncio
import time

import pytest

from rootpy import RootClient
from rootpy.enums import UserOnlineStatus
from rootpy.models import Channel, Message
from rootpy.packets import PacketType, SocketPacket
from rootpy.protocol import iter_fields
from rootpy.transport import GrpcWebTransport


def _packet(type_name, fields):
    return SocketPacket(
        sequence=1, type=PacketType[type_name], case=0,
        data={"fields": fields}, raw=b"",
    )


# --- typed events --------------------------------------------------------- #
def test_typed_events_are_built_from_packets():
    async def run():
        client = RootClient()
        seen = {}

        @client.event
        async def on_friend_request(event):
            seen["friend"] = event

        @client.event
        async def on_role_add(event):
            seen["role"] = event

        @client.event
        async def on_channel_create(event):
            seen["channel"] = event

        await client.dispatch("packet", _packet(
            "FRIENDSHIP_CREATED",
            {"id": "f1", "friend_user_id": "user-a", "friendship_group_id": "g"},
        ))
        await client.dispatch("packet", _packet(
            "COMMUNITY_MEMBER_ROLE_CREATED",
            {"community_id": "c1", "community_role_id": "r1",
             "user_ids": ["u1", "u2"]},
        ))
        await client.dispatch("packet", _packet(
            "CHANNEL_CREATED",
            {"community_id": "c1", "id": "ch1", "name": "general",
             "channel_type": 1},
        ))
        await asyncio.sleep(0.05)
        await client.close()
        return seen

    seen = asyncio.run(run())
    assert seen["friend"].user_id == "user-a"
    assert seen["role"].user_ids == ("u1", "u2")
    assert seen["role"].user_id == "u1"
    assert seen["channel"].name == "general"
    assert seen["channel"].is_text
    assert seen["friend"].received_at is not None


def test_role_move_is_a_typed_event():
    """COMMUNITY_ROLE_MOVED reaches on_role_move, with the ordering payload.

    The slot had no route, so the event never fired even though SUBROUTES
    listed ``5404: role_move`` -- for slot 90, which cannot carry a move.
    ``before_role_id`` is the whole point of the event: without it you know a
    role moved but not where to.
    """
    async def run():
        client = RootClient()
        seen = {}

        @client.event
        async def on_role_move(event):
            seen["move"] = event

        await client.dispatch("packet", _packet(
            "COMMUNITY_ROLE_MOVED",
            {"packet_type": 5404, "community_id": "c1", "id": "r-moved",
             "before_community_role_id": "r-anchor"},
        ))
        await asyncio.sleep(0.05)
        await client.close()
        return seen

    seen = asyncio.run(run())
    assert seen["move"].role_id == "r-moved"
    assert seen["move"].before_role_id == "r-anchor"
    assert seen["move"].community_id == "c1"


def test_a_role_move_to_the_top_has_no_anchor():
    """An absent field 6 means "first", not "unknown"."""
    from rootpy.typed_events import TypedEventBuilder

    class _Client:
        communities = {}
        channels = {}
        cache = None

    built = TypedEventBuilder(_Client()).build(_packet(
        "COMMUNITY_ROLE_MOVED",
        {"packet_type": 5404, "community_id": "c1", "id": "r-moved"},
    ))
    assert built is not None
    name, event = built
    assert name == "role_move"
    assert event.before_role_id == ""


def test_role_delete_reads_its_own_field_name():
    """CommunityRoleDeletedPacket names field 4 ``community_role_id``.

    The builder read only ``id``, so every real delete arrived with an empty
    role_id. Both spellings must work: the create/move slots use ``id``.
    """
    from rootpy.typed_events import TypedEventBuilder

    class _Client:
        communities = {}
        channels = {}
        cache = None

    _, event = TypedEventBuilder(_Client()).build(_packet(
        "COMMUNITY_ROLE_DELETED",
        {"packet_type": 5403, "community_id": "c1",
         "community_role_id": "r-gone"},
    ))
    assert event.role_id == "r-gone"


@pytest.mark.parametrize("packet_type,expected,is_direct", [
    (5801, "reaction_add", False),
    (5802, "reaction_remove", False),
    (204, "reaction_add", True),
    (205, "reaction_remove", True),
])
def test_reactions_split_by_packet_type(packet_type, expected, is_direct):
    """One slot, one packet class, four meanings.

    MESSAGE_REACTION (173) carries channel add/remove (5801/5802) and DM
    add/remove (204/205). Routing on the slot alone cannot tell a removed
    reaction from a new one, nor a DM from a channel.
    """
    from rootpy.typed_events import TypedEventBuilder

    class _Client:
        communities = {}
        channels = {}
        cache = None

    fields = {"packet_type": packet_type, "container_id": "k1",
              "shortcode": ":thumbsup:", "message_id": "m1",
              "user_id": "u1"}
    if not is_direct:
        fields["community_id"] = "c1"

    name, event = TypedEventBuilder(_Client()).build(
        _packet("MESSAGE_REACTION", fields)
    )
    assert name == expected
    assert event.is_direct is is_direct
    assert event.shortcode == ":thumbsup:"
    assert event.message_id == "m1"
    assert event.container_id == "k1"


def test_a_reaction_without_a_decoded_packet_type_falls_back():
    """No field 1 means we guess from the absent community, not crash."""
    from rootpy.typed_events import TypedEventBuilder

    class _Client:
        communities = {}
        channels = {}
        cache = None

    name, event = TypedEventBuilder(_Client()).build(_packet(
        "MESSAGE_REACTION",
        {"container_id": "d1", "shortcode": "x", "message_id": "m1",
         "user_id": "u1"},
    ))
    assert name == "reaction_add"
    assert event.is_direct is True


def test_reaction_events_reach_a_handler():
    async def run():
        client = RootClient()
        seen = {}

        @client.event
        async def on_reaction_add(event):
            seen["add"] = event

        @client.event
        async def on_reaction_remove(event):
            seen["remove"] = event

        await client.dispatch("packet", _packet(
            "MESSAGE_REACTION",
            {"packet_type": 5801, "community_id": "c1", "container_id": "ch1",
             "shortcode": ":fire:", "message_id": "m1", "user_id": "u1"},
        ))
        await client.dispatch("packet", _packet(
            "MESSAGE_REACTION",
            {"packet_type": 205, "container_id": "dm1", "shortcode": ":wave:",
             "message_id": "m2", "user_id": "u2"},
        ))
        await asyncio.sleep(0.05)
        await client.close()
        return seen

    seen = asyncio.run(run())
    assert seen["add"].shortcode == ":fire:"
    assert seen["add"].is_direct is False
    assert seen["remove"].shortcode == ":wave:"
    assert seen["remove"].is_direct is True


def test_unmapped_packets_do_not_raise():
    async def run():
        client = RootClient()
        await client.dispatch("packet", _packet("PING", {}))
        await client.close()

    asyncio.run(run())  # must not raise


# --- wait_for ------------------------------------------------------------- #
def test_wait_for_returns_matching_event():
    async def run():
        client = RootClient()

        async def emit():
            await asyncio.sleep(0.05)
            await client.dispatch("member_join", {"user_id": "wanted"})

        asyncio.create_task(emit())
        event = await client.wait_for("member_join", timeout=2)
        await client.close()
        return event

    assert asyncio.run(run())["user_id"] == "wanted"


def test_wait_for_check_filters():
    async def run():
        client = RootClient()

        async def emit():
            await asyncio.sleep(0.03)
            await client.dispatch("member_join", {"user_id": "no"})
            await asyncio.sleep(0.03)
            await client.dispatch("member_join", {"user_id": "yes"})

        asyncio.create_task(emit())
        event = await client.wait_for(
            "member_join", check=lambda e: e["user_id"] == "yes", timeout=2
        )
        await client.close()
        return event

    assert asyncio.run(run())["user_id"] == "yes"


def test_wait_for_times_out():
    async def run():
        client = RootClient()
        try:
            await client.wait_for("nothing", timeout=0.15)
        except asyncio.TimeoutError:
            return True
        finally:
            await client.close()
        return False

    assert asyncio.run(run())


def test_wait_for_does_not_leak_waiters():
    async def run():
        client = RootClient()
        try:
            await client.wait_for("nothing", timeout=0.1)
        except asyncio.TimeoutError:
            pass
        remaining = len(client._waiters)
        await client.close()
        return remaining

    assert asyncio.run(run()) == 0


# --- presence ------------------------------------------------------------- #
class _PresenceResponse:
    content = b""
    status_code = 200
    headers: dict = {}


async def _presence_calls(action):
    """Run ``action(client)`` and return every endpoint it sent."""
    client = RootClient()
    client.users._token_getter = lambda: "token"
    calls = []

    async def fake_unary(**kwargs):
        calls.append((kwargs["endpoint"], kwargs.get("body")))
        return _PresenceResponse()

    client.users.transport.unary = fake_unary
    result = await action(client)
    await client.close()
    return result, calls


def test_presence_sends_correct_status_value():
    result, calls = asyncio.run(_presence_calls(lambda c: c.go_idle()))
    ceiling = next(
        (endpoint, body) for endpoint, body in calls
        if endpoint.endswith("SetMaxOnlineStatus")
    )
    fields = {n: v for n, _w, v in iter_fields(ceiling[1])}
    assert result is UserOnlineStatus.INACTIVE
    assert fields[10] == 4


def test_presence_also_announces_the_device():
    """Setting the ceiling alone leaves the account invisible.

    Root shows ``min(ceiling, device)``, so an account whose ceiling is
    Active and which never announced a device is offline to everyone -- and
    both requests succeed while that happens. This test exists because that
    is not a hypothetical: ``set_online_status`` used to send the ceiling
    only, and a fan-out that called it appeared, correctly and silently, to
    do nothing at all.
    """
    _result, calls = asyncio.run(_presence_calls(lambda c: c.go_idle()))
    endpoints = [endpoint for endpoint, _body in calls]
    assert any(e.endswith("SetMaxOnlineStatus") for e in endpoints), endpoints
    assert any(e.endswith("SetDeviceOnlineStatus") for e in endpoints), endpoints


def test_presence_property_applies_the_minimum():
    """``client.presence`` is the rule written down, not the ceiling."""
    from rootpy.presence import effective_presence

    assert effective_presence(
        UserOnlineStatus.ACTIVE, UserOnlineStatus.INACTIVE
    ) is UserOnlineStatus.INACTIVE
    # The trap: a maximal ceiling with nothing announced is still invisible.
    assert effective_presence(
        UserOnlineStatus.ACTIVE, 0
    ) is UserOnlineStatus.DISCONNECTED
    assert effective_presence(0, 0) is UserOnlineStatus.UNSPECIFIED


def test_presence_reports_the_failure_instead_of_lying():
    """A ceiling that lands with no device must not return success.

    Returning the ceiling here would report exactly the state the caller
    asked for while the account stayed invisible, which is the failure this
    whole API exists to make impossible.
    """
    async def run():
        client = RootClient()
        client.users._token_getter = lambda: "token"

        async def fake_unary(**kwargs):
            if kwargs["endpoint"].endswith("SetMaxOnlineStatus"):
                return _PresenceResponse()
            raise RuntimeError("device announcement refused")

        client.users.transport.unary = fake_unary
        try:
            await client.set_presence("online")
            return None
        except RuntimeError as exc:
            return str(exc)
        finally:
            await client.close()

    message = asyncio.run(run())
    assert message is not None
    assert "not visible" in message


# --- attach lifetime ------------------------------------------------------ #
class _FakeCommunityService:
    def __init__(self) -> None:
        self.attached: list = []
        self.detached: list = []

    async def attach(self, community_id):
        self.attached.append(community_id)

    async def detach(self, community_id):
        self.detached.append(community_id)

    async def detach_many(self, community_ids):
        self.detached.extend(community_ids)


class _FakeAttachClient:
    def __init__(self) -> None:
        self.community_service = _FakeCommunityService()
        self.is_connected = True
        self.connects = 0

    async def connect(self):
        self.connects += 1
        self.is_connected = True


COMMUNITY = "0030e86a-3e52-8a02-8bb1-e2b4ec6a4128"


def test_attach_hold_reattaches_when_the_socket_comes_back():
    """The guarantee: a lost socket is a lost attach, and this notices.

    Without it the account keeps looking logged in, keeps its client state,
    and quietly stops being in the community -- measured gone by +48s when
    the socket was killed under it.
    """
    from rootpy import AttachHold

    async def run():
        client = _FakeAttachClient()
        hold = AttachHold(client, poll=0.05)
        await hold.add(COMMUNITY)
        after_first = len(client.community_service.attached)
        client.is_connected = False              # the socket drops
        await asyncio.sleep(0.3)
        await hold.stop()
        return after_first, client.community_service.attached, client.connects

    after_first, attached, connects = asyncio.run(run())
    assert after_first == 1, "add() should attach once, immediately"
    assert len(attached) >= 2, "a dropped socket must be re-attached"
    assert connects >= 1, "and the socket itself must be brought back"


def test_attach_hold_context_manager_detaches_on_exit():
    from rootpy import AttachHold

    async def run():
        client = _FakeAttachClient()
        hold = AttachHold(client, poll=0.05)
        await hold.add(COMMUNITY)
        await hold.remove(COMMUNITY)
        return client.community_service.detached, hold.running

    detached, still_running = asyncio.run(run())
    assert detached == [COMMUNITY]
    assert not still_running, "the last release should stop the watcher"


# --- broadcast concurrency -------------------------------------------------- #
def test_broadcast_concurrency_scales_with_the_host():
    """One flat default cannot be right for 5 accounts and for 1,300.

    8 was the old default. It is correct for a small host and costs 28 s per
    command at 1,089 accounts, and the only warning was a docstring.
    """
    from rootpy.host import (
        DEFAULT_MAX_CONCURRENCY,
        DEFAULT_MIN_CONCURRENCY,
        resolve_concurrency,
    )

    # A small host keeps the old behaviour: more permits than accounts.
    assert resolve_concurrency(None, 1) == DEFAULT_MIN_CONCURRENCY
    assert resolve_concurrency(None, 8) == DEFAULT_MIN_CONCURRENCY
    # A large host stops queueing behind a gate of 8.
    assert resolve_concurrency(None, 50) == 50
    assert resolve_concurrency(None, 1307) == DEFAULT_MAX_CONCURRENCY
    # An explicit number always wins, including a small one.
    assert resolve_concurrency(16, 1307) == 16
    assert resolve_concurrency(1, 1307) == 1


# --- retry classification -------------------------------------------------- #
def test_unavailable_can_be_made_non_retryable():
    """Root answers a non-member's message send with 14, not PERMISSION_DENIED.

    Retrying it cannot ever help -- measured as the same 7 of 24 accounts
    failing at concurrency 8 and at 64, each having burned two retries and
    two backoffs first.
    """
    from rootpy.transport import GrpcWebTransport

    default = GrpcWebTransport()
    assert default._is_retryable("any", "14", 1)

    narrowed = GrpcWebTransport(retry_statuses={"4", "8", "10", "13"})
    assert not narrowed._is_retryable("any", "14", 1)
    assert narrowed._is_retryable("any", "8", 1)

    hooked = GrpcWebTransport(
        should_retry=lambda endpoint, status, attempt: "Message" not in endpoint
    )
    assert not hooked._is_retryable("root.v2.MessageGrpcService/Create", "14", 1)
    assert hooked._is_retryable("root.UserGrpcService/GetSelf", "14", 1)


def test_online_status_enum_has_active():
    """ACTIVE = 0x10 was missed by the original generator (decimal-only regex)."""
    assert UserOnlineStatus.ACTIVE == 16
    assert UserOnlineStatus.from_name("online") is UserOnlineStatus.ACTIVE
    assert UserOnlineStatus.from_name("invisible") is UserOnlineStatus.DISCONNECTED
    assert UserOnlineStatus.from_name("idle") is UserOnlineStatus.INACTIVE


# --- object methods ------------------------------------------------------- #
def test_object_methods_exist():
    for name in ("send", "history", "mark_read", "is_text", "mention"):
        assert hasattr(Channel, name), f"Channel.{name}"
    for name in ("reply", "send_to_channel", "delete", "edit", "react", "is_dm"):
        assert hasattr(Message, name), f"Message.{name}"


def test_channel_send_uses_its_own_ids():
    async def run():
        sent = {}

        class _Messages:
            async def send(self, container_id, content, community_id=None, **k):
                sent.update(container=container_id, content=content,
                            community=community_id)
                return "ok"

        class _Client:
            messages = _Messages()

        channel = Channel(
            id="chan-1", community_id="comm-1", channel_group_id="g",
            name="general", channel_type=1, _client=_Client(),
        )
        await channel.send("hello")
        return sent

    sent = asyncio.run(run())
    assert sent == {"container": "chan-1", "content": "hello", "community": "comm-1"}


# --- rate limiting -------------------------------------------------------- #
def test_rate_limit_cooldown_is_per_account():
    """A 429 cools an endpoint down for *that account only*.

    The cooldown map lives on the transport, and a ``MultiClientHost`` shares
    one transport across accounts (it saves the ~831 ms TLS/HTTP-2 handshake
    per account). Keying the cooldown by endpoint alone meant one account's
    429 stalled every account on the pool. It is keyed by account as well now
    (see ``GrpcWebTransport._account_scope``), which is what makes a shared
    transport safe -- and is why sharing is the host default.

    So the assertion is the opposite of what it once was: A's cooldown holds
    A and does *not* hold B, even though they share the transport.
    """
    async def run():
        transport = GrpcWebTransport()

        class _Response:
            def __init__(self, code, headers=None):
                self.status_code = code
                self.headers = headers or {}
                self.content = b""
                self.text = ""

        # One 429 for the first call, then everyone succeeds.
        responses = [
            _Response(429, {"retry-after": "0.25"}),
            _Response(200, {"grpc-status": "0"}),
        ]

        class _Client:
            async def post(self, endpoint, headers=None, content=None):
                return responses.pop(0) if responses else _Response(
                    200, {"grpc-status": "0"}
                )

        transport._get_client = lambda: _Client()
        endpoint = "https://api.rootapp.com/root.v2.MessageGrpcService/List"
        headers_a = {"authorization": "Bearer AAA"}
        headers_b = {"authorization": "Bearer BBB"}
        key_a = transport._cooldown_key(endpoint, headers_a)
        key_b = transport._cooldown_key(endpoint, headers_b)

        # A's 429 records a cooldown under A's scoped key -- not the bare
        # endpoint (that would stall everyone), and not B's.
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers=headers_a, operation="T"
        )
        assert key_a in transport._cooldowns
        assert key_b not in transport._cooldowns
        assert "root.v2.MessageGrpcService/List" not in transport._cooldowns
        assert key_a != key_b

        loop = asyncio.get_running_loop()

        # A later call *as A* waits its own cooldown out rather than piling on.
        transport._cooldowns[key_a] = loop.time() + 0.3
        started = loop.time()
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers=headers_a, operation="T"
        )
        a_waited = loop.time() - started

        # ...while B, sharing the very same transport, does not inherit it.
        transport._cooldowns[key_a] = loop.time() + 0.3
        started = loop.time()
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers=headers_b, operation="T"
        )
        b_waited = loop.time() - started
        return a_waited, b_waited

    a_waited, b_waited = asyncio.run(run())
    assert a_waited >= 0.25          # A holds for its own cooldown
    assert b_waited < 0.1            # B is untouched by A's cooldown


# --- friend requests ------------------------------------------------------ #
def test_friend_request_manager_api():
    client = RootClient()
    for name in ("send", "accept", "decline", "respond", "pending"):
        assert hasattr(client.friend_requests, name), name
    for name in (
        "send_friend_request", "accept_friend_request",
        "decline_friend_request", "pending_friend_requests", "add_friend",
    ):
        assert hasattr(client, name), name


def test_friend_request_send_normalises_username():
    async def run():
        client = RootClient()
        captured = {}

        class _Invite:
            async def create(self, username):
                captured["username"] = username
                return type("R", (), {"data": "ok"})()

        client.high.friendship_invite = _Invite()
        await client.friend_requests.send("  @someuser ")
        await client.close()
        return captured["username"]

    assert asyncio.run(run()) == "someuser"


def test_friend_request_send_rejects_empty():
    async def run():
        client = RootClient()
        try:
            await client.friend_requests.send("   ")
        except ValueError:
            return True
        finally:
            await client.close()
        return False

    assert asyncio.run(run())


# --- add_listener --------------------------------------------------------- #
def test_add_listener_supports_multiple_handlers():
    async def run():
        client = RootClient()
        fired = []

        async def first(event):
            fired.append("first")

        async def second(event):
            fired.append("second")

        client.add_listener("member_join", first)
        client.add_listener("on_member_join", second)   # prefix tolerated
        await client.dispatch("member_join", {})
        await asyncio.sleep(0.05)

        client.remove_listener("member_join", first)
        fired.clear()
        await client.dispatch("member_join", {})
        await asyncio.sleep(0.05)
        await client.close()
        return fired

    assert asyncio.run(run()) == ["second"]


def test_documented_api_surface_exists():
    """Everything the README documents must actually exist.

    (This caught pin_list going missing during a refactor.)
    """
    from rootpy.models import Channel, Message

    client = RootClient()
    missing = []

    def check(label, ok):
        if not ok:
            missing.append(label)

    check("messages.*", all(hasattr(client.messages, n) for n in
          ("send", "list", "history", "set_view_time", "pin_list")))
    check("Channel.*", all(hasattr(Channel, n) for n in
          ("send", "history", "history_iter", "mark_read", "is_text", "mention")))
    check("Message.*", all(hasattr(Message, n) for n in
          ("reply", "edit", "react", "unreact", "pin", "unpin", "delete", "is_dm")))
    check("community_admin.*", all(hasattr(client.community_admin, n) for n in
          ("create_channel", "create_role", "delete_channel", "delete_role")))
    check("roles.*", all(hasattr(client.roles, n) for n in
          ("add_to_members", "remove_from_members")))
    check("friend_requests.*", all(hasattr(client.friend_requests, n) for n in
          ("send", "accept", "decline", "pending")))
    check("presence", all(hasattr(client, n) for n in
          ("go_online", "go_idle", "go_invisible", "set_online_status", "update_status")))
    check("events", all(hasattr(client, n) for n in
          ("event", "add_listener", "remove_listener", "add_message_listener",
           "wait_for", "watch_unread", "watch_channel", "unwatch_all")))
    check("cache", all(hasattr(client.cache, n) for n in
          ("username", "member", "stats")))
    check("send_once", hasattr(RootClient, "send_once"))
    check("discovery", all(hasattr(RootClient, n) for n in
          ("explain", "preview", "describe_services")))

    assert not missing, f"README documents missing APIs: {missing}"


# --- import cost ---------------------------------------------------------- #
def test_heavy_registries_are_not_imported_eagerly():
    """The generated registries are ~1.2 MB of source and cost ~400ms to
    import cold. Most programs never touch them, so importing rootpy (and
    even constructing a client) must not pull them in."""
    import subprocess
    import sys

    code = (
        "import sys, rootpy;"
        "c = rootpy.RootClient(token='x');"
        "heavy = [m for m in sys.modules if 'registry' in m or 'websockets' in m];"
        "print(','.join(sorted(heavy)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
    )
    loaded = [m for m in result.stdout.strip().split(",") if m]
    assert not loaded, f"eagerly imported heavy modules: {loaded}"


def test_lazy_apis_still_resolve():
    client = RootClient(token="x")
    assert client.high is not None
    assert client.raw is not None
    assert client.services is not None
    assert client.friendship_invite_api is not None
    # The alias must be a STABLE object across accesses, as it was when built
    # eagerly in __init__. (StructuredAPI itself mints a new service object on
    # every attribute access, so comparing against client.high would fail --
    # that's pre-existing behaviour, not what this guards.)
    assert client.friendship_invite_api is client.friendship_invite_api


def test_unknown_attribute_still_raises():
    client = RootClient(token="x")
    try:
        client.definitely_not_a_real_attribute
    except AttributeError:
        return
    raise AssertionError("expected AttributeError")


# --- multi-account host --------------------------------------------------- #
def test_host_runs_accounts_independently():
    """Each account gets its own client and handlers, and a bad token does not
    take down the others.

    The transport is *shared* by default now: one TLS/HTTP-2 handshake for the
    whole host rather than one per account (~831 ms each, measured). That used
    to cost rate-limit isolation, because the cooldown table rode along with
    the pool -- so the old default was a transport per account. It no longer
    does (cooldowns are keyed per account; see ``test_rate_limit_cooldown_is_
    per_account``), which makes sharing safe. ``shared_transport=False`` still
    gives each account its own pool for callers who want hard socket isolation.
    """
    async def run(host_kwargs):
        import rootpy
        from rootpy import MultiClientHost
        from rootpy.models import CurrentUser

        original_login = rootpy.RootClient.login_token
        original_connect = rootpy.RootClient.connect

        async def fake_login(self):
            await asyncio.sleep(0.01)
            if self.token == "bad":
                raise RuntimeError("invalid token")
            self.user = CurrentUser(id="me", username="ok")

        async def fake_connect(self):
            await asyncio.sleep(0.01)

        rootpy.RootClient.login_token = fake_login
        rootpy.RootClient.connect = fake_connect
        try:
            host = MultiClientHost(stagger=0.0, **host_kwargs)
            host.add("good1", "t1")
            host.add("broken", "bad")
            host.add("good2", "t2")
            await host.start()
            ready = await host.wait_ready(timeout=5)
            clients = [a.client for a in host.accounts.values() if a.client]
            transports = {id(c.transport) for c in clients}
            client_ids = {id(c) for c in clients}
            await host.stop()
            return ready, len(transports), len(client_ids)
        finally:
            rootpy.RootClient.login_token = original_login
            rootpy.RootClient.connect = original_connect

    # default (shared_transport unspecified): one shared transport, but three
    # independent clients. The default itself is the contract under test.
    ready, transport_count, client_count = asyncio.run(run({}))
    assert ready["good1"] and ready["good2"]     # a bad token must not ...
    assert not ready["broken"]                   # ... take down the others
    assert client_count == 3                     # each account its own client
    assert transport_count == 1                  # sharing one pool by default

    # opt-out: hard socket isolation, one transport per account
    ready, transport_count, client_count = asyncio.run(
        run({"shared_transport": False})
    )
    assert ready["good1"] and ready["good2"]
    assert not ready["broken"]
    assert transport_count == 3


def test_host_rejects_duplicate_names():
    from rootpy import MultiClientHost

    host = MultiClientHost()
    host.add("alice", "t1")
    try:
        host.add("alice", "t2")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a duplicate account name")


def test_broadcast_returns_an_outcome_per_account_and_never_raises():
    """A fan-out's partial success is a set of results, not an exception.

    Every requested account gets exactly one Outcome: a value on success, and a
    captured error -- never a raise -- whether the account failed to log in or
    the action itself threw. ``only=`` restricts the set.
    """
    async def run():
        import rootpy
        from rootpy import MultiClientHost
        from rootpy.models import CurrentUser

        original_login = rootpy.RootClient.login_token
        original_connect = rootpy.RootClient.connect

        async def fake_login(self):
            await asyncio.sleep(0.01)
            if self.token == "bad":
                raise RuntimeError("invalid token")
            self.user = CurrentUser(id="me", username=self.token)

        async def fake_connect(self):
            await asyncio.sleep(0.01)

        rootpy.RootClient.login_token = fake_login
        rootpy.RootClient.connect = fake_connect
        try:
            host = MultiClientHost(stagger=0.0)
            host.add("good1", "t1", gateway=False)
            host.add("broken", "bad", gateway=False)
            host.add("good2", "t2", gateway=False)
            await host.start()
            await host.wait_ready(timeout=5)

            async def action(client):
                if client.user.username == "t2":
                    raise ValueError("boom")     # captured on the outcome
                return client.user.username

            results = await host.broadcast(action)
            only = await host.broadcast(action, only=["good1"])
            await host.stop()
            return results, only
        finally:
            rootpy.RootClient.login_token = original_login
            rootpy.RootClient.connect = original_connect

    results, only = asyncio.run(run())
    # one outcome per requested account; the call itself never raised
    assert set(results) == {"good1", "broken", "good2"}
    assert results["good1"].ok and results["good1"].value == "t1"
    # an account that never became ready is a failed outcome, not a silent skip
    assert not results["broken"].ok
    # an exception raised inside the action lands on that account's outcome
    assert not results["good2"].ok
    assert isinstance(results["good2"].error, ValueError)
    # only= restricts the fan-out to the named accounts
    assert set(only) == {"good1"} and only["good1"].value == "t1"


# --- profile picture ------------------------------------------------------ #
def _client_with_fake_assets():
    client = RootClient(token="x")
    uploaded = []

    class _Assets:
        async def upload_file(self, source):
            uploaded.append(("file", str(source)))
            return "asset://from-file"

        async def upload_bytes(self, data, filename="upload.png", modified=None):
            uploaded.append(("bytes", filename, len(data)))
            return "asset://from-bytes"

    class _Response:
        content = b""

    async def fake_unary(**kwargs):
        return _Response()

    client.users.asset_service = _Assets()
    client.users.transport.unary = fake_unary
    return client, uploaded


def test_change_profile_picture_accepts_bytes():
    async def run():
        client, uploaded = _client_with_fake_assets()
        await client.change_profile_picture(b"\x89PNG data")
        await client.close()
        return uploaded

    uploaded = asyncio.run(run())
    assert uploaded[0][0] == "bytes"


def test_change_profile_picture_accepts_file_object():
    async def run():
        import io

        client, uploaded = _client_with_fake_assets()
        handle = io.BytesIO(b"\x89PNG data")
        handle.name = "avatar.png"
        await client.change_profile_picture(handle)
        await client.close()
        return uploaded

    uploaded = asyncio.run(run())
    assert uploaded[0][0] == "bytes"
    assert uploaded[0][1] == "avatar.png"


def test_change_profile_picture_accepts_path(tmp_path):
    async def run():
        client, uploaded = _client_with_fake_assets()
        image = tmp_path / "pic.png"
        image.write_bytes(b"\x89PNG data")
        await client.change_profile_picture(image)
        await client.close()
        return uploaded

    uploaded = asyncio.run(run())
    assert uploaded[0][0] == "file"


def test_change_profile_picture_passes_through_asset_uri():
    async def run():
        client, uploaded = _client_with_fake_assets()
        await client.change_profile_picture("asset://already-there")
        await client.close()
        return uploaded

    assert asyncio.run(run()) == []      # nothing re-uploaded


def test_text_mode_file_object_is_rejected_clearly():
    async def run():
        import io

        client, _uploaded = _client_with_fake_assets()
        try:
            await client.change_profile_picture(io.StringIO("not binary"))
            return None
        except TypeError as exc:
            return str(exc)
        finally:
            await client.close()

    message = asyncio.run(run())
    assert message and "binary" in message


# --- server members ------------------------------------------------------- #
def _client_with_members():
    from rootpy.models import (
        Channel, ChannelGroup, Community, CommunityExtended, CommunityMember,
    )

    community_id = "00000000-0000-00aa-0000-000000000002"
    user_a = "00000000-0000-00b1-0000-000000000001"
    user_b = "00000000-0000-00b2-0000-000000000002"
    role_id = "00000000-0000-00c1-0000-000000000003"
    client = RootClient()
    fetches = {"n": 0}

    async def fetch(cid):
        fetches["n"] += 1
        community = Community(
            id=cid, name="S", owner_user_id=user_a, default_channel_id=None,
        )
        members = (
            CommunityMember(user_a, (role_id,), b"", cid, client),
            CommunityMember(user_b, (), b"", cid, client),
        )
        text = Channel(id="ch1", community_id=cid, channel_group_id="g",
                       name="general", channel_type=1)
        voice = Channel(id="ch2", community_id=cid, channel_group_id="g",
                        name="Voice", channel_type=4)
        group = ChannelGroup("g", cid, "G", 0.0, text.permissions, (),
                             (text, voice), b"")
        extended = CommunityExtended(community, (group,), members, (), b"")
        client._community_extended[cid] = extended
        return extended

    client.fetch_community = fetch
    return client, community_id, user_a, user_b, role_id, fetches


def test_get_members_returns_everyone():
    async def run():
        client, cid, *_rest, fetches = _client_with_members()
        members = await client.get_members(cid)
        count = await client.member_count(cid)
        await client.close()
        return members, count, fetches["n"]

    members, count, fetches = asyncio.run(run())
    assert len(members) == 2
    assert count == 2
    assert fetches == 1          # second call served from cache


def test_get_member_and_role_filter():
    async def run():
        client, cid, user_a, user_b, role_id, _f = _client_with_members()
        found = await client.get_member(cid, user_b)
        missing = await client.get_member(cid, "00000000-0000-0000-0000-000000009999")
        with_role = await client.members_with_role(cid, role_id)
        await client.close()
        return found, missing, with_role, user_a, user_b

    found, missing, with_role, user_a, user_b = asyncio.run(run())
    assert found.user_id == user_b
    assert missing is None
    assert [m.user_id for m in with_role] == [user_a]


def test_member_names_uses_cache_without_fetching():
    async def run():
        client, cid, user_a, user_b, _r, _f = _client_with_members()
        client.cache.remember_user(user_a, "alice")
        names = await client.member_names(cid)
        await client.close()
        return names, user_a, user_b

    names, user_a, user_b = asyncio.run(run())
    assert names[user_a] == "alice"
    assert names[user_b] is None      # unknown, and no request made for it


def test_community_extended_helpers():
    async def run():
        client, cid, user_a, *_rest = _client_with_members()
        extended = await client.community_detail(cid)
        await client.close()
        return extended, user_a

    extended, user_a = asyncio.run(run())
    assert [c.name for c in extended.channels] == ["general", "Voice"]
    assert [c.name for c in extended.text_channels] == ["general"]
    assert extended.member(user_a).user_id == user_a


# --- user profiles -------------------------------------------------------- #
def _profile_bytes(user_id_bytes, username, about, pfp, banner):
    import struct

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

    def wrapped(field, value):
        return msg(field, text(1, value))

    return msg(
        10,
        msg(10, user_id_bytes) + text(12, username) + wrapped(16, about)
        + wrapped(11, pfp) + wrapped(17, banner) + tag(13, 0) + varint(16),
    )


def test_get_profile_parses_all_fields():
    async def run():
        import struct

        from rootpy.protocol import grpc_frame

        client = RootClient(token="x")
        client.users._token_getter = lambda: "x"
        user_id = "00000000-0000-0011-0000-000000000022"
        raw_id = (
            b"\x09" + struct.pack("<Q", 0x0000000000000011)
            + b"\x11" + struct.pack("<Q", 0x0000000000000022)
        )
        body = grpc_frame(
            _profile_bytes(raw_id, "alice", "about me", "asset://p", "asset://b")
        )

        class _Response:
            content = body

        async def fake_unary(**kwargs):
            return _Response()

        client.users.transport.unary = fake_unary
        profile = await client.get_profile(user_id)
        cached = client.cache.username(profile.user_id)
        await client.close()
        return profile, cached

    profile, cached = asyncio.run(run())
    assert profile.username == "alice"
    assert profile.about_me == "about me"
    assert profile.avatar_url == "asset://p"
    assert profile.banner_uri == "asset://b"
    assert profile.online_status == 16
    assert cached == "alice"          # profile fetches feed the cache


def test_members_detailed_batches_requests():
    """250 members must cost a handful of requests, not 250."""
    async def run():
        from rootpy.models import Community, CommunityExtended, CommunityMember

        community_id = "00000000-0000-00aa-0000-000000000002"

        def uid(index):
            return f"00000000-0000-{index:04x}-0000-000000000001"

        client = RootClient(token="x")
        client.users._token_getter = lambda: "x"
        members = tuple(
            CommunityMember(uid(i), (), b"", community_id, client)
            for i in range(250)
        )

        async def fetch(cid):
            extended = CommunityExtended(
                Community(id=cid, name="S", owner_user_id=uid(0),
                          default_channel_id=None),
                (), members, (), b"",
            )
            client._community_extended[cid] = extended
            return extended

        calls = {"n": 0}

        class _Response:
            content = b""

        async def fake_unary(**kwargs):
            calls["n"] += 1
            return _Response()

        client.fetch_community = fetch
        client.users.transport.unary = fake_unary
        detailed = await client.get_members_detailed(community_id, batch_size=100)
        await client.close()
        return len(detailed), calls["n"]

    count, requests = asyncio.run(run())
    assert count == 250
    assert requests == 3          # ceil(250/100), not 250


def _detailed_client(member_count, *, on_request=None):
    """A client whose community has ``member_count`` members and a fake wire."""
    from rootpy.models import Community, CommunityExtended, CommunityMember

    community_id = "00000000-0000-00aa-0000-000000000002"

    def uid(index):
        return f"00000000-0000-{index:04x}-0000-000000000001"

    client = RootClient(token="x")
    client.users._token_getter = lambda: "x"
    members = tuple(
        CommunityMember(uid(i), (), b"", community_id, client)
        for i in range(member_count)
    )

    async def fetch(cid):
        extended = CommunityExtended(
            Community(id=cid, name="S", owner_user_id=uid(0),
                      default_channel_id=None),
            (), members, (), b"",
        )
        client._community_extended[cid] = extended
        return extended

    class _Response:
        content = b""

    async def fake_unary(**kwargs):
        if on_request is not None:
            await on_request()
        return _Response()

    client.fetch_community = fetch
    client.users.transport.unary = fake_unary
    return client, community_id


def test_members_detailed_default_batch_is_500_not_100():
    """The default was measured, so pin it -- 100 cost 316 requests live.

    A round trip costs ~190 ms whether it carries 1 id or 200, and Root
    accepted 4,000 in one call, so a small batch buys nothing and costs a
    request each time. 2,000 members should be 4 requests, not 20.
    """
    async def run():
        client, cid = _detailed_client(2000)
        calls = {"n": 0}

        original = client.users.transport.unary

        async def counting(**kwargs):
            calls["n"] += 1
            return await original(**kwargs)

        client.users.transport.unary = counting
        detailed = await client.get_members_detailed(cid)
        await client.close()
        return len(detailed), calls["n"]

    count, requests = asyncio.run(run())
    assert count == 2000
    assert requests == 4, f"expected ceil(2000/500)=4 requests, got {requests}"


def test_members_detailed_runs_its_batches_concurrently():
    """The expensive half was the ``for`` loop, not the batch size.

    Serial, eight 50 ms batches take 400 ms; concurrent they take ~50 ms.
    Measured against a fake wire so this cannot fail because Root was busy --
    what is under test is whether the batches are issued together at all.
    """
    async def run():
        client, cid = _detailed_client(4000)   # 8 batches at the 500 default

        in_flight = {"now": 0, "peak": 0}

        async def slow():
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await asyncio.sleep(0.05)
            in_flight["now"] -= 1

        client.users.transport.unary = _replacing_unary(client, slow)

        started = time.perf_counter()
        detailed = await client.get_members_detailed(cid)
        elapsed = time.perf_counter() - started
        await client.close()
        return len(detailed), elapsed, in_flight["peak"]

    count, elapsed, peak = asyncio.run(run())
    assert count == 4000
    assert peak > 1, (
        "profile batches were issued one at a time -- peak in-flight was "
        f"{peak}, so the gather is not gathering"
    )
    assert elapsed < 0.3, (
        f"8 batches of 50 ms took {elapsed*1000:.0f} ms; serial would be ~400 ms"
    )


def test_members_detailed_concurrency_one_is_still_serial():
    """The escape hatch: ``concurrency=1`` restores the old behaviour."""
    async def run():
        client, cid = _detailed_client(4000)
        in_flight = {"now": 0, "peak": 0}

        async def slow():
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1

        client.users.transport.unary = _replacing_unary(client, slow)
        await client.get_members_detailed(cid, concurrency=1)
        await client.close()
        return in_flight["peak"]

    assert asyncio.run(run()) == 1


def test_members_detailed_survives_a_failed_batch():
    """One bad batch costs its own members' profiles, not everyone else's."""
    async def run():
        client, cid = _detailed_client(1500)   # 3 batches at the 500 default
        seen = {"n": 0}

        class _Response:
            content = b""

        async def flaky(**kwargs):
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("batch 2 is having a bad day")
            return _Response()

        client.users.transport.unary = flaky
        detailed = await client.get_members_detailed(cid)
        await client.close()
        return len(detailed), seen["n"]

    count, requests = asyncio.run(run())
    # every member still comes back -- the failed batch's members just carry
    # profile=None, which DetailedMember already degrades to.
    assert count == 1500
    assert requests == 3, f"a failed batch stopped the others: {requests}"


def _replacing_unary(client, hook):
    """A fake ``transport.unary`` that runs ``hook()`` per request."""
    class _Response:
        content = b""

    async def fake(**kwargs):
        await hook()
        return _Response()

    return fake


def test_detailed_member_exposes_profile_fields():
    from rootpy.models import CommunityMember, DetailedMember, UserProfile

    member = CommunityMember("u1", ("r1",), b"", "c1", None)
    profile = UserProfile(
        user_id="u1", username="alice", profile_picture_uri="asset://p",
        banner_uri="asset://b", description="hello",
    )
    detailed = DetailedMember(member=member, profile=profile)
    assert detailed.user_id == "u1"
    assert detailed.username == "alice"
    assert detailed.about_me == "hello"
    assert detailed.avatar_url == "asset://p"
    assert detailed.banner_uri == "asset://b"
    assert detailed.role_ids == ("r1",)
    assert str(detailed) == "alice"

    # a member whose profile couldn't be fetched degrades to None, not an error
    bare = DetailedMember(member=member, profile=None)
    assert bare.username is None
    assert bare.about_me is None
    assert bare.user_id == "u1"


# --- random member -------------------------------------------------------- #
def _client_with_n_members(count=8):
    from rootpy.models import Community, CommunityExtended, CommunityMember

    community_id = "00000000-0000-00aa-0000-000000000002"

    def uid(index):
        return f"00000000-0000-{index:04x}-0000-000000000001"

    client = RootClient()
    client.user_id = uid(0)          # we are member 0
    members = tuple(
        CommunityMember(uid(i), (), b"", community_id, client)
        for i in range(count)
    )

    async def fetch(cid):
        extended = CommunityExtended(
            Community(id=cid, name="S", owner_user_id=uid(0),
                      default_channel_id=None),
            (), members, (), b"",
        )
        client._community_extended[cid] = extended
        return extended

    client.fetch_community = fetch
    return client, community_id, uid


def test_get_random_member_excludes_self_and_varies():
    async def run():
        client, cid, uid = _client_with_n_members()
        picks = set()
        for _ in range(60):
            member = await client.get_random_member(cid, with_profile=False)
            picks.add(member.user_id)
        await client.close()
        return picks, uid(0)

    picks, me = asyncio.run(run())
    assert me not in picks, "should not pick yourself by default"
    assert len(picks) > 1, "should not always return the same member"


def test_get_random_member_can_include_self():
    async def run():
        client, cid, uid = _client_with_n_members(count=1)   # only us
        member = await client.get_random_member(
            cid, exclude_self=False, with_profile=False
        )
        await client.close()
        return member, uid(0)

    member, me = asyncio.run(run())
    assert member is not None and member.user_id == me


def test_get_random_member_returns_none_when_empty():
    async def run():
        from rootpy.models import Community, CommunityExtended

        client = RootClient()
        cid = "00000000-0000-00aa-0000-000000000002"

        async def fetch(community_id):
            extended = CommunityExtended(
                Community(id=community_id, name="S", owner_user_id="x",
                          default_channel_id=None),
                (), (), (), b"",
            )
            client._community_extended[community_id] = extended
            return extended

        client.fetch_community = fetch
        member = await client.get_random_member(cid, with_profile=False)
        await client.close()
        return member

    assert asyncio.run(run()) is None


def test_get_random_members_is_distinct_and_capped():
    async def run():
        client, cid, _uid = _client_with_n_members(count=5)
        three = await client.get_random_members(cid, 3, with_profile=False)
        too_many = await client.get_random_members(cid, 99, with_profile=False)
        await client.close()
        return three, too_many

    three, too_many = asyncio.run(run())
    assert len(three) == 3
    assert len({m.user_id for m in three}) == 3        # distinct
    assert len(too_many) == 4                          # 5 members minus self


def test_random_member_includes_profile_by_default():
    """A bare user id isn't much use -- the pick should carry a username."""
    async def run():
        from rootpy.models import UserProfile

        client, cid, uid = _client_with_n_members()

        async def fake_profile(user_id):
            return UserProfile(
                user_id=user_id, username="alice",
                profile_picture_uri="asset://pfp", banner_uri="asset://ban",
                description="about me",
            )

        client.get_profile = fake_profile
        member = await client.get_random_member(cid)
        await client.close()
        return member

    member = asyncio.run(run())
    assert member.username == "alice"
    assert member.avatar_url == "asset://pfp"
    assert member.banner_uri == "asset://ban"
    assert member.about_me == "about me"
    assert member.user_id                     # still has the id
    assert hasattr(member, "add_role")        # and the member actions


def test_random_member_survives_profile_failure():
    """A failed profile fetch must still return the member, not None."""
    async def run():
        client, cid, uid = _client_with_n_members()

        async def boom(user_id):
            raise RuntimeError("profile service down")

        client.get_profile = boom
        member = await client.get_random_member(cid)
        await client.close()
        return member

    member = asyncio.run(run())
    assert member is not None
    assert member.user_id
    assert member.username is None


# --- request timings ------------------------------------------------------ #
def test_timings_separate_waiting_from_roundtrip():
    """Rate-limit holds are our own doing and must not be counted as server time."""
    async def run():
        from rootpy.transport import GrpcWebTransport

        transport = GrpcWebTransport()

        class _Response:
            def __init__(self, code=200, headers=None):
                self.status_code = code
                self.headers = headers or {"grpc-status": "0"}
                self.content = b""
                self.text = ""

        responses = [_Response(429, {"retry-after": "0.2"}), _Response()]

        class _Client:
            async def post(self, endpoint, headers=None, content=None):
                await asyncio.sleep(0.05)
                return responses.pop(0) if responses else _Response()

        transport._get_client = lambda: _Client()
        endpoint = "https://api.rootapp.com/root.v2.MessageGrpcService/List"
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers={}, operation="List"
        )
        return transport.stats.totals()

    totals = asyncio.run(run())
    assert totals["calls"] == 1
    assert totals["retries"] == 1
    assert totals["rate_limited"] == 1
    assert totals["waiting_ms"] >= 190        # the retry-after we honoured
    assert totals["roundtrip_ms"] >= 90       # two 50ms posts


def test_timing_report_is_readable_and_resettable():
    async def run():
        client = RootClient(token="x")

        class _Response:
            status_code = 200
            headers = {"grpc-status": "0"}
            content = b""
            text = ""

        class _Client:
            async def post(self, endpoint, headers=None, content=None):
                return _Response()

        client.transport._get_client = lambda: _Client()
        await client.transport.unary(
            endpoint="https://api.rootapp.com/root.UserGrpcService/GetSelf",
            body=b"\x08\x01", headers={}, operation="GetSelf",
        )
        report = client.timing_report()
        before = client.timings()["totals"]["calls"]
        client.reset_timings()
        after = client.timings()["totals"]["calls"]
        await client.close()
        return report, before, after

    report, before, after = asyncio.run(run())
    assert "GetSelf" in report
    assert before == 1
    assert after == 0


def test_no_undefined_lazy_names():
    """Making the heavy APIs lazy removed their top-level imports -- every
    remaining use must import them locally, or it's a NameError at runtime."""
    import ast
    import pathlib

    lazy_names = {"StructuredAPI", "RawAPI", "StructuredService"}
    problems = []

    for path in pathlib.Path("rootpy").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # Names available module-wide: imports AND classes defined here.
        module_level = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    module_level.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ClassDef):
                module_level.add(node.name)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            used = {
                n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            } & lazy_names
            if not used:
                continue
            local = set()
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    for alias in inner.names:
                        local.add(alias.asname or alias.name)
            missing = used - local - module_level
            if missing:
                problems.append(f"{path}:{node.name} uses {sorted(missing)}")

    assert not problems, "undefined lazy names: " + "; ".join(problems)


def test_random_members_plural_matches_singular():
    """Both return the same shape -- a bare CommunityMember from the plural
    version was an easy trap (AttributeError on .username)."""
    async def run():
        from rootpy.models import UserProfile

        client, cid, _uid = _client_with_n_members(count=10)
        calls = {"n": 0}

        async def fake_profiles(ids):
            calls["n"] += 1
            return {
                user_id: UserProfile(
                    user_id=user_id, username="alice",
                    profile_picture_uri="asset://p", banner_uri="asset://b",
                    description="about",
                )
                for user_id in ids
            }

        async def fake_profile(user_id):
            return (await fake_profiles([user_id]))[user_id]

        client.get_profiles = fake_profiles
        client.get_profile = fake_profile

        one = await client.get_random_member(cid)
        many = await client.get_random_members(cid, 5)
        batched = calls["n"]
        await client.close()
        return one, many, batched

    one, many, batched = asyncio.run(run())
    # the same attributes work on both
    for member in (one, *many):
        assert member.user_id
        assert member.username == "alice"
        assert member.avatar_url == "asset://p"
        assert member.banner_uri == "asset://b"
        assert member.about_me == "about"
    assert len(many) == 5
    # five members cost one batched profile request, not five
    assert batched == 2          # one for the singular call, one for the batch


# --- asset URLs ----------------------------------------------------------- #
def test_asset_uris_resolve_to_real_urls():
    """Decoded per AssetInformationReflection: AssetInformation{1 url,
    2 image{10 asset_links{10 url, 11 largest_dimension_limit, 12 w, 13 h}}}."""
    async def run():
        from rootpy.protocol import grpc_frame

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

        def num(field, value):
            return tag(field, 0) + varint(value)

        def link(url, limit, width, height):
            return msg(
                10,
                text(10, url) + num(11, limit) + num(12, width) + num(13, height),
            )

        uri = "root://asset/ADCzZmV6ixu9hCMumghlAAoFaW1hZ2U"
        image = (
            link("https://cdn.root/small.png", 128, 128, 128)
            + link("https://cdn.root/big.png", 1024, 1024, 1024)
            + num(13, 1)                       # is_animated
        )
        info = (
            text(1, "https://cdn.root/original.png")
            + msg(2, image)
            + msg(10, num(1, 1786900000))      # link_expires_at
        )
        body = grpc_frame(msg(2, text(1, uri) + msg(2, info)))

        client = RootClient(token="x")
        client.assets._token_getter = lambda: "x"

        class _Response:
            content = body

        async def fake_unary(**kwargs):
            return _Response()

        client.assets.transport.unary = fake_unary
        asset = await client.get_asset(uri)
        urls = await client.asset_urls([uri])
        passthrough = await client.asset_urls(["https://already/x.png"])
        await client.close()
        return asset, urls, passthrough, uri

    asset, urls, passthrough, uri = asyncio.run(run())
    assert asset.url == "https://cdn.root/original.png"
    assert [link.max_dimension for link in asset.links] == [128, 1024]
    assert asset.best_url == "https://cdn.root/big.png"
    assert asset.url_for_size(100) == "https://cdn.root/small.png"
    assert asset.url_for_size(900) == "https://cdn.root/big.png"
    assert asset.is_animated is True
    assert asset.expires_at == 1786900000
    assert asset.width == 1024 and asset.height == 1024
    assert urls[uri] == "https://cdn.root/big.png"
    # already-usable URLs come back untouched, without a request
    assert passthrough["https://already/x.png"] == "https://already/x.png"


def test_asset_resolve_handles_empty_input():
    async def run():
        client = RootClient(token="x")
        result = await client.asset_urls([])
        await client.close()
        return result

    assert asyncio.run(run()) == {}


def test_asset_decode_matches_live_response_shape():
    """Built from a real AssetGet dump: the response carries BOTH the legacy
    map (field 1, AssetImage) and the assets map (field 2, AssetInformation).
    Mishandling the legacy entry once made the whole call return nothing."""
    async def run():
        from rootpy.protocol import grpc_frame

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

        def num(field, value):
            return tag(field, 0) + varint(value)

        uri = "root://asset/ADCzZmV6ixu9hCMumghlAAoFaW1hZ2U"
        base = "https://imagedelivery.net/o8EZ/350c2ec6/"

        def link(name, limit, width, height):
            return msg(
                10,
                text(10, base + name) + num(11, limit)
                + num(12, width) + num(13, height),
            )

        image = (
            link("placeholder", 32, 32, 32)
            + link("public", 2048, 256, 256)
            + link("small", 512, 256, 256)
            + link("thumbnail", 128, 128, 128)
            + msg(11, b"\x00" * 20)                 # web_p blob
            + msg(12, num(1, 1) + num(2, 1))        # aspect ratio
        )
        information = (
            msg(2, image)
            + msg(10, num(1, 1789084447) + num(2, 845779500))   # expiry
        )
        payload = (
            msg(1, text(1, uri) + msg(2, image))            # legacy map
            + msg(2, text(1, uri) + msg(2, information))    # assets map
        )

        client = RootClient(token="x")
        client.assets._token_getter = lambda: "x"

        class _Response:
            content = grpc_frame(payload)

        async def fake_unary(**kwargs):
            return _Response()

        client.assets.transport.unary = fake_unary
        asset = await client.get_asset(uri)
        url = await client.asset_url(uri)
        await client.close()
        return asset, url

    asset, url = asyncio.run(run())
    assert asset is not None, "legacy map entry must not break decoding"
    assert [link.max_dimension for link in asset.links] == [32, 128, 512, 2048]
    assert asset.best_url.endswith("public")
    assert asset.url_for_size(100).endswith("thumbnail")
    assert asset.expires_at and asset.expires_at > 1_700_000_000
    assert url.endswith("public")


# --- account factory ------------------------------------------------------ #
def test_account_factory_lets_the_server_demand_the_captcha():
    """Don't guess locally: attempt signup and let Root raise
    TurnstileRequired with its real challenge URL, which is the URL the user
    actually needs to open."""
    async def run():
        import rootpy
        from rootpy import AccountFactory
        from rootpy.exceptions import TurnstileRequired

        original = rootpy.RootClient.create_account
        seen = []

        async def fake(cls, *, username, password, email, access_token=None, **kw):
            seen.append(access_token)
            if not access_token:
                raise TurnstileRequired(
                    "https://challenges.cloudflare.com/turnstile/v0/REAL", "signup"
                )
            raise AssertionError("should not get here in this test")

        rootpy.RootClient.create_account = classmethod(fake)
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@example.com")
            try:
                await factory.create()          # no token at all
            except TurnstileRequired as exc:
                return exc.challenge_url, seen
            return None, seen
        finally:
            rootpy.RootClient.create_account = original

    challenge_url, seen = asyncio.run(run())
    assert challenge_url == "https://challenges.cloudflare.com/turnstile/v0/REAL"
    assert seen == [None]        # the attempt really went out without a token


def test_account_factory_uses_the_configured_domain():
    async def run():
        from types import SimpleNamespace

        import rootpy
        from rootpy import AccountFactory

        seen = []

        class _Client:
            user = SimpleNamespace(id="uid")
            user_id = "uid"
            session = SimpleNamespace(token="tok")

            async def close(self):
                pass

        original = rootpy.RootClient.create_account

        async def fake(cls, *, username, password, email, access_token=None,
                       turnstile_token=None, **kw):
            seen.append((username, email, turnstile_token))
            return _Client()

        rootpy.RootClient.create_account = classmethod(fake)
        try:
            factory = AccountFactory(
                email_pattern="hello-{tag}@solluw.com", username_prefix="test",
            )
            account = await factory.create(turnstile_token="cf-token")
            return account, seen, factory
        finally:
            rootpy.RootClient.create_account = original

    account, seen, factory = asyncio.run(run())
    assert account.email.startswith("hello-")
    assert account.email.endswith("@solluw.com")
    assert account.username.startswith("test")
    assert seen[0][2] == "cf-token"          # the token is actually sent
    assert factory.emails() == [account.email]


def test_account_factory_rejects_a_pattern_without_tag():
    from rootpy import AccountFactory

    try:
        AccountFactory(email_pattern="hello@solluw.com")
    except ValueError:
        return
    raise AssertionError("expected ValueError for a pattern without {tag}")


def test_saved_accounts_can_be_redacted(tmp_path):
    from rootpy.accounts import AccountFactory, CreatedAccount

    factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
    factory.accounts.append(
        CreatedAccount(
            username="test1", password="hunter2",
            email="hello-abc@solluw.com", token="secret-token",
        )
    )
    public = tmp_path / "accounts.public.json"
    factory.save(public, include_secrets=False)
    text = public.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "secret-token" not in text
    assert "hello-abc@solluw.com" in text


def test_account_verification_flow():
    """Create -> send code -> poll your mailbox -> verify."""
    async def run():
        from types import SimpleNamespace

        import rootpy
        from rootpy import AccountFactory

        calls = {"sent": [], "verified": []}

        class _Client:
            user = SimpleNamespace(id="uid")
            user_id = "uid"
            session = SimpleNamespace(token="tok-abc")

            async def close(self):
                pass

        originals = (
            rootpy.RootClient.create_account,
            rootpy.RootClient.send_email_verification,
            rootpy.RootClient.verify_email,
        )

        async def fake_create(cls, *, username, password, email,
                              access_token=None, **kw):
            return _Client()

        async def fake_send(cls, username=None, password=None, *,
                            token=None, turnstile_token=None):
            calls["sent"].append(token)

        async def fake_verify(cls, code, username=None, password=None, *,
                              token=None):
            calls["verified"].append(code)

        rootpy.RootClient.create_account = classmethod(fake_create)
        rootpy.RootClient.send_email_verification = classmethod(fake_send)
        rootpy.RootClient.verify_email = classmethod(fake_verify)
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
            attempts = {"n": 0}

            async def code_provider(email):
                attempts["n"] += 1
                return "123456" if attempts["n"] >= 2 else None

            account = await factory.create_and_verify(
                turnstile_token="cf-token", code_provider=code_provider,
                timeout=5, poll_interval=0.02,
            )
            return account, calls, factory
        finally:
            (rootpy.RootClient.create_account,
             rootpy.RootClient.send_email_verification,
             rootpy.RootClient.verify_email) = originals

    account, calls, factory = asyncio.run(run())
    assert account.verified is True
    # No resend by default. Signup already emails the code, and the resend is
    # gated behind its own Turnstile challenge (action=resend_verification)
    # that the signup token does not satisfy -- so doing it unasked raised
    # TurnstileRequired and lost the freshly minted credentials. This assertion
    # used to read ["tok-abc"], which was the defect written down as an
    # expectation. Opting in is covered by the test below.
    assert calls["sent"] == []
    assert calls["verified"] == ["123456"]
    assert factory.unverified() == []


def test_account_verification_can_opt_into_a_resend():
    """``resend=True`` is the only way to trigger send_email_verification."""
    async def run():
        from types import SimpleNamespace

        import rootpy
        from rootpy import AccountFactory

        calls = {"sent": [], "verified": []}

        class _Client:
            user = SimpleNamespace(id="uid")
            user_id = "uid"
            session = SimpleNamespace(token="tok-abc")

            async def close(self):
                pass

        originals = (
            rootpy.RootClient.create_account,
            rootpy.RootClient.send_email_verification,
            rootpy.RootClient.verify_email,
        )

        async def fake_create(cls, *, username, password, email,
                              access_token=None, **kw):
            return _Client()

        async def fake_send(cls, username=None, password=None, *,
                            token=None, turnstile_token=None):
            calls["sent"].append(token)

        async def fake_verify(cls, code, username=None, password=None, *,
                              token=None):
            calls["verified"].append(code)

        rootpy.RootClient.create_account = classmethod(fake_create)
        rootpy.RootClient.send_email_verification = classmethod(fake_send)
        rootpy.RootClient.verify_email = classmethod(fake_verify)
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")

            async def code_provider(email):
                return "123456"

            await factory.create_and_verify(
                turnstile_token="cf-token", code_provider=code_provider,
                timeout=5, poll_interval=0.02, resend=True,
            )
            return calls
        finally:
            (rootpy.RootClient.create_account,
             rootpy.RootClient.send_email_verification,
             rootpy.RootClient.verify_email) = originals

    calls = asyncio.run(run())
    assert calls["sent"] == ["tok-abc"]
    assert calls["verified"] == ["123456"]


def test_account_left_unverified_when_no_code_arrives():
    """A missing code must not lose the account -- verify it later."""
    async def run():
        from types import SimpleNamespace

        import rootpy
        from rootpy import AccountFactory

        class _Client:
            user = SimpleNamespace(id="uid")
            user_id = "uid"
            session = SimpleNamespace(token="tok")

            async def close(self):
                pass

        originals = (
            rootpy.RootClient.create_account,
            rootpy.RootClient.send_email_verification,
        )

        async def fake_create(cls, **kw):
            return _Client()

        async def fake_send(cls, *a, **kw):
            pass

        rootpy.RootClient.create_account = classmethod(fake_create)
        rootpy.RootClient.send_email_verification = classmethod(fake_send)
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")

            async def never(email):
                return None

            account = await factory.create_and_verify(
                turnstile_token="t", code_provider=never,
                timeout=0.1, poll_interval=0.02,
            )
            return account, factory
        finally:
            (rootpy.RootClient.create_account,
             rootpy.RootClient.send_email_verification) = originals

    account, factory = asyncio.run(run())
    assert account.verified is False
    assert account.username and account.email     # still recorded
    assert len(factory.unverified()) == 1


def test_signup_uses_connect_service_field_numbers():
    """Signup goes to connect.ConnectService/PasswordSignUp, which has its own
    numbering -- NOT root.UserGrpcService/SignUp's. Using the wrong schema
    makes every field miss and the server answers INVALID_ARGUMENT."""
    async def run():
        from rootpy.auth import AuthClient
        from rootpy.protocol import iter_fields, unwrap_grpc_web

        captured = {}

        class _Transport:
            async def unary(self, *, endpoint, body, headers, operation):
                captured["body"] = body
                captured["endpoint"] = endpoint
                raise RuntimeError("stop")

        auth = AuthClient.__new__(AuthClient)
        auth.transport = _Transport()
        try:
            await AuthClient.signup(
                auth, "someuser", "password", "hello-abc@solluw.com",
                turnstile_token="0.TOKEN",
            )
        except RuntimeError:
            pass
        payload = unwrap_grpc_web(captured["body"]) or captured["body"]
        fields = {
            number: bytes(value)
            for number, wire, value in iter_fields(payload) if wire == 2
        }
        return captured["endpoint"], fields

    endpoint, fields = asyncio.run(run())
    assert "ConnectService/PasswordSignUp" in endpoint
    assert fields[1] == b"someuser"
    assert fields[2] == b"password"
    assert fields[6] == b"hello-abc@solluw.com"
    # field 3 is TurnstileToken; field 7 is AccessToken -- different things.
    # Sending the captcha as 7 means the server never sees it and keeps
    # issuing challenges, which looks like the token being rejected.
    assert fields[3] == b"0.TOKEN"
    assert 7 not in fields
    assert 4 in fields and 5 in fields   # device description + id


def test_signup_reuses_the_device_id_across_a_challenge_retry():
    """Root binds the Turnstile challenge to the request. A fresh device id on
    the retry reads as a different signup, so the server mints a new challenge
    and the token you just solved is never checked."""
    async def run():
        from rootpy.auth import AuthClient
        from rootpy.identifiers import create_desktop_device_guid
        from rootpy.protocol import (
            decode_root_guid_message, iter_fields, unwrap_grpc_web,
        )

        seen = []

        class _Transport:
            async def unary(self, *, endpoint, body, headers, operation):
                payload = unwrap_grpc_web(body) or body
                for number, wire, value in iter_fields(payload):
                    if number == 5 and wire == 2:
                        seen.append(decode_root_guid_message(bytes(value)))
                raise RuntimeError("stop")

        auth = AuthClient.__new__(AuthClient)
        auth.transport = _Transport()

        # without an explicit id, each call invents its own
        for _ in range(2):
            try:
                await AuthClient.signup(auth, "u", "p", "e@x.com")
            except RuntimeError:
                pass
        drifting = seen.copy()

        # passing one through keeps it stable, which is what the retry needs
        seen.clear()
        device_id = create_desktop_device_guid()
        for token in (None, "0.SOLVED"):
            try:
                await AuthClient.signup(
                    auth, "u", "p", "e@x.com",
                    turnstile_token=token, device_id=device_id,
                )
            except RuntimeError:
                pass
        return drifting, seen

    drifting, stable = asyncio.run(run())
    assert drifting[0] != drifting[1]      # a fresh id per call by default
    assert stable[0] == stable[1]          # and stable when supplied


# --- two-step signup ------------------------------------------------------ #
def _two_step_harness():
    """Patch create_account to demand a challenge, then accept a token."""
    from types import SimpleNamespace

    import rootpy
    from rootpy.exceptions import TurnstileRequired

    calls = []
    original = rootpy.RootClient.create_account

    class _Client:
        def __init__(self, username):
            self.user = SimpleNamespace(id=f"uid-{username}")
            self.user_id = f"uid-{username}"
            self.session = SimpleNamespace(token="tok")

        async def close(self):
            pass

    async def fake(cls, *, username, password, email,
                   turnstile_token=None, device_id=None, **kw):
        calls.append(
            {"username": username, "password": password, "email": email,
             "device_id": device_id, "token": turnstile_token}
        )
        if not turnstile_token:
            raise TurnstileRequired(
                "https://infra.rootapp.com/ts.html?sitekey=0x4A"
                "&action=password_signup&cdata=ABC123",
                "password_signup",
            )
        return _Client(username)

    rootpy.RootClient.create_account = classmethod(fake)
    return calls, original


def test_two_step_signup_reuses_every_detail():
    """Step 3 must repeat the username, password, email AND device id from
    step 1, or Root mints a new challenge and never checks the token."""
    async def run():
        import rootpy
        from rootpy import AccountFactory

        calls, original = _two_step_harness()
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
            challenge = await factory.create_return_turnstile(username="bob")
            account = await factory.create_with_turnstile(
                turnstile_token="1.SOLVED", challenge=challenge,
            )
            return challenge, account, calls
        finally:
            rootpy.RootClient.create_account = original

    challenge, account, calls = asyncio.run(run())

    # the challenge behaves as a plain string
    assert isinstance(challenge, str)
    assert challenge.startswith("https://")
    assert "cdata=ABC123" in challenge

    # ...and carries the context
    assert challenge.username == "bob"
    assert challenge.email.endswith("@solluw.com")
    assert challenge.device_id

    # both attempts are the SAME signup
    first, second = calls
    assert first["username"] == second["username"]
    assert first["password"] == second["password"]
    assert first["email"] == second["email"]
    assert first["device_id"] == second["device_id"]     # the critical one
    assert first["token"] is None and second["token"] == "1.SOLVED"

    assert account.username == "bob"
    assert account.device_id == first["device_id"]


def test_two_step_refuses_without_context():
    """Finishing without the step-1 details is a clear error, not a silent
    new challenge."""
    async def run():
        from rootpy import AccountFactory

        factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
        try:
            await factory.create_with_turnstile(turnstile_token="1.SOLVED")
            return None
        except ValueError as exc:
            return str(exc)

    message = asyncio.run(run())
    assert message and "device_id" in message


def test_two_step_handles_no_challenge_needed():
    """If Root doesn't demand a challenge the account is created there and
    then -- surfaced as AlreadyCreatedError, with the account attached."""
    async def run():
        from types import SimpleNamespace

        import rootpy
        from rootpy import AccountFactory, AlreadyCreatedError

        original = rootpy.RootClient.create_account

        class _Client:
            user = SimpleNamespace(id="uid")
            user_id = "uid"
            session = SimpleNamespace(token="tok")

            async def close(self):
                pass

        async def fake(cls, **kw):
            return _Client()

        rootpy.RootClient.create_account = classmethod(fake)
        try:
            factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
            try:
                await factory.create_return_turnstile(username="bob")
                return None
            except AlreadyCreatedError as done:
                return done.account, factory
        finally:
            rootpy.RootClient.create_account = original

    result = asyncio.run(run())
    assert result is not None
    account, factory = result
    assert account.username == "bob"
    assert factory.accounts == [account]      # recorded, not lost


# --- multi-reply ---------------------------------------------------------- #
def _reply_harness():
    from rootpy.models import Message

    channel_id = "00000000-0000-00c1-0000-000000000003"
    community_id = "00000000-0000-00aa-0000-000000000002"

    def mid(index):
        return f"00000000-0000-{index:04x}-0000-000000000001"

    client = RootClient(token="x")
    client.messages._token_getter = lambda: "x"
    captured = {}

    class _Response:
        content = b"\x00\x00\x00\x00\x00"

    async def fake_unary(**kwargs):
        captured["body"] = kwargs["body"]
        return _Response()

    client.messages.transport.unary = fake_unary

    def parents():
        from rootpy.protocol import (
            decode_root_guid_message, iter_fields, unwrap_grpc_web,
        )

        payload = unwrap_grpc_web(captured["body"]) or captured["body"]
        return [
            decode_root_guid_message(bytes(value))
            for number, wire, value in iter_fields(payload)
            if number == 14 and wire == 2
        ]

    messages = [
        Message(id=mid(i), container_id=channel_id, user_id="u",
                content=str(i), community_id=community_id,
                _service=client.messages)
        for i in range(3)
    ]
    return client, messages, parents, channel_id, community_id


def test_reply_to_several_messages_at_once():
    async def run():
        client, messages, parents, _cid, _comm = _reply_harness()
        await client.reply_to(messages, "answering all three")
        result = parents()
        await client.close()
        return result

    assert len(asyncio.run(run())) == 3


def test_message_reply_with_includes_itself():
    async def run():
        client, messages, parents, _cid, _comm = _reply_harness()
        await messages[0].reply_with(messages[1:], "and again")
        result = parents()
        await client.close()
        return result

    assert len(asyncio.run(run())) == 3      # self + the two others


def test_channel_reply_to():
    async def run():
        from rootpy.models import Channel

        client, messages, parents, channel_id, community_id = _reply_harness()
        channel = Channel(
            id=channel_id, community_id=community_id, channel_group_id="g",
            name="general", channel_type=1, _client=client,
        )
        await channel.reply_to(messages[:2], "two")
        result = parents()
        await client.close()
        return result

    assert len(asyncio.run(run())) == 2


def test_single_reply_still_sends_one_parent():
    async def run():
        client, messages, parents, _cid, _comm = _reply_harness()
        await messages[0].reply("just one")
        result = parents()
        await client.close()
        return result

    assert len(asyncio.run(run())) == 1


def test_reply_targets_normalise_and_dedupe():
    from rootpy.models import Message
    from rootpy.services.messages import normalise_reply_targets

    def mid(index):
        return f"00000000-0000-{index:04x}-0000-000000000001"

    message = Message(id=mid(1), container_id="c", user_id="u",
                      content="x", community_id=None)

    assert len(normalise_reply_targets([mid(1), mid(2)])) == 2      # bare ids
    assert len(normalise_reply_targets(message)) == 1               # not a list
    assert len(normalise_reply_targets([message, message])) == 1    # deduped
    assert normalise_reply_targets(None) == []


def test_reply_target_limit_is_enforced():
    """Root rejects the whole send past 5, without saying why -- fail here
    with a clear message instead."""
    from rootpy.services.messages import MAX_REPLY_TARGETS, normalise_reply_targets

    assert MAX_REPLY_TARGETS == 5
    too_many = [f"00000000-0000-{i:04x}-0000-000000000001" for i in range(6)]
    try:
        normalise_reply_targets(too_many)
    except ValueError as exc:
        assert "at most 5" in str(exc)
        return
    raise AssertionError("expected ValueError for 6 reply targets")


# --- optional proxy ------------------------------------------------------- #
def test_proxy_is_optional_and_off_by_default():
    from rootpy.transport import GrpcWebTransport

    client = RootClient(token="x")
    assert client.proxy is None
    assert client.transport.proxy is None
    assert GrpcWebTransport().proxy is None


def test_proxy_reaches_the_transport_and_gateway():
    client = RootClient(token="x", proxy="socks5://127.0.0.1:1080")
    assert client.proxy == "socks5://127.0.0.1:1080"
    assert client.transport.proxy == "socks5://127.0.0.1:1080"


def test_supplied_transport_keeps_its_own_proxy():
    """Passing a transport means you configured it -- don't override it."""
    from rootpy.transport import GrpcWebTransport

    transport = GrpcWebTransport(proxy="socks5://127.0.0.1:1081")
    client = RootClient(token="x", transport=transport)
    assert client.transport.proxy == "socks5://127.0.0.1:1081"


def test_each_client_can_use_a_different_proxy():
    """Several accounts, each out of a different tunnel."""
    clients = [
        RootClient(token=f"t{i}", proxy=f"socks5://127.0.0.1:{1080 + i}")
        for i in range(4)
    ]
    assert [c.transport.proxy for c in clients] == [
        "socks5://127.0.0.1:1080", "socks5://127.0.0.1:1081",
        "socks5://127.0.0.1:1082", "socks5://127.0.0.1:1083",
    ]
    # and they don't share a connection pool
    assert len({id(c.transport) for c in clients}) == 4


def test_account_factory_routes_signup_through_its_proxy():
    """A factory-level proxy must reach create_account, or signup goes out
    direct while everything else is tunnelled -- which looks like the proxy
    working until you check the exit IP."""
    async def run():
        import rootpy
        from rootpy import AccountFactory
        from rootpy.exceptions import TurnstileRequired

        captured = {}
        original = rootpy.RootClient.create_account

        async def fake(cls, **kwargs):
            captured.update(kwargs)
            raise TurnstileRequired("https://x/ts", "password_signup")

        rootpy.RootClient.create_account = classmethod(fake)
        try:
            factory = AccountFactory(
                email_pattern="service-{tag}@solluw.com",
                proxy="socks5://127.0.0.1:1080",
            )
            try:
                await factory.create_return_turnstile(username="bob")
            except TurnstileRequired:
                pass
            return captured
        finally:
            rootpy.RootClient.create_account = original

    captured = asyncio.run(run())
    assert captured.get("proxy") == "socks5://127.0.0.1:1080"


def test_create_account_accepts_a_proxy():
    import inspect

    params = inspect.signature(RootClient.create_account).parameters
    assert "proxy" in params
    assert "transport" in params
