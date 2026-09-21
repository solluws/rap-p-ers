from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Optional

from .exceptions import RootError


@dataclass(frozen=True)
class ChannelPermissions:
    channel_full_control: bool = False
    channel_view: bool = False
    channel_use_external_emoji: bool = False
    channel_create_message: bool = False
    channel_delete_message_other: bool = False
    channel_manage_pinned_messages: bool = False
    channel_view_message_history: bool = False
    channel_create_message_attachment: bool = False
    channel_create_message_mention: bool = False
    channel_create_message_reaction: bool = False
    channel_make_message_public: bool = False
    channel_move_user_other: bool = False
    channel_voice_talk: bool = False
    channel_voice_mute_other: bool = False
    channel_voice_deafen_other: bool = False
    channel_voice_kick: bool = False
    channel_video_stream_media: bool = False
    channel_create_file: bool = False
    channel_manage_files: bool = False
    channel_view_file: bool = False
    channel_app_kick: bool = False

    def has(self, name: str) -> bool:
        """True when this permission set grants ``name``.

        ``channel_full_control`` implies every other permission, but only for
        names that actually exist -- otherwise a typo like ``channel_veiw``
        silently reported as granted whenever full control was set.
        """
        if name not in _FIELD_NAMES:
            return False
        if self.channel_full_control:
            return True
        return bool(getattr(self, name, False))

    def merge(self, other: "ChannelPermissions") -> "ChannelPermissions":
        values = {}
        for item in fields(self):
            values[item.name] = bool(
                getattr(self, item.name)
                or getattr(other, item.name)
            )
        return ChannelPermissions(**values)

    @classmethod
    def all(cls) -> "ChannelPermissions":
        return cls(**{
            item.name: True
            for item in fields(cls)
        })

    @classmethod
    def none(cls) -> "ChannelPermissions":
        return cls()

    def update(self, **kwargs) -> "ChannelPermissions":
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
        }
        values.update(kwargs)
        return type(self)(**values)


ChannelPermission = ChannelPermissions


@dataclass(frozen=True)
class CommunityPermission:
    community_manage_community: bool = False
    community_manage_roles: bool = False
    community_manage_emojis: bool = False
    community_manage_audit_log: bool = False
    community_create_invite: bool = False
    community_manage_invites: bool = False
    community_create_ban: bool = False
    community_manage_bans: bool = False
    community_full_control: bool = False
    community_kick: bool = False
    community_change_my_nickname: bool = False
    community_change_other_nickname: bool = False
    community_create_channel_group: bool = False
    community_manage_apps: bool = False

    @classmethod
    def all(cls) -> "CommunityPermission":
        return cls(**{
            item.name: True
            for item in fields(cls)
        })

    @classmethod
    def none(cls) -> "CommunityPermission":
        return cls()

    def update(self, **kwargs) -> "CommunityPermission":
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
        }
        values.update(kwargs)
        return type(self)(**values)


#: Valid permission flag names, used by ChannelPermissions.has() to reject
#: names that are not real permissions.
_FIELD_NAMES = frozenset(f.name for f in fields(ChannelPermissions))


@dataclass(frozen=True)
class ChannelOverlay:
    channel_full_control: Optional[bool] = None
    channel_view: Optional[bool] = None
    channel_use_external_emoji: Optional[bool] = None
    channel_create_message: Optional[bool] = None
    channel_delete_message_other: Optional[bool] = None
    channel_manage_pinned_messages: Optional[bool] = None
    channel_view_message_history: Optional[bool] = None
    channel_create_message_attachment: Optional[bool] = None
    channel_create_message_mention: Optional[bool] = None
    channel_create_message_reaction: Optional[bool] = None
    channel_make_message_public: Optional[bool] = None
    channel_move_user_other: Optional[bool] = None
    channel_voice_talk: Optional[bool] = None
    channel_voice_mute_other: Optional[bool] = None
    channel_voice_deafen_other: Optional[bool] = None
    channel_voice_kick: Optional[bool] = None
    channel_video_stream_media: Optional[bool] = None
    channel_create_file: Optional[bool] = None
    channel_manage_files: Optional[bool] = None
    channel_view_file: Optional[bool] = None
    channel_app_kick: Optional[bool] = None

    @classmethod
    def allow_all(cls) -> "ChannelOverlay":
        return cls(**{
            item.name: True
            for item in fields(cls)
        })

    @classmethod
    def deny_all(cls) -> "ChannelOverlay":
        return cls(**{
            item.name: False
            for item in fields(cls)
        })

    @classmethod
    def inherit_all(cls) -> "ChannelOverlay":
        return cls()

    def update(self, **kwargs) -> "ChannelOverlay":
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
        }
        values.update(kwargs)
        return type(self)(**values)


@dataclass(frozen=True)
class AccessRule:
    target_id: str
    overlay: ChannelOverlay




class PermissionName(str, Enum):
    CHANNEL_FULL_CONTROL = "channel_full_control"
    CHANNEL_VIEW = "channel_view"
    CHANNEL_USE_EXTERNAL_EMOJI = "channel_use_external_emoji"
    CHANNEL_CREATE_MESSAGE = "channel_create_message"
    CHANNEL_DELETE_MESSAGE_OTHER = "channel_delete_message_other"
    CHANNEL_MANAGE_PINNED_MESSAGES = "channel_manage_pinned_messages"
    CHANNEL_VIEW_MESSAGE_HISTORY = "channel_view_message_history"
    CHANNEL_CREATE_MESSAGE_ATTACHMENT = "channel_create_message_attachment"
    CHANNEL_CREATE_MESSAGE_MENTION = "channel_create_message_mention"
    CHANNEL_CREATE_MESSAGE_REACTION = "channel_create_message_reaction"
    CHANNEL_MAKE_MESSAGE_PUBLIC = "channel_make_message_public"
    CHANNEL_MOVE_USER_OTHER = "channel_move_user_other"
    CHANNEL_VOICE_TALK = "channel_voice_talk"
    CHANNEL_VOICE_MUTE_OTHER = "channel_voice_mute_other"
    CHANNEL_VOICE_DEAFEN_OTHER = "channel_voice_deafen_other"
    CHANNEL_VOICE_KICK = "channel_voice_kick"
    CHANNEL_VIDEO_STREAM_MEDIA = "channel_video_stream_media"
    CHANNEL_CREATE_FILE = "channel_create_file"
    CHANNEL_MANAGE_FILES = "channel_manage_files"
    CHANNEL_VIEW_FILE = "channel_view_file"
    CHANNEL_APP_KICK = "channel_app_kick"

    @classmethod
    def from_value(cls, value):
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return str(value)


class CallActionError(RootError, RuntimeError):
    # A missing-permission refusal is a routine outcome of user.mute/kick, not a
    # bug, so it has to be catchable by the library-wide `except RootError` that
    # the error-handling guide tells applications to use. RuntimeError stays in the MRO so
    # existing `except RuntimeError` handlers keep working.
    pass


class ActionNotApplicable(CallActionError):
    pass


class MissingPermissions(CallActionError):
    def __init__(self, permission) -> None:
        self.permission = PermissionName.from_value(permission)
        display = (
            self.permission.value
            if isinstance(self.permission, PermissionName)
            else str(self.permission)
        )
        super().__init__(f"Missing permission: {display}")


class PermissionStateUnavailable(CallActionError):
    pass
