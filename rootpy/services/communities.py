from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Optional

from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    normalize_root_guid,
)
from ..models import (
    Channel,
    ChannelGroup,
    Community,
    CommunityExtended,
    CommunityMember,
    CommunityRole,
)
from ..permissions import ChannelPermissions, CommunityPermission
from ..protocol import (
    decode_root_guid_message,
    decode_timestamp_message,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    length_field,
)
from ..transport import GrpcWebTransport

LIST_MINE = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/ListMine"
)

GET_EXTENDED = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/GetExtended"
)

LEAVE = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/Leave"
)

ATTACH = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/Attach"
)

DETACH = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/Detach"
)

DETACH_MANY = (
    "https://api.rootapp.com/"
    "root.CommunityGrpcService/DetachMany"
)


def _decode_wrapped_string(data: bytes) -> Optional[str]:
    for number, wire_type, value in iter_fields(data):
        if number == 1 and wire_type == 2:
            return value.decode("utf-8", errors="replace")
    return None


def _decode_float32(value: object) -> float:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 4:
        return 0.0
    return struct.unpack("<f", value)[0]


def _parse_permissions(data: bytes) -> ChannelPermissions:
    values = {}
    fields = {
        10: "channel_full_control",
        12: "channel_view",
        13: "channel_use_external_emoji",
        14: "channel_create_message",
        15: "channel_delete_message_other",
        16: "channel_manage_pinned_messages",
        17: "channel_view_message_history",
        18: "channel_create_message_attachment",
        19: "channel_create_message_mention",
        20: "channel_create_message_reaction",
        21: "channel_make_message_public",
        22: "channel_move_user_other",
        23: "channel_voice_talk",
        24: "channel_voice_mute_other",
        25: "channel_voice_deafen_other",
        26: "channel_voice_kick",
        27: "channel_video_stream_media",
        28: "channel_create_file",
        29: "channel_manage_files",
        30: "channel_view_file",
        31: "channel_app_kick",
    }
    for number, wire_type, value in iter_fields(data):
        if wire_type == 0 and number in fields:
            values[fields[number]] = bool(value)
    return ChannelPermissions(**values)


def _parse_community_permissions(data: bytes) -> CommunityPermission:
    values = {}
    fields = {
        10: "community_manage_community",
        11: "community_manage_roles",
        12: "community_manage_emojis",
        13: "community_manage_audit_log",
        14: "community_create_invite",
        15: "community_manage_invites",
        16: "community_create_ban",
        17: "community_manage_bans",
        18: "community_full_control",
        19: "community_kick",
        20: "community_change_my_nickname",
        21: "community_change_other_nickname",
        22: "community_create_channel_group",
        23: "community_manage_apps",
    }
    for number, wire_type, value in iter_fields(data):
        if wire_type == 0 and number in fields:
            values[fields[number]] = bool(value)
    return CommunityPermission(**values)


def _parse_ids(data: bytes, field_number: int) -> tuple[str, ...]:
    result = []
    for number, wire_type, value in iter_fields(data):
        if number == field_number and wire_type == 2:
            decoded = decode_root_guid_message(value)
            if decoded:
                result.append(decoded)
    return tuple(result)


