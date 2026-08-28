from __future__ import annotations

import base64
import uuid
import secrets
import struct
from datetime import datetime, timedelta, timezone

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


def root_guid_datetime(value: str) -> datetime:
    """When a Root id was minted, read straight out of the id.

    Every Root GUID is a timestamp GUID: ``create_root_guid`` above packs
    milliseconds since 2020-01-01 UTC into the top 48 bits of the high word,
    so unpacking is the same operation backwards. The client relies on this
    too -- ``MessageGuid.ToDateTime()`` is how it dates a message it has
    nothing else for.

    That makes it exact for anything Root created, and worth preferring over
    "when this process happened to see it": a backfilled message and a pushed
    one then carry the same clock.

        >>> root_guid_datetime("0030b367-6f52-8702-b0a1-aa5c7b5c338c")
        datetime.datetime(2026, 8, 17, 22, 18, 50, 578000, tzinfo=...)
    """
    high64, _low64 = parse_root_guid(value)
    return ROOT_GUID_START_DATE + timedelta(milliseconds=high64 >> 16)


def root_guid_type(value: str) -> int:
    """The ``RootGuidType`` byte an id carries (1 person, 2 community, 4 channel).

    The low byte of the high word, per ``create_root_guid``. Useful for telling
    what an id *is* when a payload gives you one without saying -- a container
    id, for instance, is a channel (4) in a community and a direct message
    (15) otherwise.
    """
    high64, _low64 = parse_root_guid(value)
    return high64 & 0xFF


def create_desktop_device_guid() -> str:
    return format_root_guid(*create_root_guid(ROOT_GUID_TYPE_DESKTOP))


def create_command_idempotency_guid() -> tuple[int, int]:
    return create_root_guid(ROOT_GUID_TYPE_COMMAND_IDEMPOTENCY)
