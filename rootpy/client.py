from __future__ import annotations
from typing import Optional, Union

import asyncio
import getpass
import inspect
from collections import deque
from dataclasses import replace
from collections.abc import Awaitable, Callable

from .auth import AuthClient
from .commands import (
    Command,
    CommandCheckFailure,
    CommandError,
    CommandNotFound,
    Context,
    invoke_command,
    parse_command_content,
)
from .events import (
    ChannelDeletedEvent,
    ChannelEvent,
    CommandErrorEvent,
    EventErrorEvent,
    CommunityDeletedEvent,
    CommunityEvent,
    CommunityLeaveEvent,
    MessageEvent,
    MessageAction,
    ChannelAction,
    CommunityLeaveReason,
    ReadyEvent,
)
from .gateway import Gateway
from .exceptions import UsernameLookupAuthenticationRequired
from .models import (
    AuthenticationSession,
    Channel,
    ChannelGroup,
    Community,
    CommunityExtended,
    CurrentUser,
    Message,
)
from .services import (
    AssetService,
    CallService,
    CommunityService,
    CommunityAdminService,
    DirectMessageService,
    MessageService,
    UserService,
)
from .dm_member import DMMemberService
from .transport import GrpcWebTransport
from .service_facade import RootServiceFacade
from .object_api import CommunityManager, PermissionManager
from .domain_managers import (
    RoleManager,
    MemberManager,
    CommunityFileManager,
    LogManager,
    CommunityAppManager,
    VoiceAdminManager,
    FriendshipGroupManager,
)
from .features import (
    EmojiManager,
    ModerationManager,
    FriendManager,
    FriendRequestManager,
    BlockManager,
    InviteManager,
    NotificationManager,
    UserSettingsManager,
    DirectoryManager,
    SearchManager,
)
from .permissions import ChannelPermissions
from .users import User
from .highlevel import HighLevelMixin

EventHandler = Callable[..., Awaitable[None]]



# Client attributes that live on the StructuredAPI. Resolved lazily so the
# heavy generated registries are only imported if something actually uses them.
_HIGH_ALIASES = {
    "channel_api": "channel",
    "channel_group_api": "channel_group",
    "community_api": "community",
    "role_api": "community_role",
    "member_api": "community_member",
    "member_role_api": "community_member_role",
    "ban_api": "community_member_ban",
    "member_invite_api": "community_member_invite",
    "emoji_api": "community_emoji",
    "link_api": "link",
    "file_api": "file",
    "directory_api": "directory",
    "user_api": "user",
    "friendship_api": "friendship",
    "friendship_invite_api": "friendship_invite",
    "notification_api": "notification",
    "asset_api": "asset",
    "message_api": "message",
    "webrtc_api": "web_rtc",
    "app_store_api": "app_store",
    "app_review_api": "app_review",
    "billing_api": "billing",
    "community_billing_api": "community_billing",
    "support_api": "support",
}


