"""Community Discovery -- ``root.CommunityDiscoveryGrpcService``.

Two methods: browse the public community directory, and join a community
straight out of it without an invite.

The field numbers here are not 1..N. Root reserves the low numbers for
envelope fields -- ``context`` is 1 -- and starts the payload at **10**.
``client.explain("community_discovery.search")`` prints the exact request
fields offline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from ..enums import (
    CommunityCategory,
    CommunityDiscoverableRequirement,
    CommunityDiscoverySort,
)
from ..identifiers import encode_root_guid, normalize_root_guid
from ..models import DiscoveredCommunity
from ..protocol import (
    decode_root_guid_message,
    decode_timestamp_message,
    encode_varint,
    field_key,
    grpc_frame,
    iter_fields,
    length_field,
    string_field,
    unwrap_grpc_web,
)
from ..transport import GrpcWebTransport

SEARCH = (
    "https://api.rootapp.com/"
    "root.CommunityDiscoveryGrpcService/Search"
)

JOIN = (
    "https://api.rootapp.com/"
    "root.CommunityDiscoveryGrpcService/Join"
)

#: ``Limit`` here is an ordinary int32 that Root does not range-check, unlike
#: ``MessageList.Limit``, which is validated at 10-50. 30 is a reasonable
#: page. Nothing is enforced locally beyond refusing nonsense.
DEFAULT_LIMIT = 30


def _varint_field(number: int, value: int) -> bytes:
    return field_key(number, 0) + encode_varint(int(value))


class CommunityDiscoveryService:
    """Browse and join public communities."""

    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter: Callable[[], str],
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter

    def _headers(self) -> dict[str, str]:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "grpc-timeout": "30S",
            "authorization": f"Bearer {self._token_getter()}",
            "content-type": "application/grpc-web",
            "accept": "application/grpc-web",
        }

    # -- decoding ----------------------------------------------------------
    @staticmethod
    def _parse_result(data: bytes) -> DiscoveredCommunity:
        """One ``CommunityDiscoveryResult``.

        Fields 12-14 are ``google.protobuf.StringValue``, so each arrives as a
        nested message with the string at field 1 rather than as a bare
        string. Absent and empty are different things for a wrapper, which is
        why these stay ``None`` when the server omits them.
        """
        out: dict = {"raw": data}
        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2:
                out["id"] = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2:
                out["name"] = value.decode("utf-8", "replace")
            elif number in (12, 13, 14) and wire_type == 2:
                inner = None
                for n, wt, v in iter_fields(value):
                    if n == 1 and wt == 2:
                        inner = v.decode("utf-8", "replace")
                out[{12: "description",
                     13: "picture_asset_uri",
                     14: "banner_asset_uri"}[number]] = inner
            elif number == 15 and wire_type == 0:
                out["category"] = CommunityCategory.coerce(value)
            elif number == 16 and wire_type == 0:
                out["member_count"] = value
            elif number == 17 and wire_type == 0:
                out["app_count"] = value
            elif number == 18 and wire_type == 0:
                out["core_count"] = value
            elif number == 19 and wire_type == 0:
                out["is_discoverable"] = bool(value)
            elif number == 20 and wire_type == 0:
                out["is_discoverable_status"] = (
                    CommunityDiscoverableRequirement.coerce(value)
                )
            elif number == 21 and wire_type == 2:
                out["indexed_at"] = decode_timestamp_message(value)
        out.setdefault("id", None)
        out.setdefault("name", "")
        return DiscoveredCommunity(**out)

    # -- calls -------------------------------------------------------------
    async def search(
        self,
        search: str = "",
        *,
        category: Optional[int] = None,
        sort_by: Optional[int] = None,
        sort_ascending: bool = False,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[tuple[DiscoveredCommunity, ...], int]:
        """Search the public directory.

        Returns ``(results, total)``. ``total`` is the size of the whole match
        set, not of this page, so it is what to page against rather than
        ``len(results)``.

        Every argument is optional: an empty search with no category lists the
        directory. ``offset``/``limit`` page it.
        """
        if limit < 0 or offset < 0:
            raise ValueError("offset and limit must not be negative")

        payload = bytearray()
        if search:
            payload += string_field(10, search)
        if category is not None:
            payload += _varint_field(11, int(category))
        if sort_by is not None:
            payload += _varint_field(12, int(sort_by))
        if sort_ascending:
            payload += _varint_field(13, 1)
        if offset:
            payload += _varint_field(14, offset)
        if limit:
            payload += _varint_field(15, limit)

        response = await self.transport.unary(
            endpoint=SEARCH,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation="CommunityDiscoverySearch",
        )

        body = unwrap_grpc_web(response.content) or b""
        results: list[DiscoveredCommunity] = []
        total = 0
        for number, wire_type, value in iter_fields(body):
            if number == 10 and wire_type == 2:
                results.append(self._parse_result(value))
            elif number == 11 and wire_type == 0:
                total = value
        return tuple(results), total

    async def join(
        self,
        community_id: str,
        *,
        is_age_verified: bool = False,
    ) -> None:
        """Join a community straight from the directory.

        ``is_age_verified`` is the caller asserting it. It is sent only when
        true; the server reads an absent proto3 bool as false either way.
        """
        community_id = normalize_root_guid(community_id)

        payload = bytearray()
        payload += length_field(10, encode_root_guid(community_id))
        if is_age_verified:
            payload += _varint_field(11, 1)

        await self.transport.unary(
            endpoint=JOIN,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation="CommunityDiscoveryJoin",
        )
