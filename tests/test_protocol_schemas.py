"""Schema + decoding regression tests.

Each of these locks in something that was wrong at some point and took real
debugging to find.
"""

import struct

from rootpy.enums import ContentFlagReason, ErrorCodeType
from rootpy.packet_schemas import PACKET_SCHEMAS, decode_packet
from rootpy.packets import PacketType
from rootpy.protocol import iter_fields
from rootpy.root_exception import decode_root_exception


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(fn, wt):
    return _varint((fn << 3) | wt)


def _msg(fn, data):
    return _tag(fn, 2) + _varint(len(data)) + bytes(data)


def _str(fn, text):
    b = text.encode()
    return _tag(fn, 2) + _varint(len(b)) + b


def _uuid(high, low):
    return _tag(1, 1) + struct.pack("<Q", high) + _tag(2, 1) + struct.pack("<Q", low)


# --- packet types --------------------------------------------------------- #
def test_typing_and_view_time_are_not_confused():
    """174 was mislabelled 'typing stopped'; 175 is read receipts, not typing."""
    assert PacketType.from_case(174) is PacketType.MESSAGE_SET_TYPING_INDICATOR
    assert PacketType.from_case(175) is PacketType.MESSAGE_SET_VIEW_TIME


def test_packet_enum_covers_the_protocol():
    assert len(list(PacketType)) == 91
    for case in (120, 150, 173, 180, 210):
        assert PacketType.from_case(case) is not PacketType.UNKNOWN


def test_every_schema_key_matches_a_packet_type():
    names = {p.name for p in PacketType}
    unknown = sorted(set(PACKET_SCHEMAS) - names)
    assert not unknown, f"schema keys with no PacketType: {unknown}"


def test_decode_packet_reaction():
    payload = (
        _msg(3, _uuid(0x11, 0x22))
        + _msg(4, _uuid(0x33, 0x44))
        + _str(5, ":heart:")
        + _msg(6, _uuid(0x55, 0x66))
        + _msg(7, _uuid(0x77, 0x88))
    )
    fields = decode_packet("MESSAGE_REACTION", payload)
    assert fields["shortcode"] == ":heart:"
    assert fields["user_id"].count("-") == 4


def test_decode_packet_message_content_and_repeated():
    payload = _str(10, "hello world") + _msg(13, b"\x01") + _msg(13, b"\x02")
    fields = decode_packet("MESSAGE", payload)
    assert fields["message_content"] == "hello world"
    assert isinstance(fields["message_uris"], list)
    assert len(fields["message_uris"]) == 2


# --- structured errors ---------------------------------------------------- #
def test_error_code_is_named():
    exc = _tag(10, 0) + _varint(1010)
    info = decode_root_exception(exc)
    assert info.error_code is ErrorCodeType.NO_PERMISSION_TO_BAN


def test_unknown_error_code_degrades_to_int():
    info = decode_root_exception(_tag(10, 0) + _varint(88888))
    assert info.error_code == 88888


def test_validation_errors_decode_per_field():
    err1 = _str(10, "username") + _str(11, "too short") + _str(12, "TOO_SHORT")
    err2 = _str(10, "email") + _str(11, "invalid") + _str(12, "BAD_FORMAT")
    payload = _msg(16, _msg(15, _msg(10, err1) + _msg(10, err2)))
    info = decode_root_exception(payload)
    assert info.payload_kind == "request_validator_list"
    assert [e.property_name for e in info.validation_errors] == ["username", "email"]
    assert info.validation_errors[0].error_code == "TOO_SHORT"


def test_content_flag_reason_enum():
    assert int(ContentFlagReason.SPAM) == 4


class TestRootGuidTimestamps:
    """Root ids carry their own creation time -- the archive relies on it.

    ``create_root_guid`` packs milliseconds since 2020-01-01 UTC into the top
    48 bits, and the desktop client reads them back the same way
    (``MessageGuid.ToDateTime()``). That makes a backfilled message and a
    pushed one comparable on one clock, which is the difference between an
    archive that sorts and one that only knows when it happened to look.
    """

    def test_it_round_trips_a_guid_we_minted(self):
        from datetime import datetime, timezone

        from rootpy.identifiers import (
            ROOT_GUID_TYPE_DESKTOP,
            create_root_guid,
            format_root_guid,
            root_guid_datetime,
        )

        before = datetime.now(timezone.utc)
        identifier = format_root_guid(*create_root_guid(ROOT_GUID_TYPE_DESKTOP))
        after = datetime.now(timezone.utc)
        when = root_guid_datetime(identifier)
        assert before.replace(microsecond=0) <= when <= after

    def test_a_real_id_reads_as_the_date_it_was_made(self):
        """A community id captured live, checked against its known date."""
        from rootpy.identifiers import root_guid_datetime

        when = root_guid_datetime("0030b367-6f52-8702-b0a1-aa5c7b5c338c")
        assert when.year == 2026 and when.month == 8 and when.day == 17

    def test_the_type_byte_says_what_the_id_is(self):
        from rootpy.identifiers import root_guid_type

        # 1 person, 2 community, 4 channel -- from the client's RootGuidType.
        assert root_guid_type("0030bf26-999d-8101-82fd-66b280480024") == 1
        assert root_guid_type("0030b367-6f52-8702-b0a1-aa5c7b5c338c") == 2
        assert root_guid_type("0030b367-6f52-8904-b9ce-4d0692862c50") == 4

    def test_a_compact_id_reads_the_same_as_its_dashed_form(self):
        import base64
        import uuid

        from rootpy.identifiers import root_guid_datetime

        dashed = "0030b367-6f52-8702-b0a1-aa5c7b5c338c"
        compact = base64.urlsafe_b64encode(
            uuid.UUID(dashed).bytes
        ).decode().rstrip("=")
        assert root_guid_datetime(compact) == root_guid_datetime(dashed)

    def test_nonsense_raises_rather_than_inventing_a_date(self):
        import pytest

        from rootpy.identifiers import root_guid_datetime

        with pytest.raises(ValueError):
            root_guid_datetime("not-a-guid")
