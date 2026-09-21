from __future__ import annotations

from typing import Optional

import struct
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any, Optional


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Unexpected end of varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift >= 70:
            raise ValueError("Invalid varint")


def field_key(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def length_field(field_number: int, payload: bytes) -> bytes:
    return field_key(field_number, 2) + encode_varint(len(payload)) + payload


def string_field(field_number: int, value: str) -> bytes:
    return length_field(field_number, value.encode("utf-8"))


def bool_field(field_number: int, value: bool) -> bytes:
    return field_key(field_number, 0) + encode_varint(1 if value else 0)


def grpc_frame(payload: bytes) -> bytes:
    """Wrap a protobuf message in a gRPC-web frame (flag + 4-byte length)."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def is_grpc_framed(body: bytes) -> bool:
    """True if ``body`` already looks like a single gRPC-web frame.

    A frame is a 1-byte flag, a 4-byte big-endian length, then exactly that
    many bytes. Raw protobuf effectively never matches this (its first byte is
    a field tag, and the length wouldn't line up), which makes framing safely
    idempotent -- see :meth:`GrpcWebTransport.unary`.
    """
    if len(body) < 5:
        return False
    if body[0] not in (0x00, 0x01):
        return False
    return int.from_bytes(body[1:5], "big") == len(body) - 5


def unwrap_grpc_web(body: bytes) -> Optional[bytes]:
    """Return the data frame's payload from a gRPC-web response body.

    Responses carry the message in a normal frame and the trailers in a frame
    flagged 0x80. Parsing the raw body instead of the payload produces
    "unsupported wire type" errors, so always unwrap first.
    """
    for flag, frame in iter_grpc_web_frames(body):
        if not flag & 0x80:
            return frame
    return None


def iter_grpc_web_frames(body: bytes) -> Iterable[tuple[int, bytes]]:
    offset = 0
    while offset + 5 <= len(body):
        flag = body[offset]
        length = int.from_bytes(body[offset + 1 : offset + 5], "big")
        offset += 5
        frame = body[offset : offset + length]
        offset += length
        if len(frame) != length:
            raise ValueError(
                f"Truncated gRPC-Web frame: expected {length}, got {len(frame)}"
            )
        yield flag, frame


def iter_fields(data: bytes) -> Iterable[tuple[int, int, Any]]:
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError(f"Truncated fixed64 field {field_number}")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            if offset + length > len(data):
                raise ValueError(f"Truncated field {field_number}")
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError(f"Truncated fixed32 field {field_number}")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")
        yield field_number, wire_type, value


def decode_root_guid_message(data: bytes) -> Optional[str]:
    high64 = None
    low64 = None
    for field_number, wire_type, value in iter_fields(data):
        if field_number == 1 and wire_type == 1:
            high64 = struct.unpack("<Q", value)[0]
        elif field_number == 2 and wire_type == 1:
            low64 = struct.unpack("<Q", value)[0]
    if high64 is None or low64 is None:
        return None
    compact = f"{high64:016x}{low64:016x}"
    return (
        f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
        f"{compact[16:20]}-{compact[20:32]}"
    )


def decode_timestamp_message(data: bytes) -> Optional[datetime]:
    seconds = 0
    nanos = 0
    found = False
    for field_number, wire_type, value in iter_fields(data):
        if field_number == 1 and wire_type == 0:
            seconds = int(value)
            found = True
        elif field_number == 2 and wire_type == 0:
            nanos = int(value)
            found = True
    if not found:
        return None
    return datetime.fromtimestamp(
        seconds + nanos / 1_000_000_000,
        tz=timezone.utc,
    )
