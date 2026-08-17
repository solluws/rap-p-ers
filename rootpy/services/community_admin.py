from __future__ import annotations

from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Tuple

from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    normalize_root_guid,
)
from ..models import Channel, ChannelGroup, Community, CommunityRole
from ..permissions import (
    AccessRule,
    ChannelOverlay,
    ChannelPermissions,
    CommunityPermission,
)
from ..protocol import (
    bool_field,
    decode_root_guid_message,
    encode_varint,
    field_key,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    length_field,
)
from ..transport import GrpcWebTransport


BASE = "https://api.rootapp.com"

COMMUNITY_CREATE = f"{BASE}/root.CommunityGrpcService/Create"
COMMUNITY_EDIT = f"{BASE}/root.CommunityGrpcService/Edit"
COMMUNITY_DELETE = f"{BASE}/root.CommunityGrpcService/Delete"

CHANNEL_GROUP_CREATE = f"{BASE}/root.ChannelGroupGrpcService/Create"
CHANNEL_GROUP_EDIT = f"{BASE}/root.ChannelGroupGrpcService/Edit"
CHANNEL_GROUP_MOVE = f"{BASE}/root.ChannelGroupGrpcService/Move"
CHANNEL_GROUP_DELETE = f"{BASE}/root.ChannelGroupGrpcService/Delete"

CHANNEL_CREATE = f"{BASE}/root.ChannelGrpcService/Create"
CHANNEL_EDIT = f"{BASE}/root.ChannelGrpcService/Edit"
CHANNEL_MOVE = f"{BASE}/root.ChannelGrpcService/Move"
CHANNEL_DELETE = f"{BASE}/root.ChannelGrpcService/Delete"

ROLE_CREATE = f"{BASE}/root.CommunityRoleGrpcService/Create"
ROLE_EDIT = f"{BASE}/root.CommunityRoleGrpcService/Edit"
ROLE_MOVE = f"{BASE}/root.CommunityRoleGrpcService/Move"
ROLE_DELETE = f"{BASE}/root.CommunityRoleGrpcService/Delete"

ACCESS_RULE_CREATE = f"{BASE}/root.AccessRuleGrpcService/Create"
ACCESS_RULE_EDIT = f"{BASE}/root.AccessRuleGrpcService/Edit"
ACCESS_RULE_DELETE = f"{BASE}/root.AccessRuleGrpcService/Delete"
ACCESS_RULE_LIST_TARGET = (
    f"{BASE}/root.AccessRuleGrpcService/ListByChannelOrChannelGroup"
)


_CHANNEL_PERMISSION_FIELDS = {
    "channel_full_control": 10,
    "channel_view": 12,
    "channel_use_external_emoji": 13,
    "channel_create_message": 14,
    "channel_delete_message_other": 15,
    "channel_manage_pinned_messages": 16,
    "channel_view_message_history": 17,
    "channel_create_message_attachment": 18,
    "channel_create_message_mention": 19,
    "channel_create_message_reaction": 20,
    "channel_make_message_public": 21,
    "channel_move_user_other": 22,
    "channel_voice_talk": 23,
    "channel_voice_mute_other": 24,
    "channel_voice_deafen_other": 25,
    "channel_voice_kick": 26,
    "channel_video_stream_media": 27,
    "channel_create_file": 28,
    "channel_manage_files": 29,
    "channel_view_file": 30,
    "channel_app_kick": 31,
}

_COMMUNITY_PERMISSION_FIELDS = {
    "community_manage_community": 10,
    "community_manage_roles": 11,
    "community_manage_emojis": 12,
    "community_manage_audit_log": 13,
    "community_create_invite": 14,
    "community_manage_invites": 15,
    "community_create_ban": 16,
    "community_manage_bans": 17,
    "community_full_control": 18,
    "community_kick": 19,
    "community_change_my_nickname": 20,
    "community_change_other_nickname": 21,
    "community_create_channel_group": 22,
    "community_manage_apps": 23,
}


