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
