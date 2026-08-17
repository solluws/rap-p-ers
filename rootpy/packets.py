from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Optional


class PacketType(IntEnum):
    """Gateway packet types.

    These values are the ``PacketContainer`` oneof field numbers the hub sends
    (``packet_case``), taken verbatim from the server's ``PacketOneofCase``
    enum -- so ``PacketType.from_case(packet_case)`` names every packet the
    gateway can receive, and the client re-dispatches each one as
    ``on_packet_<name>`` (e.g. ``on_packet_direct_message_created``,
    ``on_packet_friendship_created``, ``on_packet_message_reaction``).
    """

    UNKNOWN = 0
    PING = 1
    HUB_SERVER_MOVE = 2

    # Channels
    CHANNEL_CREATED = 30
    CHANNEL_EDITED = 31
    CHANNEL_MOVED = 32
    CHANNEL_DELETED = 33
    CHANNEL_GROUP_CREATED = 40
    CHANNEL_GROUP_EDITED = 41
    CHANNEL_GROUP_MOVED = 42
    CHANNEL_GROUP_DELETED = 43

    # Communities
    COMMUNITY = 50
    COMMUNITY_JOINED = 51
    COMMUNITY_LEAVE = 52
    COMMUNITY_DELETED = 53
    COMMUNITY_MEMBER_ATTACH = 60
    COMMUNITY_MEMBER_DETACH = 61
    COMMUNITY_MEMBER_EDITED = 62
    COMMUNITY_MEMBER_EDITED_EXTERNAL = 63
    COMMUNITY_MEMBER_BAN_CREATED = 70
    COMMUNITY_MEMBER_BAN_DELETED = 71
    COMMUNITY_MEMBER_ROLE_CREATED = 80
    COMMUNITY_MEMBER_ROLE_SET_PRIMARY = 81
    COMMUNITY_MEMBER_ROLE_DELETED = 82
    COMMUNITY_ROLE = 90
    COMMUNITY_ROLE_DELETED = 91
    COMMUNITY_ROLE_MOVED = 92
    COMMUNITY_PERMISSION_UPDATE = 100

    # Community apps
    COMMUNITY_APP_ADDED = 110
    COMMUNITY_APP_REMOVED = 111
    COMMUNITY_APP_SET_STATUS = 112
    COMMUNITY_APP_SET_SETTINGS = 113
    COMMUNITY_APP_VERSION_UPDATE_NOTIFICATION = 114
    COMMUNITY_APP_SET_BUTTON = 115
    COMMUNITY_APP_SET_CHANNEL_ACTIVITY = 126

    # Community billing / emoji
    COMMUNITY_BILLING_CORE_ASSIGNED = 118
    COMMUNITY_BILLING_CORE_REMOVED = 119
    COMMUNITY_EMOJI_CREATED = 116
    COMMUNITY_EMOJI_DELETED = 117

    # Direct messages
    DIRECT_MESSAGE_CREATED = 120
    DIRECT_MESSAGE_MEMBER_ADDED = 121
    DIRECT_MESSAGE_MEMBER_DELETED = 122
    DIRECT_MESSAGE_RING = 123
    DIRECT_MESSAGE_RING_DECLINED = 124
    DIRECT_MESSAGE_LAST_MESSAGE_DELETED = 125

    # Directories / files
    DIRECTORY = 130
    DIRECTORY_MOVED = 131
    DIRECTORY_DELETED = 132
    FILE_CREATED = 140
    FILE_EDITED = 141
    FILE_MOVED = 142
    FILE_DELETED = 143

    # Friendships
    FRIENDSHIP_CREATED = 150
    FRIENDSHIP_MOVED = 151
    FRIENDSHIP_DELETED = 152
    FRIENDSHIP_GROUP_CREATED = 160
    FRIENDSHIP_GROUP_EDITED = 161
    FRIENDSHIP_GROUP_MOVED = 162
    FRIENDSHIP_GROUP_DELETED = 163

    # Messages
    MESSAGE = 170
    MESSAGE_DELETED = 171
    MESSAGE_PIN = 172
    MESSAGE_REACTION = 173
    MESSAGE_SET_TYPING_INDICATOR = 174
    MESSAGE_SET_VIEW_TIME = 175
    MESSAGE_REACTION_DELETED_FULL = 176

    # Notifications
    NOTIFICATION = 180
    NOTIFICATION_VIEWED = 181
    NOTIFICATION_VIEWED_ALL = 182
    NOTIFICATION_DELETED = 183
    NOTIFICATION_DELETED_ALL = 184

    # User
    USER_SET_PROFILE = 190
    USER_SET_STATUS = 191
    USER_SET_EMAIL_VERIFICATION = 192
    USER_SET_MAX_STATUS = 193
    USER_DELETED = 194
    USER_SET_BADGES = 195
    USER_SET_DIRECT_MESSAGE_INVITE_REQUIREMENT = 196
    USER_SET_FRIENDSHIP_INVITE_REQUIREMENT = 197
    USER_SET_COMMUNITY_INVITE_REQUIREMENT = 198
    USER_SET_USER_SUBSCRIPTION = 199

    # WebRTC (voice/video)
    WEBRTC_USER_DEVICE = 200
    WEBRTC_USER_DETACH = 201
    WEBRTC_USER_DEVICE_SET_STATUS = 202
    WEBRTC_USER_DEVICE_SET_TRANSPORT = 203
    WEBRTC_USER_DEVICE_SET_DATA_CHANNEL = 204

    # Blocks / assets / billing
    USER_BLOCK_CREATED = 210
    USER_BLOCK_DELETED = 211
    ASSET_CHANGED = 220
    BILLING_SUBSCRIPTION_STATUS_CHANGED = 230
    BILLING_PAYMENT_FAILED = 231

    @classmethod
    def from_case(cls, packet_case: Optional[int]) -> "PacketType":
        if packet_case is None:
            return cls.UNKNOWN
        try:
            return cls(packet_case)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class SocketPacket:
    sequence: Optional[int]
    type: PacketType
    case: Optional[int]
    data: Dict[str, Any]
    raw: bytes

    @property
    def is_unknown(self) -> bool:
        return self.type is PacketType.UNKNOWN

    @property
    def fields(self) -> dict:
        """Typed, named fields decoded from the packet payload (may be empty)."""
        data = self.data if isinstance(self.data, dict) else {}
        return data.get("fields") or {}

    def get(self, name: str, default=None):
        """Shortcut for a single decoded field, e.g. ``pkt.get('user_id')``."""
        return self.fields.get(name, default)
