"""Wire-format regression tests.

These exist because of a genuinely painful bug: three RPC methods sent their
protobuf body *unframed*. The server's handler then threw a bare
UNKNOWN(2) "Exception was thrown by handler", which looks identical to a
permissions wall -- it cost a long debugging detour. The transport now frames
every request itself; these tests make sure it stays that way.
"""

import asyncio

import pytest

from rootpy.protocol import (
    grpc_frame,
    is_grpc_framed,
    iter_fields,
    unwrap_grpc_web,
)
from rootpy.transport import GrpcWebTransport


def test_frame_roundtrip():
    payload = b"\x52\x12\x09hello-world-body"
    framed = grpc_frame(payload)
    assert framed[0] == 0x00
    assert int.from_bytes(framed[1:5], "big") == len(payload)
    assert unwrap_grpc_web(framed) == payload


def test_is_grpc_framed_detects_correctly():
    payload = b"\x52\x12\x09abc"
    assert is_grpc_framed(grpc_frame(payload))
    assert not is_grpc_framed(payload)          # raw protobuf
    assert not is_grpc_framed(b"")
    assert not is_grpc_framed(b"\x00\x00")


def test_unwrap_skips_trailer_frame():
    payload = b"\x2a\x03abc"
    trailer = b"grpc-status:0\r\n"
    body = grpc_frame(payload) + b"\x80" + len(trailer).to_bytes(4, "big") + trailer
    assert unwrap_grpc_web(body) == payload


class _FakeResponse:
    status_code = 200
    headers = {"grpc-status": "0"}
    content = b""
    text = ""


def _transport_capturing(sent):
    transport = GrpcWebTransport.__new__(GrpcWebTransport)
    transport.max_retries = 0
    transport.retry_base = 0.0

    class _Client:
        async def post(self, endpoint, headers=None, content=None):
            sent.append(content)
            return _FakeResponse()

    transport._get_client = lambda: _Client()
    return transport