class RootClient(HighLevelMixin):
    @classmethod
    async def send_email_verification(
        cls,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        token: Optional[str] = None,
        turnstile_token: Optional[str] = None,
    ) -> None:

        if token is not None:
            token = token.strip()
            if not token:
                token = None

        if token is None:
            if not username or not password:
                raise ValueError(
                    "Provide either token=... or both username and password"
                )

        transport = GrpcWebTransport()
        try:
            auth_token = token

            if auth_token is None:
                auth = AuthClient(transport)
                session = await auth.login(username, password)
                auth_token = session.token

            api = StructuredAPI(
                transport,
                lambda: auth_token or "",
            )

            kwargs = {}
            if turnstile_token:
                kwargs["turnstile_token"] = turnstile_token

            await api.user.resend_email_verification_code(**kwargs)
        finally:
            await transport.close()

    @classmethod
    async def verify_email(
        cls,
        verification_code: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        token: Optional[str] = None,
    ) -> None:
        if not isinstance(verification_code, str):
            raise TypeError("verification_code must be a string")

        verification_code = verification_code.strip()
        if not verification_code:
            raise ValueError("verification_code cannot be empty")

        if token is not None:
            token = token.strip()
            if not token:
                token = None

        if token is None:
            if not username or not password:
                raise ValueError(
                    "Provide either token=... or both username and password"
                )

        transport = GrpcWebTransport()
        try:
            auth_token = token

            if auth_token is None:
                auth = AuthClient(transport)
                session = await auth.login(username, password)
                auth_token = session.token

            api = StructuredAPI(
                transport,
                lambda: auth_token or "",
            )

            await api.user.set_email_verification_code(
                verification_code=verification_code,
            )
        finally:
            await transport.close()

    def __init__(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        command_prefix: str = ">",
        user_id: Optional[str] = None,
        auto_install_media: bool = True,
        transport: Optional[GrpcWebTransport] = None,
        owns_transport: bool = True,
        preload_caches: bool = True,
        expand_communities: bool = False,
        message_cache_size: int = 2000,
        preload_direct_messages: bool = False,
        auto_reconnect: bool = True,
        reconnect_base: float = 1.0,
        reconnect_max: float = 30.0,
        max_reconnect_attempts: Optional[int] = None,
    ) -> None:
        self.username = username
        self.password = password
        self.token = token.strip() if isinstance(token, str) and token.strip() else None
        self.auto_install_media = auto_install_media
        self.auto_reconnect = bool(auto_reconnect)
        self.reconnect_base = float(reconnect_base)
        self.reconnect_max = float(reconnect_max)
        self.max_reconnect_attempts = max_reconnect_attempts
        self._owns_transport = bool(owns_transport)
        self.preload_caches = bool(preload_caches)
        # Expanding every community at login costs one GetExtended per
        # community -- 15+ heavy round-trips before the client is usable.
        # Off by default: communities expand on first access and are cached.
        self.expand_communities = bool(expand_communities)
        self.preload_direct_messages = bool(preload_direct_messages)
        self.transport = transport or GrpcWebTransport()
        # The generated RPC registries are large (~1.2 MB of module source)
        # and cost ~770 ms to import cold -- about half of total import time --
        # but most scripts never touch them. They're built on first access
        # instead; see __getattr__ below.
        self._raw_api = None
        self._high_api = None
        self._service_facade = None
        self.auth = AuthClient(self.transport)
        self.session: Optional[AuthenticationSession] = None
        self.gateway: Optional[Gateway] = None
        self._events: dict[str, EventHandler] = {}
        self.direct_messages = DirectMessageService(
            self.transport,
            self._require_token,
            lambda: self.user_id,
        )

        self.dm_service = self.direct_messages

        self.dm = DMMemberService(self)
        self.calls = CallService(
            self.transport,
            self._require_token,
            self.direct_messages,
        )
        self.messages = MessageService(
            self.transport,
            self._require_token,
            self._record_sent_message_id,
        )
        self.assets = AssetService(
            self.transport,
            self._require_token,
            self._require_web_api_url,
        )
        self.users = UserService(
            self.transport,
            self._require_token,
            self.assets,
        )
        self.community_service = CommunityService(
            self.transport,
            self._require_token,
        )
        self.community_admin = CommunityAdminService(
            self.transport,
            self._require_token,
            self.community_service,
            client=self,
        )
        self.admin = self.community_admin
        self.emojis = EmojiManager(self)
        self.moderation = ModerationManager(self)
        self.friends = FriendManager(self)
        self.blocks = BlockManager(self)
        self.invites = InviteManager(self)
        self.notifications = NotificationManager(self)
        self.user_settings = UserSettingsManager(self)
        self.directories = DirectoryManager(self)
        self.search = SearchManager(self)
        self.roles = RoleManager(self)
        self.friend_requests = FriendRequestManager(self)
        self.members = MemberManager(self)
        self.community_files = CommunityFileManager(self)
        self.logs = LogManager(self)
        self.community_apps = CommunityAppManager(self)
        self.voice_admin = VoiceAdminManager(self)
        self.friend_groups = FriendshipGroupManager(self)
        self.community = CommunityManager(self)
        self.permissions = self.community.permissions

        self.user: Optional[CurrentUser] = None
        # Bounded: an always-on watcher would otherwise accumulate every
        # message it ever saw. LRU keeps recent lookups fast without leaking.
        from .cache import LRUCache

        self.message_cache = LRUCache(maxsize=message_cache_size)
        self.communities: dict[str, Community] = {}
        self.channel_groups: dict[str, ChannelGroup] = {}
        self.channels: dict[str, Channel] = {}
        self.command_prefix = command_prefix
        self.user_id = user_id
        # Ids of messages we sent, cleared when the gateway echoes them back.
        # An echo that never arrives would otherwise leak, so this is capped.
        self._sent_message_ids: set[str] = set()
        self._sent_message_order: deque = deque()
        self._commands: dict[str, Command] = {}
        self._message_listeners: list = []
        self._watch_tasks: list = []
        from .typed_events import TypedEventBuilder
        self._typed_events = TypedEventBuilder(self)
        self._waiters: list = []
        self._extra_listeners: dict = {}
        from .cache import StateCache
        self.cache = StateCache()
        self._recent_message_ids: set = set()
        self._recent_message_order: deque = deque()
        self._auto_react_listener = None
        self._users: dict[str, User] = {}
        self._community_extended: dict[str, CommunityExtended] = {}
        self.cache_errors: list[str] = []
        self._background_tasks: set[asyncio.Task] = set()

    async def get_servers(self, *, refresh: bool = True):
        return await self.community.servers(refresh=refresh)

    async def get_communities(self, *, refresh: bool = True):
        return await self.get_servers(refresh=refresh)

    async def get_friends(self):
        return await self.friends.list()

    def service(self, name: str):
        return self.services.get(name)

    def describe_services(self, name: Optional[str] = None):
        return self.services.describe(name)

    @property
    def device_id(self) -> Optional[str]:
        return self.session.device_id if self.session else None

    @property
    def hub_url(self) -> Optional[str]:
        return self.session.hub_url if self.session else None

    def get_user(
        self,
        user_id: str,
        *,
        username: Optional[str] = None,
    ) -> User:
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(user_id)
        cached = self._users.get(normalized)
        if cached is not None:
            return cached

        user = User(self, normalized, username)
        self._users[normalized] = user
        return user

    async def ensure_community_cached(
        self,
        community_id: str,
    ) -> CommunityExtended:
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(community_id)
        cached = self._community_extended.get(normalized)
        if cached is not None:
            return cached
        return await self.fetch_community(normalized)

    def permissions_for(
        self,
        user_id: str,
        channel_id: str,
    ) -> Optional[ChannelPermissions]:
        from .identifiers import normalize_root_guid

        normalized_user = normalize_root_guid(user_id)
        normalized_channel = normalize_root_guid(channel_id)
        channel = self.channels.get(normalized_channel)
        if channel is None:
            return None
        community = self.communities.get(channel.community_id)
        if community is not None and community.owner_user_id == normalized_user:
            return ChannelPermissions(channel_full_control=True)
        extended = self._community_extended.get(channel.community_id)
        if extended is None:
            return None
        member = next((m for m in extended.members if m.user_id == normalized_user), None)
        if member is None:
            return None
        role_ids = set(member.role_ids)
        effective = ChannelPermissions()
        for role in extended.roles:
            if role.id in role_ids:
                effective = effective.merge(role.permissions)
        group = self.channel_groups.get(channel.channel_group_id) if channel.channel_group_id else None
        target_ids = role_ids | {normalized_user}
        if group is not None and (not group.role_or_member_ids or target_ids.intersection(group.role_or_member_ids)):
            effective = effective.merge(group.permissions)
        if not channel.use_channel_group_permission:
            if not channel.role_or_member_ids or target_ids.intersection(channel.role_or_member_ids):
                effective = effective.merge(channel.permissions)
        return effective

    def set_user_id(self, user_id: str) -> None:
        from .identifiers import normalize_root_guid

        self.user_id = normalize_root_guid(user_id)

    def _record_sent_message_id(self, message_id: str) -> None:
        self._sent_message_ids.add(message_id)
        self._sent_message_order.append(message_id)
        if len(self._sent_message_order) > 1000:
            stale = self._sent_message_order.popleft()
            self._sent_message_ids.discard(stale)

    def command(
        self,
        name: Optional[str] = None,
        *,
        aliases: Union[tuple[str, ...], list[str]] = (),
        owner_only: bool = True,
        description: str = "",
    ):
        def decorator(callback):
            if not inspect.iscoroutinefunction(callback):
                raise TypeError("Command callback must be async")

            command_name = (name or callback.__name__).casefold()
            command = Command(
                name=command_name,
                callback=callback,
                aliases=tuple(alias.casefold() for alias in aliases),
                owner_only=owner_only,
                description=description,
            )

            for key in (command.name, *command.aliases):
                if key in self._commands:
                    raise ValueError(f"Command name already registered: {key}")
                self._commands[key] = command

            return callback

        return decorator

    def get_command(self, name: str) -> Optional[Command]:
        return self._commands.get(name.casefold())

    async def process_commands(self, message: Message) -> None:
        try:
            parsed = parse_command_content(
                message.content,
                self.command_prefix,
            )
            if parsed is None:
                return

            invoked_with, args, raw_arguments = parsed
            command = self.get_command(invoked_with)
            if command is None:
                raise CommandNotFound(invoked_with)

            if command.owner_only:
                if self.user_id is None:
                    raise CommandCheckFailure(
                        "Client user_id is unknown. Pass user_id=... to "
                        "RootClient, call set_user_id(), or send one normal "
                        "message first so the SDK can infer it."
                    )
                if message.user_id != self.user_id:
                    return

            context = Context(
                client=self,
                message=message,
                prefix=self.command_prefix,
                invoked_with=invoked_with,
                command=command,
                args=args,
                raw_arguments=raw_arguments,
            )

            await self.dispatch("command", context)
            await invoke_command(command, context)
        except CommandNotFound:

            return
        except Exception as exc:
            await self.dispatch(
                "command_error",
                CommandErrorEvent(message, exc),
            )

    def add_listener(self, event_name: str, coroutine) -> None:
        """Register an extra handler for an event.

        ``@client.event`` allows one handler per event (keyed by function
        name); this allows any number, and lets you register dynamically::

            client.add_listener("member_join", on_join)

        Use the bare event name -- no ``on_`` prefix.
        """
        if not inspect.iscoroutinefunction(coroutine):
            raise TypeError("Event handler must be async")
        name = event_name[3:] if event_name.startswith("on_") else event_name
        self._extra_listeners.setdefault(name, []).append(coroutine)

    def remove_listener(self, event_name: str, coroutine) -> None:
        """Remove a handler previously added with :meth:`add_listener`."""
        name = event_name[3:] if event_name.startswith("on_") else event_name
        handlers = self._extra_listeners.get(name)
        if handlers and coroutine in handlers:
            handlers.remove(coroutine)

    def event(self, coroutine: EventHandler) -> EventHandler:
        if not inspect.iscoroutinefunction(coroutine):
            raise TypeError("Event handler must be async")
        if not coroutine.__name__.startswith("on_"):
            raise TypeError("Event handler name must start with on_")
        self._events[coroutine.__name__[3:]] = coroutine
        return coroutine

    def _spawn_background(
        self,
        awaitable,
        *,
        label: str,
    ) -> asyncio.Task:
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)

        def done_callback(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)

            if done_task.cancelled():
                return

            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return

            if exc is not None:
                print(
                    f"BACKGROUND ERROR {label}: "
                    f"{type(exc).__name__} {exc!r}"
                )

        task.add_done_callback(done_callback)
        return task

    def _event_aliases(self, name: str, event: object):
        aliases = []

        if name == "message" and isinstance(event, MessageEvent):
            aliases.append("message_" + event.action.value)

        elif name == "channel" and isinstance(event, ChannelEvent):
            aliases.append("channel_" + event.action.value)

        elif name == "channel_deleted":
            aliases.append("channel_delete")

        elif name == "community_deleted":
            aliases.append("community_delete")

        elif name == "community_leave":
            aliases.append("member_leave")
            if getattr(event, "is_self", False):
                aliases.append("self_community_leave")

        elif name == "call_detached":
            aliases.append("voice_detach")

        elif name == "typing_packet":
            # There is one typing packet (MESSAGE_SET_TYPING_INDICATOR); whether
            # it's start or stop is carried by the is_typing bool (field 6) in
            # the payload, not by the packet type. Read it from the decoded
            # fields the gateway attached to event.data["packet"].
            is_typing = None
            data = getattr(event, "data", None)
            if isinstance(data, dict):
                for field in data.get("packet") or []:
                    if isinstance(field, dict) and field.get("field") == 6:
                        is_typing = bool(field.get("value"))
                        break
            aliases.append("typing")
            if is_typing is True:
                aliases.append("typing_start")
            elif is_typing is False:
                aliases.append("typing_stop")

        return tuple(aliases)

    async def _run_event_handler(
        self,
        name: str,
        event: object,
    ) -> None:
        for extra in tuple(self._extra_listeners.get(name, ())):
            self._spawn_background(extra(event), label=f"listener:{name}")

        handler = self._events.get(name)
        if handler is None:
            return

        try:
            await handler(event)
        except Exception as exc:
            if name in {"command_error", "error"}:
                raise
            error_handler = self._events.get("error")
            if error_handler is not None:
                try:
                    await error_handler(EventErrorEvent(name, event, exc))
                    return
                except Exception:
                    pass
            print(f"EVENT ERROR {name}: {type(exc).__name__} {exc!r}")

    async def dispatch(self, name: str, event: object) -> None:
        # Resolve anyone waiting on this event (see wait_for).
        if self._waiters:
            still_waiting = []
            for waiter_name, check, future in self._waiters:
                if future.done():
                    continue
                if waiter_name != name:
                    still_waiting.append((waiter_name, check, future))
                    continue
                try:
                    matched = check(event) if check is not None else True
                except Exception:
                    matched = False
                if matched:
                    future.set_result(event)
                else:
                    still_waiting.append((waiter_name, check, future))
            self._waiters = still_waiting

        # A message can reach us twice: pushed inside a NOTIFICATION (mentions,
        # DMs) and again when a channel sweep notices the same message. Drop
        # the duplicate so handlers fire exactly once per message id.
        # Turn named gateway packets into friendly typed events
        # (on_member_join, on_role_add, on_friend_request, ...).
        if name == "packet" and self._typed_events is not None:
            try:
                built = self._typed_events.build(event)
            except Exception:
                built = None
            if built is not None:
                typed_name, typed_event = built
                await self.dispatch(typed_name, typed_event)

        if name == "message":
            message = getattr(event, "message", None)
            message_id = getattr(message, "id", None)
            if message_id:
                if message_id in self._recent_message_ids:
                    return
                self._recent_message_ids.add(message_id)
                self._recent_message_order.append(message_id)
                if len(self._recent_message_order) > 5000:
                    stale = self._recent_message_order.popleft()
                    self._recent_message_ids.discard(stale)

        if name == "community" and isinstance(event, CommunityEvent):
            self.communities[event.community.id] = event.community

        if name == "channel" and isinstance(event, ChannelEvent):
            self.channels[event.channel.id] = event.channel

        if name == "channel_deleted" and isinstance(
            event,
            ChannelDeletedEvent,
        ):
            cached = self.channels.pop(event.channel_id, None)
            event = ChannelDeletedEvent(
                sequence=event.sequence,
                channel_id=event.channel_id,
                community_id=event.community_id,
                channel_group_id=event.channel_group_id,
                cached_channel=cached,
                raw=event.raw,
            )

        if name == "community_deleted" and isinstance(
            event,
            CommunityDeletedEvent,
        ):
            cached = self.communities.pop(event.community_id, None)
            removed = tuple(
                channel
                for channel in self.channels.values()
                if channel.community_id == event.community_id
            )
            for channel in removed:
                self.channels.pop(channel.id, None)
            removed_group_ids = [
                group_id
                for group_id, group in self.channel_groups.items()
                if group.community_id == event.community_id
            ]
            for group_id in removed_group_ids:
                self.channel_groups.pop(group_id, None)
            event = CommunityDeletedEvent(
                sequence=event.sequence,
                community_id=event.community_id,
                cached_community=cached,
                removed_channels=removed,
                raw=event.raw,
            )

        if name == "community_leave" and isinstance(
            event,
            CommunityLeaveEvent,
        ):
            is_self = (
                self.user_id is not None
                and event.user_id == self.user_id
            )
            cached = None
            removed: tuple[Channel, ...] = ()
            if is_self:
                cached = self.communities.pop(
                    event.community_id,
                    None,
                )
                removed = tuple(
                    channel
                    for channel in self.channels.values()
                    if channel.community_id == event.community_id
                )
                for channel in removed:
                    self.channels.pop(channel.id, None)

            event = CommunityLeaveEvent(
                sequence=event.sequence,
                community_id=event.community_id,
                user_id=event.user_id,
                leave_reason=CommunityLeaveReason.from_value(event.leave_reason),
                is_self=is_self,
                cached_community=cached,
                removed_channels=removed,
                raw=event.raw,
            )

        if name == "message" and isinstance(event, MessageEvent):
            message = replace(
                event.message,
                _service=self.messages,
                _client=self,
            )
            event = replace(event, message=message)
            self.message_cache[message.id] = message
            try:
                self.cache.ingest_message(message)
            except Exception:
                pass

            if message.id in self._sent_message_ids:
                self._sent_message_ids.discard(message.id)
                if self.user_id is None:
                    self.user_id = message.user_id

        event_names = (name,) + self._event_aliases(name, event)
        for event_name in event_names:
            # An event is worth running if EITHER an @client.event handler or
            # an add_listener() handler is registered for it.
            if (
                event_name not in self._events
                and not self._extra_listeners.get(event_name)
            ):
                continue

            if name == "message":
                self._spawn_background(
                    self._run_event_handler(
                        event_name,
                        event,
                    ),
                    label="on_" + event_name,
                )
            else:
                await self._run_event_handler(
                    event_name,
                    event,
                )

        if (
            name == "message"
            and isinstance(event, MessageEvent)
            and event.action is MessageAction.CREATE
        ):
            self._spawn_background(
                self.process_commands(event.message),
                label="process_commands",
            )
            for listener in tuple(self._message_listeners):
                self._spawn_background(
                    listener(event.message),
                    label="message_listener",
                )


    @classmethod
    async def send_once(
        cls,
        token: str,
        container_id: str,
        content: str,
        *,
        community_id: Optional[str] = None,
        **kwargs,
    ):
        """Send one message and exit -- the fastest possible path.

        No login, no gateway, no preloading: exactly one HTTP request, because
        the send endpoint only needs the bearer token::

            await RootClient.send_once(TOKEN, channel_id, "hello",
                                       community_id=community_id)

        Use a normal client if you need anything else; this is for scripts
        whose whole job is to fire a single message.
        """
        client = cls(token=token, preload_caches=False)
        try:
            return await client.messages.send(
                container_id, content, community_id=community_id, **kwargs
            )
        finally:
            await client.close()

    async def community_detail(self, community_id: str, *, refresh: bool = False):
        """Return a community's full detail, fetching only if not cached.

        (``get_community`` is the synchronous cache-only lookup; this is the
        async one that will fetch when needed.)

        This is the lazy counterpart to eager preloading: the first call for a
        community does one ``GetExtended``, and every later call is free until
        you pass ``refresh=True``. Use it instead of :meth:`fetch_community`
        when you don't specifically need fresh data.
        """
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(community_id)
        if not refresh:
            cached = self._community_extended.get(normalized)
            if cached is not None:
                return cached
        return await self.fetch_community(normalized)

    async def refresh_communities(self, *, expand: bool = True) -> None:
        """Refresh the community list.

        With ``expand=False`` this is a single ListMine call and the
        per-community detail is fetched lazily by
        :meth:`get_community` (and cached).
        """
        communities = await self.community_service.list_mine()
        live_ids = {community.id for community in communities}
        for stale_id in tuple(set(self.communities) - live_ids):
            self.communities.pop(stale_id, None)
            self._community_extended.pop(stale_id, None)
            for cid, channel in tuple(self.channels.items()):
                if channel.community_id == stale_id: self.channels.pop(cid, None)
            for gid, group in tuple(self.channel_groups.items()):
                if group.community_id == stale_id: self.channel_groups.pop(gid, None)
        for community in communities:
            self.communities[community.id] = replace(
                community,
                _admin=self.community_admin,
                _service=self.community_service,
            )

        if not expand:
            self.cache_errors.clear()
            return

        results = await asyncio.gather(
            *(
                self.fetch_community(community.id)
                for community in communities
            ),
            return_exceptions=True,
        )
        self.cache_errors.clear()
        for community, result in zip(communities, results):
            if isinstance(result, Exception):
                self.cache_errors.append(
                    f"{community.id}: "
                    f"{type(result).__name__}: {result}"
                )

    async def resolve_channel(self, channel_id: str) -> Optional[Channel]:
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(channel_id)
        cached = self.channels.get(normalized)
        if cached is not None:
            return cached
        await self.refresh_communities()
        return self.channels.get(normalized)

    async def leave_community(
        self,
        community_id: str,
    ) -> None:
        """Leave a community and clear its local cached objects."""
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(community_id)
        await self.community_service.leave(normalized)

        self.communities.pop(normalized, None)

        removed_channel_ids = [
            channel_id
            for channel_id, channel in self.channels.items()
            if channel.community_id == normalized
        ]
        for channel_id in removed_channel_ids:
            self.channels.pop(channel_id, None)

        removed_group_ids = [
            group_id
            for group_id, group in self.channel_groups.items()
            if group.community_id == normalized
        ]
        for group_id in removed_group_ids:
            self.channel_groups.pop(group_id, None)

    async def fetch_community(
        self,
        community_id: str,
    ) -> CommunityExtended:
        result = await self.community_service.get_extended(community_id)
        community = replace(
            result.community,
            _admin=self.community_admin,
            _service=self.community_service,
        )
        groups = []
        for group in result.channel_groups:
            channels = tuple(replace(channel, _admin=self.community_admin, _client=self) for channel in group.channels)
            groups.append(replace(group, channels=channels, _admin=self.community_admin))
        roles = tuple(replace(role, community_id=community.id, _client=self) for role in result.roles)
        members = tuple(replace(member, community_id=community.id, _client=self) for member in result.members)
        result = replace(result, community=community, channel_groups=tuple(groups), roles=roles, members=members)
        self.communities[community.id] = community
        self._community_extended[community.id] = result
        try:
            self.cache.ingest_community(result)
        except Exception:
            pass
        stale_groups={gid for gid,g in self.channel_groups.items() if g.community_id==community.id}
        stale_channels={cid for cid,c in self.channels.items() if c.community_id==community.id}
        for group in groups:
            self.channel_groups[group.id]=group; stale_groups.discard(group.id)
            for channel in group.channels:
                self.channels[channel.id]=channel; stale_channels.discard(channel.id)
        for gid in stale_groups: self.channel_groups.pop(gid,None)
        for cid in stale_channels: self.channels.pop(cid,None)
        return result

    def get_channel_group(
        self,
        channel_group_id: str,
    ) -> Optional[ChannelGroup]:
        from .identifiers import normalize_root_guid

        return self.channel_groups.get(
            normalize_root_guid(channel_group_id)
        )

    async def create_community(self, name: str, **kwargs):
        community = await self.community_admin.create_community(
            name,
            **kwargs,
        )
        self.communities[community.id] = community
        return community

    async def clone_community(
        self,
        source_community_id: str,
        **kwargs,
    ):
        community = await self.community_admin.clone_community(
            source_community_id,
            **kwargs,
        )
        self.communities[community.id] = community
        return community

    def get_community(self, community_id: str) -> Optional[Community]:
        from .identifiers import normalize_root_guid

        community = self.communities.get(
            normalize_root_guid(community_id)
        )
        if community is not None:
            try:
                community = replace(
                    community,
                    _admin=self.community_admin,
                )
            except TypeError:
                pass
        return community

    def get_channel(self, channel_id: str) -> Optional[Channel]:
        from .identifiers import normalize_root_guid

        return self.channels.get(normalize_root_guid(channel_id))

    def get_message(self, message_id: str) -> Optional[Message]:
        from .identifiers import normalize_root_guid

        return self.message_cache.get(
            normalize_root_guid(message_id)
        )

    def messages_for_container(
        self,
        container_id: str,
    ) -> tuple[Message, ...]:
        from .identifiers import normalize_root_guid

        normalized = normalize_root_guid(container_id)
        return tuple(
            message
            for message in self.message_cache.values()
            if message.container_id == normalized
        )

    def _optional_token(self) -> str:
        if self.session is None:
            return ""
        return self.session.token

    def _require_token(self) -> str:
        """The bearer token for API calls.

        A session (from login) is the normal source, but a token passed to the
        constructor is enough on its own -- the API only needs the bearer
        header. That makes fire-and-forget use possible with zero login
        round-trips::

            client = RootClient(token=TOKEN)
            await client.messages.send(channel_id, "hi", community_id=cid)

        Anything that genuinely needs session state (the gateway, the hub URL)
        still requires a real login.
        """
        if self.session is not None:
            return self.session.token
        if self.token:
            return self.token
        raise RuntimeError(
            "Client is not logged in and no token was provided"
        )

    def _require_web_api_url(self) -> str:
        if self.session is None:
            raise RuntimeError("Client is not logged in")
        return self.session.web_api_url

    async def _initialize_authenticated_state(
        self,
        *,
        preload_caches: Optional[bool] = None,
    ) -> None:
        """Load the minimum authenticated identity and optional heavy caches.

        Large pools should use ``preload_caches=False``. This still validates
        the token with ``UserGrpcService/GetSelf`` and resolves ``user_id``,
        but skips the expensive community expansion and DM list prefetch.
        Those services remain available and load on demand.
        """
        should_preload = (
            self.preload_caches
            if preload_caches is None
            else bool(preload_caches)
        )

        # GetSelf and ListMine don't depend on each other, so run them
        # together -- one round-trip instead of two. On this API each hop is
        # ~300ms, so overlapping them is a real chunk of login time.
        if should_preload:
            user_result, communities_result = await asyncio.gather(
                self.users.get_self(),
                self.refresh_communities(expand=self.expand_communities),
                return_exceptions=True,
            )
            if isinstance(communities_result, Exception):
                self.cache_errors.append(
                    "Community cache: "
                    f"{type(communities_result).__name__}: {communities_result}"
                )
            if isinstance(user_result, Exception):
                raise user_result
        else:
            user_result = await self.users.get_self()

        self.user = replace(user_result, _service=self.users)
        self.user_id = self.user.id

        if not should_preload:
            return

        if self.preload_direct_messages:
            try:
                await self.direct_messages.list()
            except Exception as exc:
                self.cache_errors.append(
                    f"Direct-message cache: {type(exc).__name__}: {exc}"
                )


    # ------------------------------------------------------------------ #
    # Lazy heavy APIs
    # ------------------------------------------------------------------ #
    def _build_high(self):
        if self._high_api is None:
            from .structured_api import StructuredAPI

            self._high_api = StructuredAPI(self.transport, self._optional_token)
        return self._high_api

    def _build_raw(self):
        if self._raw_api is None:
            from .raw_api import RawAPI

            self._raw_api = RawAPI(self.transport, self._optional_token)
        return self._raw_api

    def _build_services(self):
        if self._service_facade is None:
            self._service_facade = RootServiceFacade(self._build_high())
        return self._service_facade

    def __getattr__(self, name):
        # Only reached when normal attribute lookup fails, so this costs
        # nothing on the hot path.
        if name in ("high", "structured", "grpc"):
            return self._build_high()
        if name in ("raw", "api"):
            return self._build_raw()
        if name in ("services", "root"):
            return self._build_services()
        target = _HIGH_ALIASES.get(name)
        if target is not None:
            # Cache it on the instance: the alias should be a stable object
            # (as it was when built eagerly), and later lookups skip
            # __getattr__ entirely.
            value = getattr(self._build_high(), target)
            setattr(self, name, value)
            return value
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )


    async def login(self, username: str, password: str) -> None:
        self.session = await self.auth.login(username, password)
        await self._initialize_authenticated_state()

    async def login_token(
        self,
        token: Optional[str] = None,
        *,
        device_id: Optional[str] = None,
        web_api_url: str = "https://api.rootapp.com/",
    ) -> None:
        """Authenticate this client with an existing Root client token."""
        resolved_token = token or self.token
        if not resolved_token:
            raise ValueError("Token is required")

        self.token = resolved_token.strip()
        self.session = await self.auth.session_from_token(
            self.token,
            device_id=device_id,
            web_api_url=web_api_url,
        )
        await self._initialize_authenticated_state()

    async def start_token(
        self,
        token: Optional[str] = None,
        *,
        device_id: Optional[str] = None,
        web_api_url: str = "https://api.rootapp.com/",
    ) -> None:
        """Start the API/gateway client with an existing Root client token."""
        await self.login_token(
            token,
            device_id=device_id,
            web_api_url=web_api_url,
        )
        await self.connect()
        assert self.gateway is not None
        await self.gateway.wait_closed()

    @classmethod
    async def create_account(
        cls,
        *,
        username: str,
        password: str,
        email: str,
        access_token: Optional[str] = None,
        command_prefix: str = ">",
        auto_install_media: bool = True,
    ) -> "RootClient":
        client = cls(
            username=username,
            password=password,
            command_prefix=command_prefix,
            auto_install_media=auto_install_media,
        )
        try:
            client.session = await client.auth.signup(
                username,
                password,
                email,
                access_token=access_token,
            )
            await client._initialize_authenticated_state()

            await client.transport.close()
            return client
        except Exception:
            await client.close()
            raise

    async def connect(self) -> None:
        if self.session is None:
            raise RuntimeError("Call login() before connect()")
        self.gateway = Gateway(
            hub_url=self.session.hub_url,
            token=self.session.token,
            device_id=self.session.device_id,
            dispatch=self.dispatch,
            auto_reconnect=self.auto_reconnect,
            reconnect_base=self.reconnect_base,
            reconnect_max=self.reconnect_max,
            max_reconnect_attempts=self.max_reconnect_attempts,
        )
        self.gateway.client_cache = self.cache
        await self.gateway.start()
        await self.dispatch(
            "ready",
            ReadyEvent(self.session.device_id, self.session.hub_url),
        )

    async def start(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        resolved_username = username or self.username
        resolved_password = password or self.password

        if self.token and not resolved_username and not resolved_password:
            await self.start_token(self.token)
            return

        if not resolved_username:
            raise ValueError("Username is required")
        if not resolved_password:
            raise ValueError("Password is required")

        self.username = resolved_username
        self.password = resolved_password

        await self.login(resolved_username, resolved_password)
        await self.connect()
        assert self.gateway is not None
        await self.gateway.wait_closed()

    async def close(self) -> None:
        try:
            if getattr(self.calls, "active_session", None) is not None:
                await self.calls.disconnect()
            else:
                await self.calls.stop_audio(close_peer=True)
        except Exception:
            pass
        if self.gateway is not None:
            await self.gateway.close()
            self.gateway = None

        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        self._background_tasks.clear()
        if self._owns_transport:
            await self.transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def wait_until_ready(self) -> None:
        if self.gateway is None:
            raise RuntimeError("Client is not connected")
        await self.gateway._ready.wait()

    @property
    def is_connected(self) -> bool:
        return self.gateway is not None and not self.gateway._closed.is_set()

    def run(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        async def runner() -> None:
            if self.auto_install_media:
                from .media_bootstrap import ensure_media_dependencies

                await asyncio.to_thread(ensure_media_dependencies)

            if self.token and not username and not password:
                resolved_username = None
                resolved_password = None
            else:
                resolved_username = (
                    username
                    or self.username
                    or input("Username: ").strip()
                )
                resolved_password = (
                    password
                    or self.password
                    or getpass.getpass("Password: ")
                )

            try:
                await self.start(resolved_username, resolved_password)
            finally:
                await self.close()

        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            pass