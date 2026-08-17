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
