"""Typed field schemas for gateway packets (generated from the server
protobuf packet messages).

Maps each packet's protobuf field number to a (name, kind) so the gateway
can decode a packet payload into a named dict instead of a generic field
list. Kinds: uuid (-> guid str), string, bool, int32/int64, enum (int),
timestamp (-> epoch seconds float), message (-> raw bytes, or list for
repeated). Only scalar/id/timestamp fields are fully decoded; nested
messages are left as raw bytes for callers that need them.
"""

from __future__ import annotations

# packet_type_name -> { field_number: (python_name, kind, repeated) }
PACKET_SCHEMAS = {
    'ASSET_CHANGED': {2: ('asset_id', 'uuid', False), 3: ('timestamp', 'timestamp', False)},
    'BILLING_PAYMENT_FAILED': {10: ('next_retry_at', 'timestamp', False)},
    'BILLING_SUBSCRIPTION_STATUS_CHANGED': {10: ('subscription_id', 'uuid', False), 12: ('product_name', 'string', False)},
    'CHANNEL_CREATED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('channel_group_id', 'uuid', False), 6: ('name', 'string', False), 9: ('before_channel_id', 'uuid', False), 10: ('use_channel_group_permission', 'bool', False), 11: ('channel_permission', 'message', False), 12: ('channel_type', 'int32', False), 13: ('web_rtc_members', 'message', True), 14: ('community_app_id', 'uuid', False), 15: ('role_or_member_ids', 'uuid', True)},
    'CHANNEL_DELETED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('channel_group_id', 'uuid', False)},
    'CHANNEL_EDITED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('channel_group_id', 'uuid', False), 6: ('name', 'string', False), 9: ('use_channel_group_permission', 'bool', False), 10: ('channel_permission', 'message', False), 11: ('role_or_member_ids', 'uuid', True)},
    'CHANNEL_GROUP_CREATED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('name', 'string', False), 6: ('before_channel_group_id', 'uuid', False), 7: ('channel_group_permission', 'message', False), 8: ('role_or_member_ids', 'uuid', True)},
    'CHANNEL_GROUP_DELETED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False)},
    'CHANNEL_GROUP_EDITED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('name', 'string', False), 6: ('channel_group_permission', 'message', False), 7: ('role_or_member_ids', 'uuid', True)},
    'CHANNEL_GROUP_MOVED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('before_channel_group_id', 'uuid', False)},
    'CHANNEL_MOVED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('channel_group_id', 'uuid', False), 6: ('before_channel_id', 'uuid', False), 7: ('channel_permission', 'message', False), 8: ('role_or_member_ids', 'uuid', True)},
    'COMMUNITY_APP_ADDED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 6: ('app_id', 'uuid', False), 8: ('name', 'string', False), 10: ('version', 'string', False), 12: ('icon_asset_uri', 'string', False), 13: ('banner_asset_uri', 'string', False), 16: ('app_version_id', 'uuid', False), 20: ('channel_id', 'uuid', False), 27: ('channel_permission', 'message', False), 28: ('community_permission', 'message', False)},
    'COMMUNITY_APP_REMOVED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('app_id', 'uuid', False)},
    'COMMUNITY_APP_SET_BUTTON': {3: ('community_id', 'uuid', False), 5: ('key', 'string', False)},
    'COMMUNITY_APP_SET_CHANNEL_ACTIVITY': {3: ('community_id', 'uuid', False), 4: ('channel_id', 'uuid', False), 5: ('last_activity_at', 'timestamp', False)},
    'COMMUNITY_APP_SET_SETTINGS': {3: ('community_id', 'uuid', False), 5: ('settings', 'message', False)},
    'COMMUNITY_APP_SET_STATUS': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('app_id', 'uuid', False), 8: ('pending_deletion_at', 'timestamp', False)},
    'COMMUNITY_APP_VERSION_UPDATE_NOTIFICATION': {3: ('community_id', 'uuid', False), 4: ('community_app_id', 'uuid', False), 5: ('app_id', 'uuid', False), 6: ('version', 'string', False), 7: ('app_version_id', 'uuid', False), 10: ('update', 'message', False)},
    'COMMUNITY_BILLING_CORE_ASSIGNED': {3: ('community_id', 'uuid', False), 4: ('core_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('core_count', 'int32', False)},
    'COMMUNITY_BILLING_CORE_REMOVED': {3: ('community_id', 'uuid', False), 4: ('core_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('core_count', 'int32', False)},
    'COMMUNITY_DELETED': {3: ('community_id', 'uuid', False)},
    'COMMUNITY_EMOJI_CREATED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('shortcode', 'string', False), 6: ('asset_uri', 'string', False)},
    'COMMUNITY_EMOJI_DELETED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False)},
    'COMMUNITY_JOINED': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False), 5: ('community_role_ids', 'uuid', True)},
    'COMMUNITY_LEAVE': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False)},
    'COMMUNITY_MEMBER_ATTACH': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False)},
    'COMMUNITY_MEMBER_BAN_CREATED': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False)},
    'COMMUNITY_MEMBER_BAN_DELETED': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False)},
    'COMMUNITY_MEMBER_DETACH': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False)},
    'COMMUNITY_MEMBER_EDITED_EXTERNAL': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False), 5: ('is_favorite', 'bool', False), 6: ('before_community_id', 'uuid', False)},
    'COMMUNITY_MEMBER_EDITED': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False), 5: ('nickname', 'string', False)},
    'COMMUNITY_MEMBER_ROLE_CREATED': {3: ('community_id', 'uuid', False), 4: ('user_ids', 'uuid', True), 5: ('community_role_id', 'uuid', False)},
    'COMMUNITY_MEMBER_ROLE_DELETED': {3: ('community_id', 'uuid', False), 4: ('user_ids', 'uuid', True), 5: ('community_role_id', 'uuid', False)},
    'COMMUNITY_MEMBER_ROLE_SET_PRIMARY': {3: ('community_id', 'uuid', False), 4: ('user_id', 'uuid', False), 5: ('community_role_id', 'uuid', False)},
    'COMMUNITY': {3: ('community_id', 'uuid', False), 4: ('owner_user_id', 'uuid', False), 5: ('default_channel_id', 'uuid', False), 6: ('name', 'string', False), 7: ('picture_hex', 'string', False), 9: ('reject_unverified_email', 'bool', False), 10: ('join_throttle', 'message', False), 17: ('is_age_restricted', 'bool', False)},
    'COMMUNITY_PERMISSION_UPDATE': {3: ('community_id', 'uuid', False), 4: ('community_permission', 'message', False), 5: ('channel_groups_created', 'message', True), 6: ('channel_groups_edited', 'message', True), 7: ('channel_groups_moved', 'message', True), 8: ('channel_groups_deleted', 'message', True), 9: ('channels_created', 'message', True), 10: ('channels_edited', 'message', True), 11: ('channels_moved', 'message', True), 12: ('channels_deleted', 'message', True)},
    'COMMUNITY_ROLE_DELETED': {3: ('community_id', 'uuid', False), 4: ('community_role_id', 'uuid', False)},
    'COMMUNITY_ROLE_MOVED': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 6: ('before_community_role_id', 'uuid', False)},
    'COMMUNITY_ROLE': {3: ('community_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('channel_permission', 'message', False), 6: ('community_permission', 'message', False), 7: ('name', 'string', False), 8: ('color_hex', 'string', False), 9: ('before_community_role_id', 'uuid', False), 10: ('is_mentionable', 'bool', False), 11: ('is_self_assignable', 'bool', False)},
    'DIRECT_MESSAGE_CREATED': {3: ('creator_user_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('member_user_ids', 'uuid', True), 6: ('last_message', 'message', False), 7: ('web_rtc_members', 'message', True)},
    'DIRECT_MESSAGE_LAST_MESSAGE_DELETED': {4: ('id', 'uuid', False), 5: ('last_message', 'message', False)},
    'DIRECT_MESSAGE_MEMBER_ADDED': {3: ('agent_id', 'uuid', False), 4: ('id', 'uuid', False), 5: ('member_user_ids', 'uuid', True)},
    'DIRECT_MESSAGE_MEMBER_DELETED': {3: ('user_id', 'uuid', False), 4: ('id', 'uuid', False)},
    'DIRECT_MESSAGE_RING_DECLINED': {3: ('user_id', 'uuid', False), 4: ('id', 'uuid', False)},
    'DIRECT_MESSAGE_RING': {3: ('caller_user_id', 'uuid', False), 4: ('id', 'uuid', False)},
    'DIRECTORY_DELETED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('id', 'uuid', False)},
    'DIRECTORY_MOVED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('id', 'uuid', False), 6: ('parent_directory_id', 'uuid', False), 7: ('old_parent_directory_id', 'uuid', False)},
    'DIRECTORY': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('id', 'uuid', False), 6: ('parent_directory_id', 'uuid', False), 7: ('name', 'string', False)},
    'FILE_CREATED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('directory_id', 'uuid', False), 6: ('id', 'uuid', False), 7: ('name', 'string', False), 8: ('length', 'int64', False), 9: ('mime_type', 'string', False), 10: ('asset_id', 'uuid', False), 11: ('modified_at', 'timestamp', False)},
    'FILE_DELETED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('directory_id', 'uuid', False), 6: ('id', 'uuid', False)},
    'FILE_EDITED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('id', 'uuid', False), 6: ('directory_id', 'uuid', False), 7: ('name', 'string', False)},
    'FILE_MOVED': {4: ('community_id', 'uuid', False), 5: ('container_id', 'uuid', False), 6: ('id', 'uuid', False), 7: ('directory_id', 'uuid', False), 8: ('old_directory_id', 'uuid', False)},
    'FRIENDSHIP_CREATED': {3: ('id', 'uuid', False), 4: ('friend_user_id', 'uuid', False), 5: ('friendship_group_id', 'uuid', False)},
    'FRIENDSHIP_DELETED': {3: ('id', 'uuid', False), 4: ('friend_user_id', 'uuid', False), 5: ('friendship_group_id', 'uuid', False)},
    'FRIENDSHIP_GROUP_CREATED': {3: ('id', 'uuid', False), 4: ('is_default', 'bool', False), 5: ('name', 'string', False)},
    'FRIENDSHIP_GROUP_DELETED': {3: ('id', 'uuid', False)},
    'FRIENDSHIP_GROUP_EDITED': {3: ('id', 'uuid', False), 4: ('is_default', 'bool', False), 5: ('name', 'string', False)},
    'FRIENDSHIP_GROUP_MOVED': {3: ('id', 'uuid', False), 4: ('before_friendship_group_id', 'uuid', False)},
    'FRIENDSHIP_MOVED': {3: ('id', 'uuid', False), 4: ('friend_user_id', 'uuid', False), 5: ('friendship_group_id', 'uuid', False), 6: ('old_friendship_group_id', 'uuid', False), 7: ('before_friendship_id', 'uuid', False)},
    'HUB_SERVER_MOVE': {},
    'MESSAGE_DELETED': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('id', 'uuid', False), 6: ('deleted_at', 'timestamp', False)},
    'MESSAGE': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('id', 'uuid', False), 7: ('deleted_at', 'timestamp', False), 8: ('edited_at', 'timestamp', False), 9: ('pinned_at', 'timestamp', False), 10: ('message_content', 'string', False), 12: ('payload', 'message', False), 13: ('message_uris', 'message', True), 14: ('reference_maps', 'message', False), 15: ('reactions', 'message', True), 16: ('parent_messages', 'message', True)},
    'MESSAGE_PIN': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('message_id', 'uuid', False)},
    'MESSAGE_REACTION_DELETED_FULL': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('shortcode', 'string', False), 6: ('message_id', 'uuid', False)},
    'MESSAGE_REACTION': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('shortcode', 'string', False), 6: ('message_id', 'uuid', False), 7: ('user_id', 'uuid', False)},
    'MESSAGE_SET_TYPING_INDICATOR': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('is_typing', 'bool', False), 7: ('created_at', 'timestamp', False)},
    'MESSAGE_SET_VIEW_TIME': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_last_viewed_at', 'timestamp', False)},
    'NOTIFICATION_DELETED_ALL': {},
    'NOTIFICATION_DELETED': {3: ('id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('sub_container_id', 'uuid', False)},
    'NOTIFICATION': {3: ('id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('sub_container_id', 'uuid', False), 6: ('user_id', 'uuid', False), 8: ('payload', 'message', False), 9: ('is_viewed', 'bool', False)},
    'NOTIFICATION_VIEWED_ALL': {},
    'NOTIFICATION_VIEWED': {2: ('notification_ids', 'uuid', True)},
    'PING': {2: ('sequence_number', 'int64', False)},
    'USER_BLOCK_CREATED': {2: ('user_id', 'uuid', False), 3: ('block_user_id', 'uuid', False)},
    'USER_BLOCK_DELETED': {2: ('user_id', 'uuid', False), 3: ('block_user_id', 'uuid', False)},
    'USER_DELETED': {2: ('user_id', 'uuid', False)},
    'USER_SET_BADGES': {3: ('user_id', 'uuid', False), 5: ('badges', 'message', True)},
    'USER_SET_COMMUNITY_INVITE_REQUIREMENT': {2: ('user_id', 'uuid', False), 5: ('is_email_verified', 'bool', False)},
    'USER_SET_DIRECT_MESSAGE_INVITE_REQUIREMENT': {2: ('user_id', 'uuid', False), 5: ('is_email_verified', 'bool', False)},
    'USER_SET_EMAIL_VERIFICATION': {2: ('user_id', 'uuid', False), 3: ('is_verified', 'bool', False)},
    'USER_SET_FRIENDSHIP_INVITE_REQUIREMENT': {2: ('user_id', 'uuid', False), 5: ('is_email_verified', 'bool', False)},
    'USER_SET_MAX_STATUS': {2: ('user_id', 'uuid', False)},
    'USER_SET_PROFILE': {3: ('user_id', 'uuid', False), 4: ('username', 'string', False), 5: ('profile_picture_asset_uri', 'string', False)},
    'USER_SET_STATUS': {3: ('user_id', 'uuid', False)},
    'USER_SET_USER_SUBSCRIPTION': {2: ('user_id', 'uuid', False), 3: ('user_subscription', 'message', False)},
    'WEBRTC_USER_DETACH': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('device_id', 'uuid', False), 7: ('is_kick', 'bool', False)},
    'WEBRTC_USER_DEVICE': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('device_id', 'uuid', False), 7: ('is_muted', 'bool', False), 8: ('is_admin_muted', 'bool', False), 9: ('is_deafened', 'bool', False), 10: ('is_admin_deafened', 'bool', False), 25: ('is_video_paused', 'bool', False), 26: ('is_screen_paused', 'bool', False)},
    'WEBRTC_USER_DEVICE_SET_DATA_CHANNEL': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('device_id', 'uuid', False), 7: ('data_channel_name', 'string', False)},
    'WEBRTC_USER_DEVICE_SET_STATUS': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('device_id', 'uuid', False), 7: ('is_muted', 'bool', False), 8: ('is_admin_muted', 'bool', False), 9: ('is_deafened', 'bool', False), 10: ('is_admin_deafened', 'bool', False), 11: ('is_video_paused', 'bool', False), 12: ('is_screen_paused', 'bool', False)},
    'WEBRTC_USER_DEVICE_SET_TRANSPORT': {3: ('community_id', 'uuid', False), 4: ('container_id', 'uuid', False), 5: ('user_id', 'uuid', False), 6: ('device_id', 'uuid', False)},
}


