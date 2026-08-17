from __future__ import annotations

import base64
import uuid
import secrets
import struct
from datetime import datetime, timezone

from .protocol import field_key

ROOT_GUID_START_DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)
ROOT_GUID_TYPE_DESKTOP = 18
ROOT_GUID_TYPE_COMMAND_IDEMPOTENCY = 30


def normalize_root_guid(value: str) -> str:

    if not isinstance(value, str):
        raise TypeError("Root GUID must be a string")

    value = value.strip()

    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        pass

    if len(value) == 22:
        try:
            raw = base64.urlsafe_b64decode(value + "==")
        except Exception:
            raw = b""

        if len(raw) == 16:
            return str(uuid.UUID(bytes=raw))

    raise ValueError(f"Invalid Root GUID: {value!r}")


def parse_root_guid(value: str) -> tuple[int, int]:
    compact = normalize_root_guid(value).replace("-", "")
    return int(compact[:16], 16), int(compact[16:], 16)


def format_root_guid(high64: int, low64: int) -> str:
    compact = f"{high64:016x}{low64:016x}"
    return (
        f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
        f"{compact[16:20]}-{compact[20:32]}"
    )


def encode_root_guid_parts(high64: int, low64: int) -> bytes:
    return (
        field_key(1, 1)
        + struct.pack("<Q", high64)
        + field_key(2, 1)
        + struct.pack("<Q", low64)
    )


def encode_root_guid(value: str) -> bytes:
    high64, low64 = parse_root_guid(value)
    return encode_root_guid_parts(high64, low64)


def create_root_guid(root_guid_type: int) -> tuple[int, int]:
    random_bytes = bytearray(secrets.token_bytes(10))
    milliseconds = int(
        (datetime.now(timezone.utc) - ROOT_GUID_START_DATE).total_seconds() * 1000
    )
    random_bytes[1] = root_guid_type
    random_bytes[2] = 0x80 | (random_bytes[2] >> 2)
    high64 = (
        (milliseconds << 16)
        + 0x8000
        + ((random_bytes[0] & 0x0F) << 8)
        + random_bytes[1]
    )
    low64 = int.from_bytes(random_bytes[2:10], "big")
    return high64, low64


def create_desktop_device_guid() -> str:
    return format_root_guid(*create_root_guid(ROOT_GUID_TYPE_DESKTOP))


def create_command_idempotency_guid() -> tuple[int, int]:
    return create_root_guid(ROOT_GUID_TYPE_COMMAND_IDEMPOTENCY)
