"""Root platform enums (generated from the server's protobuf enums).

Each is an :class:`IntEnum`, so members compare equal to their integer
value and unknown values coerce to a plain int rather than raising -- the
wire protocol may add codes this build doesn't know yet.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Union


class _RootEnum(IntEnum):
    @classmethod
    def coerce(cls, value: Union[int, '_RootEnum', None]):
        """Return the matching member, or the raw int if unknown/None."""
        if value is None:
            return None
        try:
            return cls(int(value))
        except ValueError:
            return int(value)

    @property
    def label(self) -> str:
        """Lower-case, space-free name, e.g. ``requested_self``."""
        return self.name.lower()


class ErrorCodeType(_RootEnum):
    UNSPECIFIED = 0
    SERVER_ERROR = 1
    NOT_FOUND = 2
    ALREADY_EXISTS = 3
    WRONG_TYPE = 4
    NOT_ENOUGH_MEMBERS = 5
    NOT_MEMBER_OF = 6
    REQUESTED_SELF = 7
    BANNED = 8
    TOO_MANY_REQUESTS = 9
    TOO_LARGE = 10
    POLICY = 11
    TIMEOUT = 12
    STILL_PROCESSING = 13
    SERVICE_UNAVAILABLE = 14
    UPLOAD_NOT_FOUND = 15
    UN_AUTHENTICATED = 16
    BLOCKED = 17
    LIMIT_EXCEEDED = 18
    FAILED_TO_CREATE = 100
    FAILED_TO_PARSE = 101
    FAILED_TO_AUTHENTICATE = 102
    FAILED_TO_SIGN_UP = 103
    FAILED_TO_UPLOAD = 104
    FAILED_EMAIL_VERIFICATION_CODE_TIMEOUT = 105
    FAILED_EMAIL_VERIFICATION_CODE = 106
    FAILED_INVALID_RECOVERY_CODE = 107
    FAILED_UNKNOWN_PASSKEY = 108
    PENDING_FRIENDSHIP_REQUESTED_SELF = 200
    REQUEST_VALIDATION_FAILED = 300
    POLICY_VIOLATION_WELL_KNOWN_PASSWORD = 400
    POLICY_AGE_RESTRICTED = 401
    POLICY_LAST_PRIMARY_AUTH_FACTOR = 402
    POLICY_USER_ALREADY_HAS_PASSWORD = 403
    POLICY_CHALLENGE_MISMATCH = 404
    POLICY_CANNOT_REVOKE_CURRENT_DEVICE = 405
    NO_PERMISSION_TO_CREATE = 1000
    NO_PERMISSION_TO_ADD = 1001
    NO_PERMISSION_TO_READ = 1002
    NO_PERMISSION_TO_EDIT = 1003
    NO_PERMISSION_TO_DELETE = 1004
    NO_PERMISSION_TO_MOVE = 1005
    NO_PERMISSION_TO_UPLOAD = 1006
    NO_PERMISSION_TO_TYPE = 1007
    NO_PERMISSION_TO_SPEAK = 1008
    NO_PERMISSION_TO_KICK = 1009
    NO_PERMISSION_TO_BAN = 1010
    PAYMENT_FAILED = 5000


class ContentFlagReason(_RootEnum):
    UNSPECIFIED = 0
    OTHER = 1
    DMCA = 2
    COPYRIGHT = 3
    SPAM = 4
    HATESPEECH = 5
    VIOLENCE = 6
    HARASSMENT = 7
    SEXUALCONTENT = 8
    MISINFORMATION = 9
    IMPERSONATION = 10
    OBJECTIONABLE = 11


class NotificationType(_RootEnum):
    UNSPECIFIED = 0
    FRIENDSHIP_INVITE_CREATED = 1
    FRIENDSHIP_INVITE_RESPONDED = 2
    MESSAGE_MENTIONED = 3
    COMMUNITY_MEMBER_INVITED = 4
    COMMUNITY_MEMBER_KICKED = 5
    COMMUNITY_MEMBER_BANNED = 6
    THREADED_MESSAGE_MENTIONED = 7
    THREADED_MESSAGE_MESSAGE_MENTION = 8
    COMMUNITY_APP_SUSPENDED = 9
    COMMUNITY_APP = 10


class MessageType(_RootEnum):
    UNSPECIFIED = 0
    USER_MESSAGE = 1
    SYSTEM = 2


class ChannelType(_RootEnum):
    UNSPECIFIED = 0
    TEXT = 1
    THREADED_TEXT = 2
    VOICE = 4
    APP = 8


class UserOnlineStatus(_RootEnum):
    """Presence states Root supports.

    Root has no separate 'do not disturb' state -- the platform models
    presence as active / inactive (idle) / disconnected (appear offline).
    """

    UNSPECIFIED = 0
    DISCONNECTED = 1   # appears offline / invisible
    INACTIVE = 4       # idle / away
    ACTIVE = 16        # online  (0x10 in the protocol)

    # friendly aliases
    @classmethod
    def from_name(cls, name: str) -> "UserOnlineStatus":
        """Accept everyday names: online, idle, away, invisible, offline."""
        key = (name or "").strip().lower().replace(" ", "").replace("_", "")
        table = {
            "online": cls.ACTIVE,
            "active": cls.ACTIVE,
            "idle": cls.INACTIVE,
            "away": cls.INACTIVE,
            "inactive": cls.INACTIVE,
            "invisible": cls.DISCONNECTED,
            "offline": cls.DISCONNECTED,
            "disconnected": cls.DISCONNECTED,
        }
        if key not in table:
            raise ValueError(
                f"unknown status {name!r}; use one of {sorted(table)}"
            )
        return table[key]

