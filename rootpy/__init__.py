from .client import RootClient
from .commands import (
    Command,
    CommandArgumentError,
    CommandCheckFailure,
    CommandError,
    CommandNotFound,
    Context,
    parse_channel_id,
    parse_channel_mention,
    parse_user_id,
    parse_user_mention,
)
from .emoji import normalize_reaction, parse_emoji_mention
from .events import (
    MessageAction,
    ChannelAction,
    CommunityLeaveReason,
    CallDetachedEvent,
    ChannelDeletedEvent,
    ChannelEvent,
    CommandErrorEvent,
    CommandEvent,
    CommunityDeletedEvent,
    CommunityEvent,
    CommunityLeaveEvent,
    MessageEvent,
    NotificationEvent,
    ReadyEvent,
)
from .exceptions import (
    ErrorInfo,
    get_error_info,
    format_root_error,
    UsernameLookupAuthenticationRequired,
    GrpcStatus,
    GrpcCancelled,
    GrpcUnknown,
    GrpcInvalidArgument,
    GrpcDeadlineExceeded,
    GrpcNotFound,
    GrpcAlreadyExists,
    GrpcPermissionDenied,
    GrpcResourceExhausted,
    GrpcFailedPrecondition,
    GrpcAborted,
    GrpcOutOfRange,
    GrpcUnimplemented,
    GrpcInternal,
    GrpcUnavailable,
    GrpcDataLoss,
    GrpcUnauthenticated,
    AccountAlreadyExists,
    AuthenticationError,
    EmailAlreadyExists,
    GrpcWebError,
    RootError,
    SignUpError,
    TurnstileRequired,
    UsernameAlreadyExists,
)
from .identifiers import (
    normalize_root_guid,
    root_guid_datetime,
    root_guid_type,
)
from .media import (
    MediaBackendInfo,
    media_backend_info,
)
from .media_bootstrap import (
    ensure_ffmpeg,
    ensure_media_dependencies,
)
from .models import (
    AuthenticationSession,
    CallSession,
    Channel,
    ChannelGroup,
    Community,
    CommunityExtended,
    CurrentUser,
    Message,
    MessageAttachment,
    MessageSendResult,
)
from .packets import (
    PacketType,
    SocketPacket,
)
from .permissions import (
    PermissionName,
    AccessRule,
    ChannelOverlay,
    ChannelPermission,
    CommunityPermission,
    ActionNotApplicable,
    CallActionError,
    ChannelPermissions,
    MissingPermissions,
    PermissionStateUnavailable,
)
from .services.assets import AssetService
from .services.direct_messages import (
    DirectMessage,
    DirectMessageService,
)
from .dm_member import DMMemberService
from .users import User


__all__ = [
    # Client
    "RootClient",
    "ErrorInfo",
    "get_error_info",

    # Multi-account hosting
    "MultiClientHost",
    "HostedAccount",
    "Outcome",
    "format_root_error",

    # Reading unread
    "UnreadReader",
    "UnreadChannel",
    "is_unread",

    # Presence and attach lifetime
    "AttachHold",
    "PresenceWatch",
    "effective_presence",

    # Commands
    "Command",
    "CommandArgumentError",
    "CommandCheckFailure",
    "CommandError",
    "CommandNotFound",
    "Context",
    "parse_channel_id",
    "parse_channel_mention",
    "parse_user_id",
    "parse_user_mention",

    "normalize_reaction",
    "parse_emoji_mention",

    # Events
    "CallDetachedEvent",
    "ChannelDeletedEvent",
    "ChannelEvent",
    "CommandErrorEvent",
    "CommandEvent",
    "CommunityDeletedEvent",
    "CommunityEvent",
    "CommunityLeaveEvent",
    "MessageEvent",
    "NotificationEvent",
    "ReadyEvent",

    # Exceptions
    "RootError",
    "AuthenticationError",
    "GrpcWebError",
    "TurnstileRequired",
    "SignUpError",
    "AccountAlreadyExists",
    "UsernameAlreadyExists",
    "EmailAlreadyExists",

    "normalize_root_guid",
    "root_guid_datetime",
    "root_guid_type",

    # Models
    "AuthenticationSession",
    "CallSession",
    "Channel",
    "ChannelGroup",
    "Community",
    "CommunityExtended",
    "CurrentUser",
    "Message",
    "MessageAttachment",
    "MessageSendResult",
    "User",

    # Packets
    "PacketType",
    "SocketPacket",

    # Permissions
    "ActionNotApplicable",
    # The base the other three share. Exported so `except CallActionError`
    # catches a refused mute/kick in one clause -- the subclasses were
    # reachable before, their base was not.
    "CallActionError",
    "ChannelPermissions",
    "MissingPermissions",
    "PermissionStateUnavailable",

    # Direct messages
    "DirectMessage",
    "DirectMessageService",
    "DMMemberService",
    "DirectMessageError",

    "AssetService",

    # Voice / media
    "AudioPlayback",
    "IceInfo",
    "MediaBackendInfo",
    "media_backend_info",
    "ensure_ffmpeg",
    "ensure_media_dependencies",
    "RawAPI",
    "RawService",
    "RawMethod",
    "RawRpcResult",
    "AccessRule",
    "ChannelOverlay",
    "ChannelPermission",
    "CommunityPermission",
    "CommunityAdminService",
    "GrpcStatus",
    "GrpcCancelled",
    "GrpcUnknown",
    "GrpcInvalidArgument",
    "GrpcDeadlineExceeded",
    "GrpcNotFound",
    "GrpcAlreadyExists",
    "GrpcPermissionDenied",
    "GrpcResourceExhausted",
    "GrpcFailedPrecondition",
    "GrpcAborted",
    "GrpcOutOfRange",
    "GrpcUnimplemented",
    "GrpcInternal",
    "GrpcUnavailable",
    "GrpcDataLoss",
    "GrpcUnauthenticated",
    "MessageAction",
    "ChannelAction",
    "CommunityLeaveReason",
    "PermissionName",
    "AttrDict",
    "EnumValue",
    "StructuredAPI",
    "StructuredMethod",
    "StructuredResult",
    "StructuredService",
    "UsernameLookupAuthenticationRequired",
    "RootServiceFacade",
    "RoleManager",
    "MemberManager",
    "CommunityFileManager",
    "LogManager",
    "CommunityAppManager",
    "VoiceAdminManager",
    "FriendshipGroupManager",
]