def _string_wrapper(value: str) -> bytes:
    return length_field(1, value.encode("utf-8"))


def _encode_bool_permissions(obj, mapping: Dict[str, int]) -> bytes:
    payload = bytearray()
    for name, number in mapping.items():
        if bool(getattr(obj, name)):
            payload += bool_field(number, True)
    return bytes(payload)


def _encode_overlay(overlay: ChannelOverlay) -> bytes:
    payload = bytearray()
    for name, number in _CHANNEL_PERMISSION_FIELDS.items():
        value = getattr(overlay, name)
        if value is None:
            continue
        wrapper = bool_field(1, True) if value else b""
        payload += length_field(number, wrapper)
    return bytes(payload)


def _decode_overlay(data: bytes) -> ChannelOverlay:
    values = {}
    inverse = {v: k for k, v in _CHANNEL_PERMISSION_FIELDS.items()}
    for number, wire_type, value in iter_fields(data):
        if number not in inverse or wire_type != 2:
            continue
        flag = False
        for inner_number, inner_wire, inner_value in iter_fields(value):
            if inner_number == 1 and inner_wire == 0:
                flag = bool(inner_value)
                break
        values[inverse[number]] = flag
    return ChannelOverlay(**values)


class CommunityAdminService:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter,
        community_service,
        *,
        client=None,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self.community_service = community_service
        self.client = client

    def _headers(self) -> dict:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
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

    async def _unary(self, endpoint: str, payload: bytes, operation: str):
        return await self.transport.unary(
            endpoint=endpoint,
            body=grpc_frame(payload),
            headers=self._headers(),
            operation=operation,
        )

    @staticmethod
    def _first_guid(body: bytes, field_number: int) -> Optional[str]:
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == field_number and wire_type == 2:
                    return decode_root_guid_message(value)
            break
        return None

    def _attach(self, obj):
        if obj is None:
            return None
        try:
            return replace(obj, _admin=self)
        except TypeError:
            return obj

    async def create_community(
        self,
        name: str,
        *,
        picture_hex: str = "",
        icon_upload_token_uri: Optional[str] = None,
        description: Optional[str] = None,
        reject_unverified_email: bool = False,
        is_age_restricted: bool = False,
        template_type: str = "",
    ) -> Community:
        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, name.encode("utf-8"))
        if picture_hex:
            payload += length_field(11, picture_hex.encode("utf-8"))
        if template_type:
            payload += length_field(12, template_type.encode("utf-8"))
        if icon_upload_token_uri is not None:
            payload += length_field(
                13,
                _string_wrapper(icon_upload_token_uri),
            )
        if reject_unverified_email:
            payload += bool_field(14, True)
        if description is not None:
            payload += length_field(16, _string_wrapper(description))
        if is_age_restricted:
            payload += bool_field(17, True)

        response = await self._unary(
            COMMUNITY_CREATE,
            bytes(payload),
            "CommunityCreate",
        )
        community_id = self._first_guid(response.content, 4)
        if not community_id:
            raise RuntimeError("CommunityCreate returned no community ID")

        result = await self.community_service.get_extended(community_id)
        return self._attach(result.community)

    async def edit_community(
        self,
        community_id: str,
        *,
        name: Optional[str] = None,
        picture_hex: Optional[str] = None,
        picture_token_uri: Optional[str] = None,
        update_picture: bool = False,
        default_channel_id: Optional[str] = None,
        reject_unverified_email: Optional[bool] = None,
        description: Optional[str] = None,
        is_age_restricted: Optional[bool] = None,
    ) -> Community:
        community_id = normalize_root_guid(community_id)
        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))

        if name is not None:
            payload += length_field(11, name.encode("utf-8"))
        if picture_hex is not None:
            payload += length_field(12, picture_hex.encode("utf-8"))
        if update_picture:
            payload += bool_field(13, True)
        if picture_token_uri is not None:
            payload += length_field(14, _string_wrapper(picture_token_uri))
        if default_channel_id is not None:
            payload += length_field(
                15,
                encode_root_guid(normalize_root_guid(default_channel_id)),
            )
        if reject_unverified_email:
            payload += bool_field(16, True)
        if description is not None:
            payload += length_field(18, _string_wrapper(description))
        if is_age_restricted:
            payload += bool_field(19, True)

        await self._unary(
            COMMUNITY_EDIT,
            bytes(payload),
            "CommunityEdit",
        )
        result = await self.community_service.get_extended(community_id)
        return self._attach(result.community)

    async def delete_community(self, community_id: str) -> None:
        community_id = normalize_root_guid(community_id)
        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
        )
        await self._unary(
            COMMUNITY_DELETE,
            payload,
            "CommunityDelete",
        )

    @staticmethod
    def _access_rule_create_payload(rule: AccessRule) -> bytes:
        return (
            length_field(
                10,
                encode_root_guid(
                    normalize_root_guid(rule.target_id)
                ),
            )
            + length_field(
                11,
                _encode_overlay(rule.overlay),
            )
        )

    async def create_channel_group(
        self,
        community_id: str,
        name: str,
        *,
        access_rules: Optional[Iterable[AccessRule]] = None,
    ) -> ChannelGroup:
        community_id = normalize_root_guid(community_id)
        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, name.encode("utf-8"))
        for rule in access_rules or ():
            payload += length_field(
                12,
                self._access_rule_create_payload(rule),
            )

        response = await self._unary(
            CHANNEL_GROUP_CREATE,
            bytes(payload),
            "ChannelGroupCreate",
        )
        group_id = self._first_guid(response.content, 5)
        if not group_id:
            raise RuntimeError("ChannelGroupCreate returned no ID")

        extended = await self.community_service.get_extended(community_id)
        for group in extended.channel_groups:
            if group.id == group_id:
                return self._attach(group)
        raise RuntimeError("Created channel group was not returned by GetExtended")

    async def edit_channel_group(
        self,
        community_id: str,
        group_id: str,
        *,
        name: str,
    ) -> ChannelGroup:
        community_id = normalize_root_guid(community_id)
        group_id = normalize_root_guid(group_id)
        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(group_id))
            + length_field(12, name.encode("utf-8"))
        )
        await self._unary(
            CHANNEL_GROUP_EDIT,
            payload,
            "ChannelGroupEdit",
        )
        extended = await self.community_service.get_extended(community_id)
        for group in extended.channel_groups:
            if group.id == group_id:
                return self._attach(group)
        raise RuntimeError("Edited channel group was not returned by GetExtended")

    async def move_channel_group(
        self,
        community_id: str,
        group_id: str,
        *,
        before_group_id: Optional[str] = None,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        group_id = normalize_root_guid(group_id)
        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(group_id))
        if before_group_id is not None:
            payload += length_field(
                12,
                encode_root_guid(normalize_root_guid(before_group_id)),
            )
        await self._unary(
            CHANNEL_GROUP_MOVE,
            bytes(payload),
            "ChannelGroupMove",
        )

    async def delete_channel_group(
        self,
        community_id: str,
        group_id: str,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        group_id = normalize_root_guid(group_id)
        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(group_id))
        )
        await self._unary(
            CHANNEL_GROUP_DELETE,
            payload,
            "ChannelGroupDelete",
        )

    async def create_channel(
        self,
        community_id: str,
        channel_group_id: str,
        name: str,
        *,
        description: Optional[str] = None,
        channel_type: int = 0,
        use_channel_group_permission: bool = False,
        icon_token_uri: Optional[str] = None,
        access_rules: Optional[Iterable[AccessRule]] = None,
    ) -> Channel:
        community_id = normalize_root_guid(community_id)
        channel_group_id = normalize_root_guid(channel_group_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(channel_group_id))
        payload += length_field(12, name.encode("utf-8"))
        if description is not None:
            payload += length_field(13, _string_wrapper(description))
        if channel_type:
            payload += field_key(14, 0) + encode_varint(channel_type)
        if use_channel_group_permission:
            payload += bool_field(15, True)
        if icon_token_uri is not None:
            payload += length_field(16, _string_wrapper(icon_token_uri))
        for rule in access_rules or ():
            payload += length_field(
                17,
                self._access_rule_create_payload(rule),
            )

        response = await self._unary(
            CHANNEL_CREATE,
            bytes(payload),
            "ChannelCreate",
        )
        channel_id = self._first_guid(response.content, 6)
        if not channel_id:
            raise RuntimeError("ChannelCreate returned no ID")

        extended = await self.community_service.get_extended(community_id)
        for group in extended.channel_groups:
            for channel in group.channels:
                if channel.id == channel_id:
                    return self._attach(channel)
        raise RuntimeError("Created channel was not returned by GetExtended")

    async def edit_channel(
        self,
        community_id: str,
        channel_id: str,
        *,
        name: str,
        description: Optional[str] = None,
        update_icon: bool = False,
        icon_token_uri: Optional[str] = None,
        use_channel_group_permission: bool = False,
    ) -> Channel:
        community_id = normalize_root_guid(community_id)
        channel_id = normalize_root_guid(channel_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(channel_id))
        payload += length_field(12, name.encode("utf-8"))
        if description is not None:
            payload += length_field(13, _string_wrapper(description))
        if update_icon:
            payload += bool_field(14, True)
        if icon_token_uri is not None:
            payload += length_field(15, _string_wrapper(icon_token_uri))
        if use_channel_group_permission:
            payload += bool_field(16, True)

        await self._unary(
            CHANNEL_EDIT,
            bytes(payload),
            "ChannelEdit",
        )
        extended = await self.community_service.get_extended(community_id)
        for group in extended.channel_groups:
            for channel in group.channels:
                if channel.id == channel_id:
                    return self._attach(channel)
        raise RuntimeError("Edited channel was not returned by GetExtended")

    async def move_channel(
        self,
        community_id: str,
        channel_id: str,
        *,
        old_group_id: Optional[str] = None,
        new_group_id: Optional[str] = None,
        before_channel_id: Optional[str] = None,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        channel_id = normalize_root_guid(channel_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(channel_id))

        if old_group_id is not None:
            payload += length_field(
                12,
                encode_root_guid(normalize_root_guid(old_group_id)),
            )
        if new_group_id is not None:
            payload += length_field(
                13,
                encode_root_guid(normalize_root_guid(new_group_id)),
            )
        if before_channel_id is not None:
            payload += length_field(
                14,
                encode_root_guid(normalize_root_guid(before_channel_id)),
            )

        await self._unary(
            CHANNEL_MOVE,
            bytes(payload),
            "ChannelMove",
        )

    async def delete_channel(
        self,
        community_id: str,
        channel_id: str,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        channel_id = normalize_root_guid(channel_id)
        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(channel_id))
        )
        await self._unary(
            CHANNEL_DELETE,
            payload,
            "ChannelDelete",
        )

    async def create_role(
        self,
        community_id: str,
        name: str,
        *,
        color_hex: Optional[str] = None,
        community_permissions: Optional[CommunityPermission] = None,
        channel_permissions: Optional[ChannelPermissions] = None,
        mentionable: bool = False,
        self_assignable: bool = False,
    ) -> CommunityRole:
        community_id = normalize_root_guid(community_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, name.encode("utf-8"))

        if color_hex is not None:
            payload += length_field(12, _string_wrapper(color_hex))
        if community_permissions is not None:
            payload += length_field(
                15,
                _encode_bool_permissions(
                    community_permissions,
                    _COMMUNITY_PERMISSION_FIELDS,
                ),
            )
        if channel_permissions is not None:
            payload += length_field(
                16,
                _encode_bool_permissions(
                    channel_permissions,
                    _CHANNEL_PERMISSION_FIELDS,
                ),
            )
        if mentionable:
            payload += bool_field(17, True)
        if self_assignable:
            payload += bool_field(18, True)

        response = await self._unary(
            ROLE_CREATE,
            bytes(payload),
            "CommunityRoleCreate",
        )
        role_id = self._first_guid(response.content, 4)
        if not role_id:
            raise RuntimeError("CommunityRoleCreate returned no ID")

        extended = await self.community_service.get_extended(community_id)
        for role in extended.roles:
            if role.id == role_id:
                return role
        raise RuntimeError("Created role was not returned by GetExtended")

    async def edit_role(
        self,
        community_id: str,
        role_id: str,
        *,
        name: str,
        color_hex: str = "",
        community_permissions: Optional[CommunityPermission] = None,
        channel_permissions: Optional[ChannelPermissions] = None,
        mentionable: bool = False,
        self_assignable: bool = False,
    ) -> CommunityRole:
        community_id = normalize_root_guid(community_id)
        role_id = normalize_root_guid(role_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(role_id))
        payload += length_field(13, name.encode("utf-8"))
        if color_hex:
            payload += length_field(14, color_hex.encode("utf-8"))
        if community_permissions is not None:
            payload += length_field(
                15,
                _encode_bool_permissions(
                    community_permissions,
                    _COMMUNITY_PERMISSION_FIELDS,
                ),
            )
        if channel_permissions is not None:
            payload += length_field(
                16,
                _encode_bool_permissions(
                    channel_permissions,
                    _CHANNEL_PERMISSION_FIELDS,
                ),
            )
        if mentionable:
            payload += bool_field(17, True)
        if self_assignable:
            payload += bool_field(18, True)

        await self._unary(
            ROLE_EDIT,
            bytes(payload),
            "CommunityRoleEdit",
        )
        extended = await self.community_service.get_extended(community_id)
        for role in extended.roles:
            if role.id == role_id:
                return role
        raise RuntimeError("Edited role was not returned by GetExtended")

    async def move_role(
        self,
        community_id: str,
        role_id: str,
        *,
        before_role_id: Optional[str] = None,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        role_id = normalize_root_guid(role_id)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, encode_root_guid(community_id))
        payload += length_field(11, encode_root_guid(role_id))
        if before_role_id is not None:
            payload += length_field(
                12,
                encode_root_guid(normalize_root_guid(before_role_id)),
            )

        await self._unary(
            ROLE_MOVE,
            bytes(payload),
            "CommunityRoleMove",
        )

    async def delete_role(
        self,
        community_id: str,
        role_id: str,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        role_id = normalize_root_guid(role_id)
        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(role_id))
        )
        await self._unary(
            ROLE_DELETE,
            payload,
            "CommunityRoleDelete",
        )

    async def list_access_rules(
        self,
        community_id: str,
        channel_or_group_id: str,
    ) -> Tuple[AccessRule, ...]:
        community_id = normalize_root_guid(community_id)
        channel_or_group_id = normalize_root_guid(channel_or_group_id)

        payload = (
            length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(channel_or_group_id))
        )
        response = await self._unary(
            ACCESS_RULE_LIST_TARGET,
            payload,
            "AccessRuleListByChannelOrChannelGroup",
        )

        result = []
        for flag, frame in iter_grpc_web_frames(response.content):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number != 10 or wire_type != 2:
                    continue
                target_id = None
                overlay = ChannelOverlay()
                for inner_number, inner_wire, inner_value in iter_fields(value):
                    if inner_number == 5 and inner_wire == 2:
                        target_id = decode_root_guid_message(inner_value)
                    elif inner_number == 6 and inner_wire == 2:
                        overlay = _decode_overlay(inner_value)
                if target_id:
                    result.append(AccessRule(target_id, overlay))
            break
        return tuple(result)

    async def set_access_rule(
        self,
        community_id: str,
        channel_or_group_id: str,
        target_id: str,
        overlay: ChannelOverlay,
        *,
        exists: bool = False,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        channel_or_group_id = normalize_root_guid(channel_or_group_id)
        target_id = normalize_root_guid(target_id)

        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(channel_or_group_id))
            + length_field(12, encode_root_guid(target_id))
            + length_field(13, _encode_overlay(overlay))
        )

        await self._unary(
            ACCESS_RULE_EDIT if exists else ACCESS_RULE_CREATE,
            payload,
            "AccessRuleEdit" if exists else "AccessRuleCreate",
        )

    async def delete_access_rule(
        self,
        community_id: str,
        channel_or_group_id: str,
        target_id: str,
    ) -> None:
        community_id = normalize_root_guid(community_id)
        channel_or_group_id = normalize_root_guid(channel_or_group_id)
        target_id = normalize_root_guid(target_id)

        payload = (
            length_field(1, self._context())
            + length_field(10, encode_root_guid(community_id))
            + length_field(11, encode_root_guid(channel_or_group_id))
            + length_field(12, encode_root_guid(target_id))
        )
        await self._unary(
            ACCESS_RULE_DELETE,
            payload,
            "AccessRuleDelete",
        )

    async def clone_community(
        self,
        source_community_id: str,
        *,
        name: Optional[str] = None,
        clone_roles: bool = True,
        clone_access_rules: bool = True,
        clone_member_overrides: bool = False,
    ) -> Community:

        source_community_id = normalize_root_guid(source_community_id)
        source = await self.community_service.get_extended(source_community_id)

        target = await self.create_community(
            name or source.community.name,
            picture_hex=source.community.picture_hex,
            description=source.community.description,
            reject_unverified_email=source.community.reject_unverified_email,
            is_age_restricted=source.community.is_age_restricted,
        )
        target_id = target.id

        target_extended = await self.community_service.get_extended(target_id)

        role_map: Dict[str, str] = {}

        source_roles = sorted(source.roles, key=lambda r: r.position)
        target_roles = sorted(target_extended.roles, key=lambda r: r.position)

        if source_roles and target_roles:
            role_map[source_roles[0].id] = target_roles[0].id

        if clone_roles:
            for role in reversed(source_roles[1:]):
                created = await self.create_role(
                    target_id,
                    role.name,
                    color_hex=role.color_hex or None,
                    community_permissions=role.community_permissions,
                    channel_permissions=role.permissions,
                    mentionable=role.is_mentionable,
                    self_assignable=role.is_self_assignable,
                )
                role_map[role.id] = created.id

        def remap_rules(rules):
            mapped = []
            for rule in rules:
                replacement = role_map.get(rule.target_id)
                if replacement is not None:
                    mapped.append(AccessRule(replacement, rule.overlay))
                elif clone_member_overrides:
                    mapped.append(rule)
            return mapped

        group_map: Dict[str, str] = {}
        channel_map: Dict[str, str] = {}

        for source_group in sorted(source.channel_groups, key=lambda g: g.position):
            group_rules = ()
            if clone_access_rules:
                group_rules = remap_rules(
                    await self.list_access_rules(
                        source_community_id,
                        source_group.id,
                    )
                )
            created_group = await self.create_channel_group(
                target_id,
                source_group.name,
                access_rules=group_rules,
            )
            group_map[source_group.id] = created_group.id

            for source_channel in sorted(
                source_group.channels,
                key=lambda c: c.position,
            ):
                rules = ()
                if clone_access_rules and not source_channel.use_channel_group_permission:
                    rules = remap_rules(
                        await self.list_access_rules(
                            source_community_id,
                            source_channel.id,
                        )
                    )

                created_channel = await self.create_channel(
                    target_id,
                    created_group.id,
                    source_channel.name,
                    description=source_channel.description,
                    channel_type=source_channel.channel_type,
                    use_channel_group_permission=(
                        source_channel.use_channel_group_permission
                    ),
                    access_rules=rules,
                )
                channel_map[source_channel.id] = created_channel.id

        mapped_default = channel_map.get(source.community.default_channel_id or "")
        if mapped_default is not None:
            await self.edit_community(
                target_id,
                default_channel_id=mapped_default,
            )

        final = await self.community_service.get_extended(target_id)
        return self._attach(final.community)