class CommunityService:
    def __init__(self, transport: GrpcWebTransport, token_getter: Callable[[], str]) -> None:
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

    @staticmethod
    def _context() -> bytes:
        high64, low64 = create_command_idempotency_guid()
        return length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )

    @staticmethod
    def _parse_member(data: bytes) -> Optional[CommunityMember]:
        user_id = None
        roles = []
        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2:
                user_id = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2:
                role_id = decode_root_guid_message(value)
                if role_id:
                    roles.append(role_id)
        if not user_id:
            return None
        return CommunityMember(user_id, tuple(roles), data)

    @staticmethod
    def _parse_role(data: bytes) -> Optional[CommunityRole]:
        role_id = None
        channel_permissions = ChannelPermissions()
        community_permissions = CommunityPermission()
        channel_permission_raw = b""
        community_permission_raw = b""
        name = ""
        color_hex = ""
        position = 0.0
        is_mentionable = False
        is_self_assignable = False

        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2:
                role_id = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2:
                channel_permission_raw = value
                channel_permissions = _parse_permissions(value)
            elif number == 12 and wire_type == 2:
                community_permission_raw = value
                community_permissions = _parse_community_permissions(value)
            elif number == 13 and wire_type == 2:
                name = value.decode("utf-8", errors="replace")
            elif number == 14 and wire_type == 2:
                color_hex = value.decode("utf-8", errors="replace")
            elif number == 15 and wire_type == 5:
                position = _decode_float32(value)
            elif number == 16 and wire_type == 0:
                is_mentionable = bool(value)
            elif number == 17 and wire_type == 0:
                is_self_assignable = bool(value)

        if not role_id:
            return None

        return CommunityRole(
            id=role_id,
            permissions=channel_permissions,
            name=name,
            position=position,
            raw=data,
            community_permissions=community_permissions,
            color_hex=color_hex,
            is_mentionable=is_mentionable,
            is_self_assignable=is_self_assignable,
            channel_permission_raw=channel_permission_raw,
            community_permission_raw=community_permission_raw,
        )


    @staticmethod
    def _parse_channel(data: bytes) -> Optional[Channel]:
        values = {"name": "", "position": 0.0, "channel_type": 0}
        permissions = ChannelPermissions()
        role_or_member_ids = []
        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2: values["community_id"] = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2: values["channel_group_id"] = decode_root_guid_message(value)
            elif number == 12 and wire_type == 2: values["id"] = decode_root_guid_message(value)
            elif number == 13 and wire_type == 2: values["name"] = value.decode("utf-8", errors="replace")
            elif number == 14 and wire_type == 2: values["description"] = _decode_wrapped_string(value)
            elif number == 15 and wire_type == 2: values["icon_asset_uri"] = _decode_wrapped_string(value)
            elif number == 16 and wire_type == 0: values["channel_type"] = int(value)
            elif number == 17 and wire_type == 5: values["position"] = _decode_float32(value)
            elif number == 18 and wire_type == 2: values["community_app_id"] = decode_root_guid_message(value)
            elif number == 19 and wire_type == 2: permissions = _parse_permissions(value)
            elif number == 20 and wire_type == 2: values["last_activity_at"] = decode_timestamp_message(value)
            elif number == 21 and wire_type == 2: values["user_last_viewed_at"] = decode_timestamp_message(value)
            elif number == 22 and wire_type == 0: values["use_channel_group_permission"] = bool(value)
            elif number == 24 and wire_type == 2:
                item = decode_root_guid_message(value)
                if item: role_or_member_ids.append(item)
        if not values.get("community_id") or not values.get("id"):
            return None
        return Channel(permissions=permissions, role_or_member_ids=tuple(role_or_member_ids), raw=data, **values)

    @classmethod
    def _parse_channel_group(cls, data: bytes) -> Optional[ChannelGroup]:
        community_id = group_id = None
        name = ""
        position = 0.0
        permissions = ChannelPermissions()
        channels = []
        ids = []
        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2: community_id = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2: group_id = decode_root_guid_message(value)
            elif number == 12 and wire_type == 2: name = value.decode("utf-8", errors="replace")
            elif number == 13 and wire_type == 5: position = _decode_float32(value)
            elif number == 14 and wire_type == 2: permissions = _parse_permissions(value)
            elif number == 15 and wire_type == 2:
                channel = cls._parse_channel(value)
                if channel: channels.append(channel)
            elif number == 16 and wire_type == 2:
                item = decode_root_guid_message(value)
                if item: ids.append(item)
        if not community_id or not group_id:
            return None
        return ChannelGroup(group_id, community_id, name, position, permissions, tuple(ids), tuple(channels), data)

    @classmethod
    def _parse_response(cls, body: bytes) -> CommunityExtended:
        payload = next((frame for flag, frame in iter_grpc_web_frames(body) if not flag & 0x80), None)
        if payload is None:
            raise RuntimeError("Community GetExtended returned no protobuf message")
        values = {
            "name": "",
            "picture_hex": "",
            "owner_user_id": None,
            "default_channel_id": None,
            "picture_asset_uri": None,
            "description": None,
            "reject_unverified_email": False,
            "is_age_restricted": False,
        }
        groups = []
        members = []
        roles = []
        for number, wire_type, value in iter_fields(payload):
            if number == 10 and wire_type == 2: values["id"] = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2: values["name"] = value.decode("utf-8", errors="replace")
            elif number == 12 and wire_type == 2: values["picture_asset_uri"] = _decode_wrapped_string(value)
            elif number == 13 and wire_type == 2: values["picture_hex"] = value.decode("utf-8", errors="replace")
            elif number == 14 and wire_type == 2: values["owner_user_id"] = decode_root_guid_message(value)
            elif number == 15 and wire_type == 2: values["default_channel_id"] = decode_root_guid_message(value)
            elif number == 17 and wire_type == 2:
                member = cls._parse_member(value)
                if member: members.append(member)
            elif number == 18 and wire_type == 2:
                role = cls._parse_role(value)
                if role: roles.append(role)
            elif number == 19 and wire_type == 2:
                group = cls._parse_channel_group(value)
                if group: groups.append(group)
            elif number == 22 and wire_type == 0: values["reject_unverified_email"] = bool(value)
            elif number == 24 and wire_type == 2: values["description"] = _decode_wrapped_string(value)
            elif number == 25 and wire_type == 0: values["is_age_restricted"] = bool(value)
        if not values.get("id"):
            raise RuntimeError("Community GetExtended returned no community ID")
        community = Community(raw=payload, **values)
        return CommunityExtended(community, tuple(groups), tuple(members), tuple(roles), payload)


    @staticmethod
    def _parse_community_packet(data: bytes) -> Optional[Community]:
        values = {
            number: (wire_type, value)
            for number, wire_type, value in iter_fields(data)
        }

        def guid(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        def text(number: int) -> str:
            item = values.get(number)
            if item is None or item[0] != 2:
                return ""
            return item[1].decode("utf-8", errors="replace")

        def wrapped(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return _decode_wrapped_string(item[1])

        def integer(number: int) -> int:
            item = values.get(number)
            if item is None or item[0] != 0:
                return 0
            return int(item[1])

        community_id = guid(3)
        if not community_id:
            return None

        return Community(
            id=community_id,
            owner_user_id=guid(4),
            default_channel_id=guid(5),
            name=text(6),
            picture_hex=text(7),
            picture_asset_uri=wrapped(8),
            reject_unverified_email=bool(integer(9)),
            description=wrapped(11),
            is_age_restricted=bool(integer(17)),
            packet_type=integer(1),
            raw=data,
        )

    @classmethod
    def _parse_list_mine_response(
        cls,
        body: bytes,
    ) -> tuple[Community, ...]:
        payload = next(
            (
                frame
                for flag, frame in iter_grpc_web_frames(body)
                if not flag & 0x80
            ),
            None,
        )
        if payload is None:
            return ()

        communities = []
        for _, wire_type, member_data in iter_fields(payload):
            if wire_type != 2:
                continue

            for number, inner_wire, community_data in iter_fields(
                member_data
            ):
                if number != 10 or inner_wire != 2:
                    continue
                community = cls._parse_community_packet(community_data)
                if community is not None:
                    communities.append(community)
                break
        return tuple(communities)


    async def leave(self, community_id: str) -> None:
        """Leave a community as the currently authenticated user."""
        community_id = normalize_root_guid(community_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(
            10,
            encode_root_guid(community_id),
        )

        await self.transport.unary(
            endpoint=LEAVE,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation="CommunityLeave",
        )

    async def attach(self, community_id: str) -> None:
        """Subscribe this account's hub connections to a community.

        Membership alone delivers nothing. Until a community is attached the
        hub sends no packet for it -- not a message, not a channel edit --
        while user-scoped traffic (DMs, notifications, status) arrives
        normally. That asymmetry is why a bot can look perfectly connected and
        still never see a channel post.

        The desktop client does this from its full-load path
        (``Community.attachAsync`` runs off
        ``UpdateFromCommunityExtendedResponse``) and undoes it in
        ``FullyUnload``, which is why it only receives live traffic for
        communities it has actually opened.

        Attaching is **visible to other members**: the server broadcasts
        ``COMMUNITY_MEMBER_ATTACH`` (5502), and clients show attached members
        as present. Use :meth:`detach` to go back.

        The subscription is per hub connection, so it has to be re-established
        after a reconnect -- :class:`~rootpy.unread.UnreadReader` does that for
        you.
        """
        community_id = normalize_root_guid(community_id)
        await self.transport.unary(
            endpoint=ATTACH,
            body=grpc_frame(length_field(10, encode_root_guid(community_id))),
            headers=self._headers(),
            operation="CommunityAttach",
        )

    async def detach(self, community_id: str) -> None:
        """Stop receiving a community's packets, and stop appearing present."""
        community_id = normalize_root_guid(community_id)
        await self.transport.unary(
            endpoint=DETACH,
            body=grpc_frame(length_field(10, encode_root_guid(community_id))),
            headers=self._headers(),
            operation="CommunityDetach",
        )

    async def detach_many(self, community_ids) -> None:
        """Detach from several communities in one request.

        ``CommunityDetachManyRequest`` repeats field 10, so this is one round
        trip instead of one per community. An empty sequence sends nothing.
        """
        payload = bytearray()
        for community_id in community_ids:
            payload += length_field(
                10,
                encode_root_guid(normalize_root_guid(community_id)),
            )
        if not payload:
            return
        await self.transport.unary(
            endpoint=DETACH_MANY,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation="CommunityDetachMany",
        )

    async def list_mine(self) -> tuple[Community, ...]:
        response = await self.transport.unary(
            endpoint=LIST_MINE,
            body=grpc_frame(b""),
            headers={
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
            },
            operation="CommunityListMine",
        )
        return self._parse_list_mine_response(response.content)

    async def get_extended(self, community_id: str) -> CommunityExtended:
        community_id = normalize_root_guid(community_id)
        response = await self.transport.unary(
            endpoint=GET_EXTENDED,
            body=grpc_frame(length_field(10, encode_root_guid(community_id))),
            headers={
                "user-agent": "grpc-dotnet/2.83.0 (.NET 10.0.10; CLR 10.0.10; net10.0; windows; x64)",
                "te": "trailers",
                "grpc-accept-encoding": "identity,gzip,deflate",
                "grpc-timeout": "30S",
                "authorization": f"Bearer {self._token_getter()}",
                "content-type": "application/grpc-web",
            },
            operation="CommunityGetExtended",
        )
        return self._parse_response(response.content)