from .services.community_admin import CommunityAdminService


from .service_facade import RootServiceFacade
from .domain_managers import (
    RoleManager,
    MemberManager,
    CommunityFileManager,
    LogManager,
    CommunityAppManager,
    VoiceAdminManager,
    FriendshipGroupManager,
)

from .object_api import CommunityManager, PermissionManager

from .pagination import AsyncPager, Page

from .events import EventErrorEvent

from .exceptions import HttpStatusError, BadRequest, Unauthorized, Forbidden, HttpNotFound, Conflict, PayloadTooLarge, RateLimited, ServerError, BadGateway, ServiceUnavailable, GatewayTimeout, RetryExhausted, DirectMessageError

from .root_exception import (
    RootExceptionInfo,
    ValidationError,
    decode_root_exception,
    decode_root_exception_header,
)

from .enums import (
    ErrorCodeType,
    PacketErrorCode,
    ContentFlagReason,
    NotificationType,
    MessageType,
    ChannelType,
    UserOnlineStatus,
)

from .packet_schemas import PACKET_SCHEMAS, decode_packet

from .events import ChannelActivity

from .typed_events import (
    BlockEvent,
    ChannelEvent as TypedChannelEvent,
    CommunityEvent as TypedCommunityEvent,
    FriendEvent,
    MemberEvent,
    MemberRoleEvent,
    ReactionEvent,
    RoleEvent,
)

from .cache import LRUCache, StateCache

from .validation import (
    DEFAULT_PICTURE_HEX,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_RULE,
    normalize_hex_colour,
    validate_nickname,
    validate_username,
)

# The generated RPC registries are big and slow to import (~770 ms cold, about
# half of total import time), and most programs never touch them. They load on
# first use instead -- `from rootpy import StructuredAPI` still works, it just
# pays the cost at that moment rather than on every import of the package.
_LAZY_EXPORTS = {
    "RawAPI": "raw_api",
    "RawMethod": "raw_api",
    "RawRpcResult": "raw_api",
    "RawService": "raw_api",
    "AttrDict": "structured_api",
    "EnumValue": "structured_api",
    "StructuredAPI": "structured_api",
    "StructuredMethod": "structured_api",
    "StructuredResult": "structured_api",
    "StructuredService": "structured_api",
    # Voice: rootpy.services.calls pulls in the media support modules, so it
    # is kept off the import path too.
    "AudioPlayback": "services.calls",
    "IceInfo": "services.calls",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'rootpy' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value        # cache it, so this happens once
    return value

from .host import HostedAccount, MultiClientHost, Outcome

from .models import DetailedMember, UserProfile

from .stats import EndpointStats, TransportStats

from .unread import UnreadChannel, UnreadReader, is_unread

from .attach import AttachHold

from .presence import PresenceWatch, effective_presence

from .services.assets import Asset, AssetLink

from .accounts import (
    AccountFactory,
    AlreadyCreatedError,
    CreatedAccount,
    TurnstileChallenge,
)