from typing import Any, Dict, Optional

from .protocol import decode_root_guid_message, iter_fields


def _decode_timestamp(data: bytes) -> Optional[float]:
    """google.protobuf.Timestamp -> epoch seconds (float), or None."""
    seconds = 0
    nanos = 0
    for n, wt, value in iter_fields(data):
        if n == 1 and wt == 0:
            seconds = int(value)
        elif n == 2 and wt == 0:
            nanos = int(value)
    if seconds == 0 and nanos == 0:
        return None
    return seconds + nanos / 1_000_000_000


def _convert(kind: str, wire_type: int, value: Any):
    if kind == "uuid":
        return decode_root_guid_message(value) if isinstance(value, (bytes, bytearray)) else None
    if kind == "string":
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None
    if kind in ("bool",):
        return bool(value) if wire_type == 0 else None
    if kind in ("int32", "int64", "enum"):
        return int(value) if wire_type == 0 else None
    if kind == "double":
        return value
    if kind == "timestamp":
        return _decode_timestamp(value) if isinstance(value, (bytes, bytearray)) else None
    # message (nested) -> keep raw bytes for callers who want to decode further
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


def decode_packet(packet_type_name: str, payload: Optional[bytes]) -> Dict[str, Any]:
    """Decode a packet payload into a named dict using its schema.

    Unknown packet types (or a missing payload) return an empty dict. Repeated
    fields collect into a list; nested messages are left as raw bytes.
    """
    schema = PACKET_SCHEMAS.get(packet_type_name)
    if not schema or not payload:
        return {}
    out: Dict[str, Any] = {}
    for number, wire_type, value in iter_fields(payload):
        entry = schema.get(number)
        if entry is None:
            continue
        name, kind, repeated = entry
        converted = _convert(kind, wire_type, value)
        if repeated:
            out.setdefault(name, []).append(converted)
        else:
            out[name] = converted
    return out
