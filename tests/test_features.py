"""Tests for the convenience layer: typed events, wait_for, presence,
object methods, and rate-limit awareness."""

import asyncio

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
def test_presence_sends_correct_status_value():
    async def run():
        client = RootClient()
        client.users._token_getter = lambda: "token"
        captured = {}

        class _Response:
            content = b""

        async def fake_unary(**kwargs):
            captured["body"] = kwargs["body"]
            captured["endpoint"] = kwargs["endpoint"]
            return _Response()

        client.users.transport.unary = fake_unary
        result = await client.go_idle()
        fields = {n: v for n, _w, v in iter_fields(captured["body"])}
        await client.close()
        return result, fields, captured["endpoint"]

    result, fields, endpoint = asyncio.run(run())
    assert result is UserOnlineStatus.INACTIVE
    assert fields[10] == 4
    assert endpoint.endswith("SetMaxOnlineStatus")


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
def test_rate_limit_cooldown_is_shared():
    async def run():
        transport = GrpcWebTransport()

        class _Response:
            def __init__(self, code, headers=None):
                self.status_code = code
                self.headers = headers or {}
                self.content = b""
                self.text = ""

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
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers={}, operation="T"
        )
        assert "root.v2.MessageGrpcService/List" in transport._cooldowns

        # a later call must wait rather than pile on
        loop = asyncio.get_running_loop()
        transport._cooldowns["root.v2.MessageGrpcService/List"] = loop.time() + 0.3
        started = loop.time()
        await transport.unary(
            endpoint=endpoint, body=b"\x08\x01", headers={}, operation="T"
        )
        return loop.time() - started

    assert asyncio.run(run()) >= 0.25


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
    """Each account gets its own client, handlers, and rate-limit budget."""
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
            self.user = CurrentUser(id="me", username="ok")

        async def fake_connect(self):
            await asyncio.sleep(0.01)

        rootpy.RootClient.login_token = fake_login
        rootpy.RootClient.connect = fake_connect
        try:
            host = MultiClientHost(stagger=0.0)
            host.add("good1", "t1")
            host.add("broken", "bad")
            host.add("good2", "t2")
            await host.start()
            ready = await host.wait_ready(timeout=5)
            transports = {
                id(a.client.transport)
                for a in host.accounts.values() if a.client
            }
            await host.stop()
            return ready, len(transports)
        finally:
            rootpy.RootClient.login_token = original_login
            rootpy.RootClient.connect = original_connect

    ready, transport_count = asyncio.run(run())
    # a bad token must not take down the others
    assert ready["good1"] and ready["good2"]
    assert not ready["broken"]
    # separate transports => one account's 429 can't stall another
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
        three = await client.get_random_members(cid, 3)
        too_many = await client.get_random_members(cid, 99)
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