class _Response:
    def __init__(self, status_code, headers=None, content=b"", text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = text


def _transport(responses, *, max_retries=2):
    """A transport whose POSTs replay ``responses`` (a response or an exception)."""
    transport = GrpcWebTransport.__new__(GrpcWebTransport)
    transport.max_retries = max_retries
    transport.retry_base = 0.0
    transport._cooldowns = {}
    calls = {"n": 0}

    class _Client:
        async def post(self, endpoint, headers=None, content=None):
            calls["n"] += 1
            item = responses[min(calls["n"] - 1, len(responses) - 1)]
            if isinstance(item, BaseException):
                raise item
            return item

    transport._get_client = lambda: _Client()
    return transport, calls


class TestTransportRetries:
    """The retry block every RPC in the library rides on."""

    ENDPOINT = "https://example.invalid/root.Svc/Method"

    def _run(self, transport):
        return asyncio.run(
            transport.unary(
                endpoint=self.ENDPOINT, body=b"\x2a\x03abc",
                headers={}, operation="Test",
            )
        )

    def test_remote_protocol_error_is_retried(self):
        """An HTTP/2 GOAWAY is the routine keep-alive failure, not a bug.

        ``httpx.RemoteProtocolError`` is neither a ``NetworkError`` nor a
        ``TimeoutException`` -- it sits under ``ProtocolError`` -- so it used
        to escape the retry block entirely: no retry however high
        ``max_retries`` was, and no ``stats`` entry either, so it did not even
        show up in the timing report.
        """
        import httpx

        ok = _Response(200, {"grpc-status": "0"})
        transport, calls = _transport(
            [httpx.RemoteProtocolError("server disconnected"), ok]
        )
        response = self._run(transport)
        assert response.status_code == 200
        assert calls["n"] == 2, "the RemoteProtocolError was not retried"

    def test_local_protocol_error_is_not_retried(self):
        """Our own bug -- retrying just repeats it."""
        import httpx

        transport, calls = _transport([httpx.LocalProtocolError("we sent junk")])
        with pytest.raises(httpx.LocalProtocolError):
            self._run(transport)
        assert calls["n"] == 1

    def test_a_retried_transport_error_is_still_counted(self):
        import httpx

        ok = _Response(200, {"grpc-status": "0"})
        transport, _ = _transport(
            [httpx.RemoteProtocolError("boom"), ok]
        )
        from rootpy.stats import TransportStats

        transport.stats = TransportStats()
        self._run(transport)
        assert transport.stats.totals()["retries"] == 1


class TestRateLimitCooldownIsAlwaysRecorded:
    """A 429 must register its cooldown even when we are out of retries.

    ``_note_rate_limit`` used to live inside the ``if retryable and attempt <=
    max_retries`` branch, so the *last* 429 -- the one that actually gives up
    -- threw away the server's Retry-After. Nothing was recorded for other
    coroutines to respect and no ``rate_limited`` stat was counted, exactly
    when a fan-out was hitting the limit hardest.
    """

    ENDPOINT = "https://example.invalid/root.Svc/Method"

    def _run(self, transport):
        return asyncio.run(
            transport.unary(
                endpoint=self.ENDPOINT, body=b"\x2a\x03abc",
                headers={}, operation="Test",
            )
        )

    def _throttled(self, max_retries):
        from rootpy.stats import TransportStats

        limited = _Response(429, {"retry-after": "7"}, text="slow down")
        transport, calls = _transport([limited], max_retries=max_retries)
        transport.stats = TransportStats()
        return transport, calls

    def test_the_final_429_still_records_a_cooldown(self):
        from rootpy.exceptions import RateLimited

        transport, _ = self._throttled(max_retries=0)
        with pytest.raises(RateLimited):
            self._run(transport)
        assert transport._cooldowns, (
            "the server said Retry-After: 7 and the transport forgot it"
        )

    def test_the_final_429_is_counted_as_rate_limited(self):
        from rootpy.exceptions import RateLimited

        transport, _ = self._throttled(max_retries=0)
        with pytest.raises(RateLimited):
            self._run(transport)
        assert transport.stats.totals()["rate_limited"] == 1

    def test_a_429_that_is_retried_still_behaves(self):
        """The path that already worked must keep working."""
        from rootpy.exceptions import RateLimited

        transport, calls = self._throttled(max_retries=1)
        with pytest.raises(RateLimited):
            self._run(transport)
        assert calls["n"] == 2
        assert transport.stats.totals()["rate_limited"] == 2
        assert transport._cooldowns

    def test_a_non_429_failure_records_no_cooldown(self):
        from rootpy.stats import TransportStats

        transport, _ = _transport(
            [_Response(500, {}, text="boom")], max_retries=0
        )
        transport.stats = TransportStats()
        with pytest.raises(Exception):
            self._run(transport)
        assert not transport._cooldowns
        assert transport.stats.totals()["rate_limited"] == 0


def test_transport_frames_raw_bodies():
    """THE regression test: a raw body must go out framed."""
    sent = []
    transport = _transport_capturing(sent)
    raw = b"\x52\x12\x09raw-protobuf-body"

    asyncio.run(
        transport.unary(
            endpoint="https://example.invalid/Svc/Method",
            body=raw,
            headers={},
            operation="Test",
        )
    )

    assert is_grpc_framed(sent[0]), "transport must frame an unframed body"
    assert unwrap_grpc_web(sent[0]) == raw


def test_transport_does_not_double_frame():
    """Call sites that already frame must not get framed twice."""
    sent = []
    transport = _transport_capturing(sent)
    raw = b"\x52\x12\x09raw-protobuf-body"

    asyncio.run(
        transport.unary(
            endpoint="https://example.invalid/Svc/Method",
            body=grpc_frame(raw),
            headers={},
            operation="Test",
        )
    )

    assert unwrap_grpc_web(sent[0]) == raw, "body was framed twice"


class _RecordingTransport:
    """Captures what a service would have sent, without a network."""

    def __init__(self):
        self.sent = []

    async def unary(self, *, endpoint, body, headers, operation):
        self.sent.append((endpoint, body, operation))

        class _Response:
            content = b""
            status_code = 200
            headers: dict = {}

        return _Response()


class TestCommunityAttachWire:
    """``Attach`` is the call that turns channel push on -- pin its shape.

    Membership delivers nothing on its own: without this request the hub sends
    no packet for a community at all, so an encoding regression here would not
    raise, it would just make the SDK silently stop hearing about channels.
    """

    @staticmethod
    def _service():
        from rootpy.services.communities import CommunityService

        transport = _RecordingTransport()
        return CommunityService(transport, lambda: "token"), transport

    def test_attach_sends_the_community_as_field_10(self):
        service, transport = self._service()
        asyncio.run(service.attach("0030b367-6f52-8702-b0a1-aa5c7b5c338c"))
        endpoint, body, operation = transport.sent[0]

        assert endpoint.endswith("root.CommunityGrpcService/Attach")
        assert operation == "CommunityAttach"
        assert is_grpc_framed(body)
        fields = dict((n, v) for n, _w, v in iter_fields(unwrap_grpc_web(body)))
        assert set(fields) == {10}, "Attach carries only the community id"

    def test_detach_is_the_same_shape_at_the_other_endpoint(self):
        service, transport = self._service()
        asyncio.run(service.detach("0030b367-6f52-8702-b0a1-aa5c7b5c338c"))
        endpoint, body, operation = transport.sent[0]

        assert endpoint.endswith("root.CommunityGrpcService/Detach")
        assert operation == "CommunityDetach"
        assert is_grpc_framed(body)

    def test_attach_and_detach_encode_the_same_id_identically(self):
        service, transport = self._service()
        asyncio.run(service.attach("0030b367-6f52-8702-b0a1-aa5c7b5c338c"))
        asyncio.run(service.detach("0030B367-6F52-8702-B0A1-AA5C7B5C338C"))
        assert transport.sent[0][1] == transport.sent[1][1], (
            "case in the id changed the bytes on the wire"
        )

    def test_detach_many_repeats_field_10(self):
        service, transport = self._service()
        asyncio.run(service.detach_many([
            "0030b367-6f52-8702-b0a1-aa5c7b5c338c",
            "0030cf51-e179-8d02-b2ab-480d511fe3ba",
        ]))
        endpoint, body, _operation = transport.sent[0]

        assert endpoint.endswith("root.CommunityGrpcService/DetachMany")
        numbers = [n for n, _w, _v in iter_fields(unwrap_grpc_web(body))]
        assert numbers == [10, 10]

    def test_detach_many_of_nothing_sends_nothing(self):
        """An empty list is a no-op, not an empty request the server rejects."""
        service, transport = self._service()
        asyncio.run(service.detach_many([]))
        assert transport.sent == []


# --------------------------------------------------------------------------
# Community Discovery
# --------------------------------------------------------------------------
class _ReplyingTransport(_RecordingTransport):
    """A recording transport that also hands back a canned gRPC-web body."""

    def __init__(self, payload: bytes = b""):
        super().__init__()
        self._payload = payload

    async def unary(self, *, endpoint, body, headers, operation):
        await super().unary(endpoint=endpoint, body=body,
                            headers=headers, operation=operation)
        framed = self._payload
        frame = b"\x00" + len(framed).to_bytes(4, "big") + framed

        class _Response:
            content = frame
            status_code = 200
            headers: dict = {}

        return _Response()


class TestCommunityDiscoveryWire:
    """Discovery numbers its payload fields from 10, not from 1.

    This is the trap. A guess that starts at field 1 produces a request the
    server accepts without complaint and that returns nothing useful, so
    there is no error to follow. These tests pin the real numbers.
    """

    @staticmethod
    def _service(payload: bytes = b""):
        from rootpy.services.discovery import CommunityDiscoveryService
        transport = _ReplyingTransport(payload)
        return CommunityDiscoveryService(transport, lambda: "token"), transport

    @staticmethod
    def _fields(body: bytes):
        from rootpy.protocol import iter_fields, unwrap_grpc_web
        inner = unwrap_grpc_web(body)
        if inner is None:
            inner = body[5:]
        return {n: v for n, _wt, v in iter_fields(inner)}

    def test_search_sends_its_fields_at_10_through_15(self):
        from rootpy.enums import CommunityCategory, CommunityDiscoverySort
        service, transport = self._service()
        asyncio.run(service.search(
            "minecraft",
            category=CommunityCategory.GAMING,
            sort_by=CommunityDiscoverySort.MEMBER_COUNT,
            sort_ascending=True,
            offset=30,
            limit=15,
        ))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith("root.CommunityDiscoveryGrpcService/Search")
        assert operation == "CommunityDiscoverySearch"
        fields = self._fields(body)
        assert fields[10] == b"minecraft"
        assert fields[11] == CommunityCategory.GAMING
        assert fields[12] == CommunityDiscoverySort.MEMBER_COUNT
        assert fields[13] == 1
        assert fields[14] == 30
        assert fields[15] == 15
        # nothing may land on field 1: that slot is the envelope's
        assert 1 not in fields

    def test_search_omits_what_it_was_not_given(self):
        """An empty search lists the directory; it must not send empty fields."""
        service, transport = self._service()
        asyncio.run(service.search())
        fields = self._fields(transport.sent[0][1])
        assert set(fields) == {15}          # the default limit only
        assert fields[15] == 30

    def test_search_rejects_a_negative_page(self):
        service, _ = self._service()
        with pytest.raises(ValueError):
            asyncio.run(service.search(offset=-1))
        with pytest.raises(ValueError):
            asyncio.run(service.search(limit=-5))

    def test_search_decodes_results_and_the_total(self):
        """``total`` is the whole match set, not this page -- page on it."""
        from rootpy.protocol import length_field, string_field, field_key, encode_varint
        from rootpy.identifiers import encode_root_guid

        cid = "0030bf26-999d-8101-82fd-66b280480024"
        row = bytearray()
        row += length_field(10, encode_root_guid(cid))
        row += string_field(11, "Test Community")
        row += length_field(12, string_field(1, "a description"))   # StringValue
        row += field_key(15, 0) + encode_varint(1)                  # GAMING
        row += field_key(16, 0) + encode_varint(4200)               # member_count
        row += field_key(19, 0) + encode_varint(1)                  # is_discoverable
        payload = length_field(10, bytes(row)) + field_key(11, 0) + encode_varint(97)

        service, _ = self._service(payload)
        results, total = asyncio.run(service.search("test"))
        assert total == 97
        assert len(results) == 1
        found = results[0]
        assert found.id == cid
        assert found.name == "Test Community"
        assert found.description == "a description"
        assert found.member_count == 4200
        assert found.is_discoverable is True
        # a StringValue the server omitted stays None, not ""
        assert found.banner_asset_uri is None

    def test_join_sends_the_community_id_at_10(self):
        service, transport = self._service()
        cid = "0030bf26-999d-8101-82fd-66b280480024"
        asyncio.run(service.join(cid))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith("root.CommunityDiscoveryGrpcService/Join")
        assert operation == "CommunityDiscoveryJoin"
        fields = self._fields(body)
        assert set(fields) == {10}
        assert 11 not in fields          # age flag omitted when false

    def test_join_sends_the_age_flag_only_when_asserted(self):
        service, transport = self._service()
        asyncio.run(service.join("0030bf26-999d-8101-82fd-66b280480024",
                                 is_age_verified=True))
        fields = self._fields(transport.sent[0][1])
        assert fields[11] == 1


class TestNotificationAndDiscoverableWire:
    """Three newer methods on services that already existed.

    All three put ``context`` at field 1 and their payload at 10+, which is
    the same shape Discovery uses. The one that is easy to get wrong is the
    member setting: it lives on ``CommunityMemberGrpcService``, not
    ``CommunityGrpcService``, because it changes your membership rather than
    the community.
    """

    COMMUNITY = "0030bf26-999d-8101-82fd-66b280480024"
    CHANNEL = "0030bf26-999d-8101-82fd-66b280480025"

    @staticmethod
    def _fields(body: bytes):
        from rootpy.protocol import iter_fields, unwrap_grpc_web
        inner = unwrap_grpc_web(body)
        if inner is None:
            inner = body[5:]
        return {n: v for n, _wt, v in iter_fields(inner)}

    @staticmethod
    def _community_service():
        from rootpy.services.communities import CommunityService
        transport = _RecordingTransport()
        return CommunityService(transport, lambda: "token"), transport

    def test_set_discoverable_sends_the_flag_at_11(self):
        service, transport = self._community_service()
        asyncio.run(service.set_discoverable(self.COMMUNITY, True))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith("root.CommunityGrpcService/SetDiscoverable")
        assert operation == "CommunitySetDiscoverable"
        fields = self._fields(body)
        assert 1 in fields          # context
        assert 10 in fields         # community_id
        assert fields[11] == 1

    def test_set_discoverable_false_still_sends_the_field(self):
        """proto3 would drop a false bool; unlisting must not become a no-op."""
        service, transport = self._community_service()
        asyncio.run(service.set_discoverable(self.COMMUNITY, False))
        assert self._fields(transport.sent[0][1])[11] == 0

    def test_member_setting_goes_to_the_member_service(self):
        from rootpy.enums import NotificationStatus
        service, transport = self._community_service()
        asyncio.run(service.set_notification_setting(
            self.COMMUNITY, NotificationStatus.MENTION))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith(
            "root.CommunityMemberGrpcService/SetNotificationSetting")
        assert operation == "CommunityMemberSetNotificationSetting"
        fields = self._fields(body)
        assert fields[12] == NotificationStatus.MENTION
        assert 11 not in fields     # community-wide when no channel given

    def test_member_setting_scopes_to_a_channel_when_asked(self):
        from rootpy.enums import NotificationStatus
        service, transport = self._community_service()
        asyncio.run(service.set_notification_setting(
            self.COMMUNITY, NotificationStatus.NONE, channel_id=self.CHANNEL))
        fields = self._fields(transport.sent[0][1])
        assert 11 in fields
        assert fields[12] == NotificationStatus.NONE

    def test_direct_message_setting_puts_the_id_at_10(self):
        from rootpy.enums import NotificationStatus
        from rootpy.services.direct_messages import DirectMessageService
        transport = _RecordingTransport()
        service = DirectMessageService(transport, lambda: "token")
        asyncio.run(service.set_notification_setting(
            self.COMMUNITY, NotificationStatus.ALL))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith(
            "root.DirectMessageGrpcService/SetNotificationSetting")
        fields = self._fields(body)
        assert 10 in fields
        assert fields[11] == NotificationStatus.ALL

class TestCommunityGetWire:
    """``CommunityGetResponse`` numbers its fields unlike any sibling.

    The id is 4 here, 3 on ``CommunityPacket`` and 10 on
    ``CommunityGetExtendedResponse``, so a parser copied from either of the
    other two decodes nothing. These pin the real numbers.
    """

    COMMUNITY = "0030bf26-999d-8101-82fd-66b280480024"
    OWNER = "0030bf26-999d-8101-82fd-66b280480025"
    CHANNEL = "0030bf26-999d-8101-82fd-66b280480026"

    @staticmethod
    def _fields(body: bytes):
        from rootpy.protocol import iter_fields, unwrap_grpc_web
        inner = unwrap_grpc_web(body)
        if inner is None:
            inner = body[5:]
        return {n: v for n, _wt, v in iter_fields(inner)}

    @classmethod
    def _response(cls, **overrides):
        """Build a CommunityGetResponse body from the real field numbers."""
        from rootpy.identifiers import encode_root_guid
        from rootpy.protocol import encode_varint, field_key, length_field

        def wrapper(text):
            return length_field(1, text.encode("utf-8"))

        payload = bytearray()
        payload += length_field(4, encode_root_guid(cls.COMMUNITY))
        payload += length_field(5, encode_root_guid(cls.OWNER))
        payload += length_field(6, encode_root_guid(cls.CHANNEL))
        payload += length_field(7, b"Sandbox")
        payload += length_field(8, b"3b82f6")
        payload += length_field(9, wrapper("https://cdn/icon.png"))
        payload += field_key(10, 0) + encode_varint(1)
        payload += length_field(12, wrapper("a description"))
        payload += field_key(19, 0) + encode_varint(1)
        payload += field_key(20, 0) + encode_varint(overrides.get("category", 3))
        if "banner" in overrides:
            payload += length_field(21, wrapper(overrides["banner"]))
        payload += field_key(22, 0) + encode_varint(
            1 if overrides.get("is_discoverable", True) else 0)
        payload += field_key(23, 0) + encode_varint(
            overrides.get("status", 0))
        payload += field_key(50, 0) + encode_varint(412)
        return bytes(payload)

    def _service(self, payload: bytes):
        from rootpy.services.communities import CommunityService
        transport = _ReplyingTransport(payload)
        return CommunityService(transport, lambda: "token"), transport

    def test_the_request_is_just_the_id_at_10_with_no_context(self):
        """``CommunityGetRequest`` has no ``context`` field, unlike the writes."""
        service, transport = self._service(self._response())
        asyncio.run(service.get(self.COMMUNITY))
        endpoint, body, operation = transport.sent[0]
        assert endpoint.endswith("root.CommunityGrpcService/Get")
        assert operation == "CommunityGet"
        fields = self._fields(body)
        assert set(fields) == {10}, fields

    def test_it_reads_the_id_from_field_4_not_3_or_10(self):
        service, _ = self._service(self._response())
        community = asyncio.run(service.get(self.COMMUNITY))
        assert community.id == self.COMMUNITY
        assert community.owner_user_id == self.OWNER
        assert community.default_channel_id == self.CHANNEL
        assert community.name == "Sandbox"

    def test_the_discoverable_pair_comes_off_22_and_23(self):
        from rootpy.enums import CommunityDiscoverableRequirement

        service, _ = self._service(self._response(
            is_discoverable=False,
            status=int(CommunityDiscoverableRequirement.MEMBER_COUNT),
        ))
        community = asyncio.run(service.get(self.COMMUNITY))
        assert community.is_discoverable is False
        assert (community.is_discoverable_status
                == CommunityDiscoverableRequirement.MEMBER_COUNT)

    def test_category_is_20_and_banner_is_a_wrapper_at_21(self):
        from rootpy.enums import CommunityCategory

        service, _ = self._service(self._response(
            category=int(CommunityCategory.GAMING),
            banner="https://cdn/banner.png",
        ))
        community = asyncio.run(service.get(self.COMMUNITY))
        assert community.category == CommunityCategory.GAMING
        assert community.banner_asset_uri == "https://cdn/banner.png"

    def test_an_absent_banner_is_none_rather_than_empty(self):
        service, _ = self._service(self._response())
        community = asyncio.run(service.get(self.COMMUNITY))
        assert community.banner_asset_uri is None

    def test_a_response_without_an_id_is_an_error_not_a_blank_community(self):
        service, _ = self._service(b"")
        with pytest.raises(RuntimeError, match="no community ID"):
            asyncio.run(service.get(self.COMMUNITY))

