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
    # Both are voice-call refusals, which is exactly the kind a caller
    # branches on.
    WEB_RTC_BACKEND_MISMATCH = 6000
    WEB_RTC_CALL_BANNED = 6001


class PacketErrorCode(_RootEnum):
    """Field 1 of a gateway ``ClientNotification`` -- unrelated to
    :class:`ErrorCodeType`, which rides on API responses.

    ``SYNC_LOST`` is the server saying our resume cursor is no longer valid.
    It is the same condition the 4016 close code reports, but stated in-band
    and *before* the socket goes away, so it is the one signal that does not
    have to be inferred from close-code text.
    """

    UNSPECIFIED = 0
    SYNC_LOST = 1


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


class UserDirectMessageInviteConnection(_RootEnum):
    """Who is allowed to open a DM with you.

    ``FRIEND`` is the default a fresh account lands on, which is why a new
    pair of accounts gets ``PERMISSION_DENIED`` from ``DirectMessageCreate``
    until they befriend each other.
    """

    UNSPECIFIED = 0
    ANY = 1
    CONNECTED = 2       # shares a community with you
    NONE = 3
    FRIEND = 4


class UserCommunityInviteConnection(_RootEnum):
    """Who is allowed to invite you to a community."""

    UNSPECIFIED = 0
    ANY = 1
    CONNECTED = 2
    NONE = 3
    FRIEND = 4


class UserFriendshipInviteConnection(_RootEnum):
    """Who is allowed to send you a friend request.

    No ``FRIEND`` member, unlike the other two: requiring friendship in order
    to be friended is not a state Root models.
    """

    UNSPECIFIED = 0
    ANY = 1
    CONNECTED = 2
    NONE = 3


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



# --------------------------------------------------------------------------
# Community Discovery, and per-container notification settings
# --------------------------------------------------------------------------
class CommunityCategory(_RootEnum):
    """The category a community lists itself under in Discovery.

    The gaps are the server's: 5, 8 and 9 are not values of
    ``CommunityCategory``.

    Because of them, ``CommunityCategory(row.category)`` raises ValueError on
    data the server considers perfectly valid -- and will do so again the day
    Root adds a category before rootpy learns about it. Use
    :meth:`coerce`, which returns the member when there is one and the plain
    int when there is not.
    """

    UNSPECIFIED = 0
    GAMING = 1
    MUSIC = 2
    EDUCATION = 3
    TECHNOLOGY = 4
    ART_AND_CREATIVE = 6
    SPORTS = 7
    SOCIAL = 10


class CommunityDiscoverySort(_RootEnum):
    """Ordering for ``client.discovery.search``."""

    UNSPECIFIED = 0
    MEMBER_COUNT = 1
    CORE_COUNT = 2


class CommunityDiscoverableRequirement(_RootEnum):
    """Why a community is not listable yet.

    ``CommunityGetResponse.is_discoverable_status`` carries the *first*
    unmet requirement, so it is a single value rather than a set.
    """

    UNSPECIFIED = 0
    CORE_COUNT = 1
    OWNER_EMAIL_VERIFIED = 2
    PICTURE = 3
    BANNER = 4
    MEMBER_COUNT = 5
    AGE = 6
    JOIN_THROTTLE = 7
    DESCRIPTION = 8
    CATEGORY = 9
    CONTENT = 10
    MODERATION = 11


class CommunityJoinSource(_RootEnum):
    """How a member arrived."""

    UNSPECIFIED = 0
    INVITE = 1
    INVITE_LINK = 2
    DISCOVERY = 3


class NotificationStatus(_RootEnum):
    """Per-community, per-channel and per-DM notification setting.

    Note the name: the *field* is spelled ``notification_setting`` on every
    message that carries it, but the enum type is ``NotificationStatus``.
    """

    UNSPECIFIED = 0
    NONE = 1
    ALL = 2
    MENTION = 3


class WebRtcBackend(_RootEnum):
    """Which media stack Root routed a call session to.

    ``V2`` is what a live server returns today. It does not negotiate media
    itself: ``WebRtcSessionCreateResponse`` comes back with no
    ``session_id`` and no ``session_description``, and instead carries a
    LiveKit ``server_url`` (field 18) and an ``access_token`` JWT (field 19)
    for the client to join with.

    rootpy performs the signalling and hands you those two values. It does
    **not** bundle a LiveKit client, so joining the room is yours to do.
    """

    UNSPECIFIED = 0
    V1 = 1
    V2 = 2


class WebRtcCallResult(_RootEnum):
    """Outcome on ``WebRtcSessionCreateResponse``. Note CONNECTED is 0."""

    CONNECTED = 0
    ENDED = 1
