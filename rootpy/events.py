from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Optional

from .models import Channel, Community, Message


class MessageAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    UNKNOWN = "unknown"

    @classmethod
    def from_value(cls, value: str) -> "MessageAction":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class ChannelAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    UNKNOWN = "unknown"


class CommunityLeaveReason(IntEnum):
    # Wire values come from RootApp.WebApi.Shared.Enums/CommunityLeaveReason:
    # Unspecified=0, User=1, Kicked=4, Banned=7. 2 and 3 are not on the wire at
    # all -- with the old KICKED=2/BANNED=3 every real kick or ban fell through
    # from_value() and decoded as UNKNOWN, indistinguishable from a parse miss.
    # UNKNOWN keeps 0 deliberately: that is the app's Unspecified.
    UNKNOWN = 0
    LEFT = 1
    KICKED = 4
    BANNED = 7

    @classmethod
    def from_value(cls, value) -> "CommunityLeaveReason":
        try:
            return cls(int(value))
        except (TypeError, ValueError):
            return cls.UNKNOWN


@dataclass(frozen=True)
class ReadyEvent:
    device_id: str
    hub_url: str


@dataclass(frozen=True)
class NotificationEvent:
    sequence: Optional[int]
    packet_case: Optional[int]
    raw: bytes


@dataclass(frozen=True)
class CallDetachedEvent:
    sequence: Optional[int]
    container_id: str
    raw: bytes


@dataclass(frozen=True)
class MessageEvent:
    sequence: Optional[int]
    message: Message
    action: MessageAction
    raw: bytes


@dataclass(frozen=True)
class CommandEvent:
    context: object


@dataclass(frozen=True)
class CommandErrorEvent:
    message: Message
    error: Exception


@dataclass(frozen=True)
class CommunityEvent:
    sequence: Optional[int]
    community: Community
    raw: bytes


@dataclass(frozen=True)
class ChannelEvent:
    sequence: Optional[int]
    channel: Channel
    action: ChannelAction
    raw: bytes


@dataclass(frozen=True)
class ChannelDeletedEvent:
    sequence: Optional[int]
    channel_id: str
    community_id: str
    channel_group_id: Optional[str]
    cached_channel: Optional[Channel]
    raw: bytes


@dataclass(frozen=True)
class CommunityDeletedEvent:
    sequence: Optional[int]
    community_id: str
    cached_community: Optional[Community]
    removed_channels: tuple[Channel, ...]
    raw: bytes


@dataclass(frozen=True)
class CommunityLeaveEvent:
    sequence: Optional[int]
    community_id: str
    user_id: Optional[str]
    leave_reason: CommunityLeaveReason
    is_self: bool
    cached_community: Optional[Community]
    removed_channels: tuple[Channel, ...]
    raw: bytes


@dataclass(frozen=True)
class EventErrorEvent:
    event_name: str
    event: object
    error: Exception


@dataclass(frozen=True)
class ChannelActivity:
    """A channel got new messages, detected via unread state.

    Emitted by :meth:`RootClient.watch_unread` when new activity is seen but
    the message contents can't be fetched (this session's MessageList access
    is rejected by the server). The activity signal itself is accurate.
    """

    channel_id: str
    channel_name: str
    community_id: Optional[str]
    last_activity_at: object = None
