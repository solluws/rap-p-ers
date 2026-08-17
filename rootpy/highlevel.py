"""High-level convenience methods for :class:`~rootpy.client.RootClient`.

The rest of the library is deliberately layered: a raw gRPC-Web surface
(:class:`RawAPI`), a structured surface (:class:`StructuredAPI`), typed
services (``client.messages``, ``client.calls`` ...), and rich models
(``Message.reply``, ``CurrentUser.edit`` ...). That layering is great for
control but verbose for the common case of "do one thing to my own account".

This mixin adds a flat set of one-call verbs on the client itself so that the
everyday actions read the way you'd say them::

    await client.message(channel, "hello")
    await client.reply(msg, "on it")
    await client.call(friend)
    await client.update_username("new_name")

Every method here is a thin wrapper over an existing service call. Each one
acts on a single target: they are conveniences for driving *your own*
account, not batch/broadcast tools. Anything more elaborate (bulk edits,
community administration, permission rules, raw RPCs) stays available on the
underlying services and managers, which these wrappers simply delegate to.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, List, Optional, Union

from .commands import parse_channel_mention, parse_user_mention
from .events import MessageEvent, MessageAction, ChannelActivity
from .identifiers import normalize_root_guid
from .models import (
    Channel,
    CurrentUser,
    Message,
    MessageSendResult,
)
from .services.direct_messages import DirectMessage
from .users import User

log = logging.getLogger("rootpy.highlevel")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .services.calls import AudioPlayback, CallSession

# What a caller may hand us to name a person or a place.
UserTarget = Union[User, CurrentUser, str]
SendTarget = Union[Channel, User, CurrentUser, Message, DirectMessage, MessageSendResult, str]


class HighLevelMixin:
    """Flat, one-call helpers mixed into :class:`RootClient`.

    The mixin assumes the attributes that ``RootClient.__init__`` sets up
    (``self.messages``, ``self.calls``, ``self.users``, ``self.dm``,
    ``self.friends``, ``self.blocks``, ``self.notifications``,
    ``self.community`` ...). It never touches the network directly.
    """

    # ------------------------------------------------------------------ #
    # target resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _user_id(user: UserTarget) -> str:
        """Coerce a User / CurrentUser / id-string into a canonical id."""
        raw = getattr(user, "id", user)
        return normalize_root_guid(raw if isinstance(raw, str) else str(raw))

    async def _resolve_send_target(
        self,
        target: SendTarget,
    ) -> tuple[str, Optional[str]]:
        """Return ``(container_id, community_id)`` for a message destination.

        Accepts, in order of preference:

        * anything that already carries a container -- a received
          :class:`Message`, a :class:`Context`, or a
          :class:`MessageSendResult` (replies land in the same place);
        * a resolved :class:`Channel` (uses its community);
        * an open :class:`DirectMessage`;
        * a :class:`User` / :class:`CurrentUser` (opens or reuses the 1:1 DM);
        * a channel-mention or user-mention string;
        * a raw channel id (resolved so its community comes along).

        A raw *user* id is ambiguous (it is not itself a container), so to DM
        by id pass ``client.get_user(id)`` rather than the bare string.
        """
        # Objects that already know their container (messages, ctx, results).
        container = getattr(target, "container_id", None)
        if container:
            return (
                normalize_root_guid(container),
                getattr(target, "community_id", None),
            )

        if isinstance(target, Channel):
            return target.id, target.community_id

        if isinstance(target, DirectMessage):
            return target.id, None

        if isinstance(target, (User, CurrentUser)):
            dm = await self.dm.open(target)
            return dm.id, None

        if isinstance(target, str):
            channel_mention = parse_channel_mention(target)
            if channel_mention is not None:
                _, channel_id = channel_mention
                return await self._channel_container(channel_id)

            user_mention = parse_user_mention(target)
            if user_mention is not None:
                _, user_id = user_mention
                dm = await self.dm.open(user_id)
                return dm.id, None

            return await self._channel_container(normalize_root_guid(target))

        raise TypeError(
            "Unsupported message target "
            f"{target!r}: pass a Channel, a User, a received Message, "
            "or a channel/user mention."
        )

    async def _channel_container(
        self,
        channel_id: str,
    ) -> tuple[str, Optional[str]]:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.resolve_channel(channel_id)
        if channel is None:
            raise ValueError(
                f"Unknown channel {channel_id!r}. Pass a resolved Channel, or "
                "for a direct message pass a User (client.get_user(id))."
            )
        return channel.id, channel.community_id

    # ------------------------------------------------------------------ #
    # messaging
    # ------------------------------------------------------------------ #
    async def message(
        self,
        target: SendTarget,
        content: str,
        *,
        reply_to: Optional[Message] = None,
        attachments: Optional[List[str]] = None,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """Send ``content`` to ``target``.

        ``target`` may be a channel, a user (opens a DM), or anything that
        carries a container (reply in place). ``attachments`` is a list of
        already-uploaded asset token URIs -- use ``client.assets.upload_file``
        to turn a local path into one.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message content cannot be empty")

        container_id, community_id = await self._resolve_send_target(target)
        parents = [reply_to.id] if reply_to is not None else None
        return await self.messages.send(
            container_id,
            content,
            community_id=community_id,
            attachment_token_uris=attachments,
            parent_message_ids=parents,
            needs_parent_notification=reply_to is not None,
            delete_after=delete_after,
        )

    async def reply(
        self,
        message: Message,
        content: str,
        *,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """Reply to ``message`` in its own channel or DM."""
        return await self.message(
            message,
            content,
            reply_to=message,
            delete_after=delete_after,
        )

    async def edit_message(
        self,
        message: Message,
        content: str,
        *,
        uris: Optional[List[str]] = None,
    ) -> Message:
        """Edit one of your own messages."""
        return await self.messages.edit_message(message, content, uris=uris)

    async def delete_message(self, message: Message) -> None:
        """Delete a message you can delete."""
        await self.messages.delete_message(message)

    async def react(self, message: Message, emoji: str) -> None:
        """Add a reaction (unicode emoji or ``:shortcode:``)."""
        await self.messages.add_reaction(message, emoji)

    async def unreact(self, message: Message, emoji: str) -> None:
        """Remove a reaction you added."""
        await self.messages.remove_reaction(message, emoji)

    async def pin(self, message: Message) -> None:
        await self.messages.pin_message(message)

    async def unpin(self, message: Message) -> None:
        await self.messages.unpin_message(message)

    # ------------------------------------------------------------------ #
    # direct messages
    # ------------------------------------------------------------------ #
    async def open_dm(self, user: UserTarget) -> DirectMessage:
        """Open (or reuse) the 1:1 DM with ``user``.

        The returned object is attached to this client, so you can use it
        directly::

            dm = await client.open_dm(user_id)
            await dm.send("hey")
        """
        from dataclasses import replace as _replace

        conversation = await self.dm.open(user)
        try:
            return _replace(conversation, _client=self)
        except Exception:
            return conversation

    async def direct_message(
        self,
        user: UserTarget,
        content: str,
        *,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """DM ``user`` directly -- shorthand for ``message(user, ...)``."""
        return await self.dm.send(user, content, delete_after=delete_after)

    # ------------------------------------------------------------------ #
    # voice / calls
    # ------------------------------------------------------------------ #
    async def call(self, user: UserTarget) -> "CallSession":
        """Start a voice call with ``user`` (rings your 1:1 DM)."""
        target = user if isinstance(user, (User, CurrentUser)) else self.get_user(self._user_id(user))
        return await self.calls.call_user(target)

    async def join_voice(self, channel: Union[Channel, str]) -> "CallSession":
        """Join the voice ``channel`` (a Channel, or an id/mention to resolve)."""
        if not isinstance(channel, Channel):
            channel_id = channel
            mention = parse_channel_mention(channel) if isinstance(channel, str) else None
            if mention is not None:
                _, channel_id = mention
            resolved = self.get_channel(normalize_root_guid(channel_id))
            if resolved is None:
                resolved = await self.resolve_channel(normalize_root_guid(channel_id))
            if resolved is None:
                raise ValueError(f"Unknown voice channel {channel!r}")
            channel = resolved
        return await self.calls.join_channel(channel)

    async def leave_voice(self) -> None:
        """Leave the current call, if any."""
        await self.calls.disconnect()

    async def play(
        self,
        source: str,
        *,
        loop: bool = False,
    ) -> "AudioPlayback":
        """Play a local file or URL into the active call."""
        return await self.calls.play_audio(source, loop=loop)

    async def stop_playing(self) -> None:
        """Stop audio playback but stay in the call."""
        await self.calls.stop_audio(close_peer=False)

    async def mute(self, muted: bool = True) -> None:
        """Mute (or unmute) yourself in the active call."""
        container_id, community_id = self.calls.require_active_target()
        await self.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            muted=muted,
        )

    async def unmute(self) -> None:
        await self.mute(False)

    async def deafen(self, deafened: bool = True) -> None:
        """Deafen (or undeafen) yourself in the active call."""
        container_id, community_id = self.calls.require_active_target()
        await self.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            deafened=deafened,
        )

    async def undeafen(self) -> None:
        await self.deafen(False)

    # ------------------------------------------------------------------ #
    # profile / account
    # ------------------------------------------------------------------ #
    async def whoami(self, *, refresh: bool = False) -> CurrentUser:
        """Return the signed-in account, refreshing from the server if asked."""
        if refresh or self.user is None:
            from dataclasses import replace

            self.user = replace(await self.users.get_self(), _service=self.users)
            self.user_id = self.user.id
        return self.user

    async def update_username(self, username: str) -> None:
        """Change your username."""
        await self.users.set_username(username)
        if self.user is not None:
            from dataclasses import replace

            self.user = replace(self.user, username=username)

    async def update_status(self, status: Optional[str]) -> None:
        """Set (or clear) your custom status text."""
        await self.users.set_status(status)

    async def update_description(self, description: Optional[str]) -> None:
        """Set (or clear) your profile "about" description."""
        await self.users.set_description(description)

    async def change_profile_picture(self, source) -> Optional[str]:
        """Change your profile picture.

        ``source`` can be whatever you have to hand::

            await client.change_profile_picture("avatar.png")        # file
            await client.change_profile_picture(Path("~/pic.jpg"))   # path
            await client.change_profile_picture(image_bytes)         # bytes
            await client.change_profile_picture(open("a.png", "rb")) # file object
            await client.change_profile_picture("https://.../a.png") # URL
            await client.change_profile_picture(None)                # remove it

        Files, bytes and URLs are uploaded to Root's asset store first;
        the returned value is the resulting asset URI.
        """
        return await self.users.set_profile_picture(source)

    async def update_avatar(self, source) -> Optional[str]:
        """Alias for :meth:`change_profile_picture`."""
        return await self.change_profile_picture(source)

    async def remove_profile_picture(self) -> None:
        """Clear your profile picture."""
        await self.users.set_profile_picture(None)

    async def change_banner(self, source) -> Optional[str]:
        """Change your profile banner. Same source types as
        :meth:`change_profile_picture`."""
        return await self.users.set_banner(source)

    async def update_banner(self, source) -> Optional[str]:
        """Alias for :meth:`change_banner`."""
        return await self.change_banner(source)

    async def update_profile(
        self,
        *,
        username: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        avatar: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> CurrentUser:
        """Update several profile fields in one call (only the ones given)."""
        if username is not None:
            await self.update_username(username)
        if description is not None:
            await self.update_description(description)
        if status is not None:
            await self.update_status(status)
        if avatar is not None:
            await self.update_avatar(avatar)
        if banner is not None:
            await self.update_banner(banner)
        return await self.whoami()

    # ------------------------------------------------------------------ #
    # friends / blocks
    # ------------------------------------------------------------------ #
    async def add_friend(self, username: str):
        """Send a friend request to ``username``.

        Same as ``client.friend_requests.send(username)``.
        """
        return await self.friend_requests.send(username)

    async def send_friend_request(self, username: str):
        """Send a friend request to ``username``."""
        return await self.friend_requests.send(username)

    async def accept_friend_request(self, notification):
        """Accept an incoming friend request."""
        return await self.friend_requests.accept(notification)

    async def decline_friend_request(self, notification):
        """Decline an incoming friend request."""
        return await self.friend_requests.decline(notification)

    async def pending_friend_requests(self):
        """Incoming friend requests awaiting an answer."""
        return await self.friend_requests.pending()

    async def remove_friend(self, user: UserTarget):
        """Remove an existing friend."""
        return await self.friends.remove(self._user_id(user))

    async def list_friends(self):
        """List your current friends."""
        return await self.friends.list()

    async def block(self, user: UserTarget):
        """Block a user."""
        return await self.blocks.block(self._user_id(user))

    async def unblock(self, user: UserTarget):
        """Unblock a user."""
        return await self.blocks.unblock(self._user_id(user))

    async def list_blocked(self):
        """List users you've blocked."""
        return await self.blocks.list()

    # ------------------------------------------------------------------ #
    # notifications / communities (read-side conveniences)
    # ------------------------------------------------------------------ #
    async def list_notifications(self, **kwargs):
        """List your notifications."""
        return await self.notifications.list(**kwargs)

    async def unread_count(self):
        """How many notifications are unviewed."""
        return await self.notifications.count_unviewed()

    async def mark_all_read(self):
        """Mark every notification as viewed."""
        return await self.notifications.mark_all_viewed()

    async def wait_for(
        self,
        event: str,
        *,
        check=None,
        timeout: Optional[float] = None,
    ):
        """Wait for the next matching event and return it.

        Works with every event the client dispatches -- ``"message"``,
        ``"friend_request"``, ``"member_join"``, ``"role_add"``,
        ``"channel_create"``, and so on (no ``on_`` prefix)::

            # wait for a specific person to reply
            event = await client.wait_for(
                "message",
                check=lambda e: e.message.user_id == user_id,
                timeout=30,
            )
            print(event.message.content)

            # wait for anyone to send a friend request
            request = await client.wait_for("friend_request", timeout=60)
            print(request.username or request.user_id)

        Raises :class:`asyncio.TimeoutError` if ``timeout`` elapses.
        """
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((event, check, future))
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._waiters = [
                entry for entry in self._waiters if entry[2] is not future
            ]

    async def set_online_status(self, status):
        """Set your presence: ``"online"``, ``"idle"``, or ``"invisible"``.

            await client.set_online_status("idle")

        Root has no do-not-disturb state; presence is active / inactive /
        disconnected.
        """
        return await self.users.set_online_status(status)

    async def go_online(self):
        """Shorthand for ``set_online_status("online")``."""
        return await self.set_online_status("online")

    async def go_idle(self):
        """Shorthand for ``set_online_status("idle")``."""
        return await self.set_online_status("idle")

    async def go_invisible(self):
        """Shorthand for ``set_online_status("invisible")``."""
        return await self.set_online_status("invisible")

    async def get_profile(self, user_id: str):
        """A user's public profile: username, pictures, about-me, status.

            profile = await client.get_profile(user_id)
            print(profile.username, profile.about_me)
            print(profile.avatar_url, profile.banner_uri)
        """
        profile = await self.users.get_profile(user_id)
        if profile is not None:
            self.cache.remember_user(profile.user_id, profile.username)
        return profile

    async def get_profiles(self, user_ids) -> dict:
        """Profiles for many users in ONE request -> ``{user_id: UserProfile}``.

        Batching matters: fetching 200 members one at a time is 200 requests,
        this is one.
        """
        profiles = await self.users.get_profiles(user_ids)
        for profile in profiles.values():
            self.cache.remember_user(profile.user_id, profile.username)
        return profiles

    async def get_members_detailed(
        self,
        community_id: str,
        *,
        refresh: bool = False,
        batch_size: int = 100,
    ):
        """Members of a community **with their profiles attached**.

            for member in await client.get_members_detailed(community_id):
                print(member.username, member.about_me)
                print(member.profile_picture_uri, member.banner_uri)

        Profiles are fetched in batches (one request per ``batch_size`` users)
        rather than one request per member, and every name learned is fed into
        the client's cache so later events can resolve it for free.

        Returns :class:`DetailedMember` objects -- the member record plus its
        profile, with the profile fields readable directly.
        """
        from .models import DetailedMember

        members = await self.get_members(community_id, refresh=refresh)
        ids = [m.user_id for m in members if m.user_id]

        profiles: dict = {}
        for start in range(0, len(ids), max(1, batch_size)):
            chunk = ids[start:start + batch_size]
            try:
                profiles.update(await self.get_profiles(chunk))
            except Exception as exc:
                log.debug("profile batch failed: %s", exc)

        return tuple(
            DetailedMember(member=m, profile=profiles.get(m.user_id))
            for m in members
        )

    async def get_members(self, community_id: str, *, refresh: bool = False):
        """Every member of a community.

            for member in await client.get_members(community_id):
                print(member.user_id, member.role_ids)

        Members come from the community's ``GetExtended`` payload, which is
        cached -- so repeated calls are free until you pass ``refresh=True``.
        Each member is attached to this client, so ``member.add_role(...)``,
        ``member.kick()`` and friends work directly.

        Note that Root's member records carry ids and roles, not display
        names; use :meth:`member_names` if you want names filled in where the
        client has seen them.
        """
        detail = await self.community_detail(community_id, refresh=refresh)
        return tuple(detail.members)

    async def get_member(self, community_id: str, user_id: str, *,
                         refresh: bool = False):
        """One member of a community, or None if they aren't in it."""
        from .identifiers import normalize_root_guid

        target = normalize_root_guid(user_id)
        for member in await self.get_members(community_id, refresh=refresh):
            if member.user_id == target:
                return member
        return None

    def timings(self) -> dict:
        """Where this client's time has gone.

            print(client.timing_report())

        Returns totals plus a per-endpoint breakdown::

            {"totals": {"calls": 12, "roundtrip_ms": 2043.1,
                        "waiting_ms": 0.0, "overhead_ms": 3.2, ...},
             "endpoints": {"root.UserGrpcService/GetSelf": {...}, ...}}

        ``roundtrip_ms`` is network + server together -- those can't be
        separated from inside the process. ``waiting_ms`` is time this client
        deliberately held requests back (rate-limit cooldowns, retry backoff),
        and ``overhead_ms`` is our own framing and bookkeeping.
        """
        return self.transport.stats.summary()

    def timing_report(self) -> str:
        """The same information as a readable table."""
        return self.transport.stats.report()

    def reset_timings(self) -> None:
        """Clear the counters -- useful around a specific operation."""
        self.transport.stats.reset()

    async def get_random_member(
        self,
        community_id: str,
        *,
        refresh: bool = False,
        exclude_self: bool = True,
        with_profile: bool = True,
    ):
        """Pick a random member of a community, or None if there are none.

            member = await client.get_random_member(community_id)
            print(member.user_id, member.username)
            print(member.avatar_url, member.about_me)

        Returns a :class:`DetailedMember`: the member record (user id, role
        ids, and the ``add_role`` / ``kick`` / ``ban`` actions) plus their
        profile -- username, profile picture, banner, about-me and status.

        exclude_self:  leave yourself out of the draw (default).
        with_profile:  fetch the chosen member's profile. On by default since
                       a bare user id usually isn't much use; it costs one
                       request, and only for the member actually picked. Set
                       False to get just the member record.
        """
        import random

        from .models import DetailedMember

        members = list(await self.get_members(community_id, refresh=refresh))
        if exclude_self and self.user_id:
            members = [m for m in members if m.user_id != self.user_id]
        if not members:
            return None

        chosen = random.choice(members)
        if not with_profile:
            return chosen

        profile = None
        try:
            profile = await self.get_profile(chosen.user_id)
        except Exception as exc:
            log.debug("random member profile fetch failed: %s", exc)
        # Fall back to a cached username if the fetch didn't work, so the
        # caller still gets a name where we have one.
        if profile is None:
            from .models import UserProfile

            cached_name = self.cache.username(chosen.user_id)
            if cached_name:
                profile = UserProfile(
                    user_id=chosen.user_id, username=cached_name
                )
        return DetailedMember(member=chosen, profile=profile)

    async def get_random_members(
        self,
        community_id: str,
        count: int = 1,
        *,
        refresh: bool = False,
        exclude_self: bool = True,
    ):
        """Pick several distinct random members (fewer if the server is small)."""
        import random

        members = list(await self.get_members(community_id, refresh=refresh))
        if exclude_self and self.user_id:
            members = [m for m in members if m.user_id != self.user_id]
        if not members:
            return ()
        return tuple(random.sample(members, min(max(1, count), len(members))))

    async def member_count(self, community_id: str, *, refresh: bool = False) -> int:
        """How many members a community has, as reported by the server."""
        return len(await self.get_members(community_id, refresh=refresh))

    async def member_names(self, community_id: str, *, refresh: bool = False):
        """Members as ``{user_id: username or None}``.

        Root doesn't put display names on member records, so a name only
        appears if the client has seen that user somewhere that does carry one
        (a notification, or a user fetch). Unknown names come back as ``None``
        rather than triggering a request per member.
        """
        members = await self.get_members(community_id, refresh=refresh)
        return {
            member.user_id: self.cache.username(member.user_id)
            for member in members
        }

    async def members_with_role(self, community_id: str, role_id: str, *,
                                refresh: bool = False):
        """Members holding a particular role."""
        from .identifiers import normalize_root_guid

        target = normalize_root_guid(role_id)
        return tuple(
            member
            for member in await self.get_members(community_id, refresh=refresh)
            if target in (member.role_ids or ())
        )

    async def list_communities(self, *, refresh: bool = True):
        """List the communities you're in."""
        return await self.community.servers(refresh=refresh)

    async def list_messages(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        direction: str = "older",
        after: Optional[float] = None,
        limit: Optional[int] = 50,
    ):
        """Fetch message history for a channel or DM (see MessageService.list)."""
        return await self.messages.list(
            container_id,
            community_id=community_id,
            direction=direction,
            after=after,
            limit=limit,
        )

    def history(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        limit: Optional[int] = 200,
        before: Optional[float] = None,
        page_size: int = 50,
    ):
        """Iterate a channel's or DM's history, newest first.

            async for message in client.history(channel_id, limit=500):
                print(message.content)
        """
        return self.messages.history(
            container_id,
            community_id=community_id,
            limit=limit,
            before=before,
            page_size=page_size,
        )

    def watch_channel(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        interval: float = 5.0,
        limit: int = 50,
    ):
        """Poll a channel for new messages and dispatch them live-ish.

        The hub doesn't push plain channel messages, so this fills the gap by
        polling ``messages.list`` on an interval, tracking which message ids
        it has seen, and dispatching only genuinely new ones through the normal
        ``on_message`` path (so message listeners and ``on_message`` handlers
        fire, exactly like a DM or mention would).

        The initial poll is treated as the baseline -- existing history is
        recorded as "seen" but not replayed, so you only get messages that
        arrive after you start watching. Returns the ``asyncio.Task``; cancel
        it (or call :meth:`unwatch_all`) to stop.

        Watch specific channels you care about; polling every channel in every
        community would be a lot of requests.
        """
        container_id = normalize_root_guid(container_id)

        async def _poll() -> None:
            seen: set = set()
            primed = False
            while True:
                try:
                    msgs = await self.messages.list(
                        container_id,
                        community_id=community_id,
                        direction="older",
                        limit=limit,
                    )
                    for m in msgs:
                        if not m.id or m.id in seen:
                            continue
                        seen.add(m.id)
                        if primed:
                            # dispatch() fans out to listeners itself.
                            await self.dispatch(
                                "message",
                                MessageEvent(None, m, MessageAction.CREATE, b""),
                            )
                    primed = True
                    if len(seen) > 5000:
                        seen = set(list(seen)[-2000:])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("watch_channel poll failed: %s", exc)
                await asyncio.sleep(interval)

        task = asyncio.create_task(_poll())
        self._watch_tasks.append(task)
        return task

    def unwatch_all(self) -> None:
        """Stop all channel watchers started with :meth:`watch_channel`."""
        for task in self._watch_tasks:
            task.cancel()
        self._watch_tasks = []

    def watch_unread(
        self,
        *,
        interval: float = 3.0,
        include_dms: bool = True,
        mark_read: bool = False,
        callback=None,
        concurrency: int = 8,
        community_refresh: float = 120.0,
        dm_every: int = 20,
    ):
        """Watch every channel the account can see, driven by unread state.

        Root gives each channel a ``last_activity_at`` (when it last got a
        message) and ``user_last_viewed_at`` (when you last read it) -- one
        ``GetExtended`` per community returns these for *all* its channels. So
        detecting new activity everywhere costs one request per community, and
        only channels whose activity advanced get their messages fetched.

        New messages are dispatched through the normal ``on_message`` path, so
        ``@client.event on_message`` handlers and message listeners fire, and
        they're deduplicated against messages the gateway already pushed (a
        mention arrives both ways -- you'll only see it once).

        Efficiency: community sweeps run **concurrently** (``concurrency``
        at a time) rather than one after another, and the community list is
        cached and only refreshed every ``community_refresh`` seconds.

        DMs are swept rarely (every ``dm_every`` sweeps, default 20) because
        the sweep costs **one request per conversation** -- an account with 100
        DMs polling every 3rd sweep would issue ~670 requests a minute -- and
        DM messages already arrive instantly as pushed notifications. The sweep
        is only a safety net for anything the push missed. Set
        ``include_dms=False`` to skip it entirely.

        The first sweep is a baseline -- existing activity is recorded but not
        replayed, so you only get messages that arrive after you start.

        interval: seconds between sweeps (detection latency).
        mark_read: mark channels viewed after surfacing (off by default).
        callback: optional ``async (message) -> None``.

        Returns the ``asyncio.Task``; cancel it or call :meth:`unwatch_all`.
        """
        limiter = asyncio.Semaphore(max(1, concurrency))

        async def _dispatch(message) -> None:
            # client.dispatch() already fans out to message listeners and
            # dedupes by message id -- don't invoke listeners again here.
            await self.dispatch(
                "message",
                MessageEvent(None, message, MessageAction.CREATE, b""),
            )
            if callback is not None:
                self._spawn_background(callback(message), label="unread_callback")

        async def _fetch_new(channel, community_id, prev, primed) -> None:
            """Fetch and dispatch messages posted since we last saw this channel.

            Uses direction=NEWER with the previous ``last_activity_at`` as the
            cursor, so the server returns only messages newer than that -- no
            history to filter, nothing to prime, and the first new message in a
            channel shows up like any other. Duplicates are filtered by id in
            ``client.dispatch`` regardless.
            """
            after = prev.timestamp() if prev is not None else None
            try:
                async with limiter:
                    msgs = await self.messages.list(
                        channel.id,
                        community_id=community_id,
                        direction="newer",
                        after=after,
                        limit=None,
                    )
            except Exception as exc:
                log.debug("watch_unread: list %s failed: %s", channel.id, exc)
                msgs = []
            if not msgs:
                # No text available -- still report that the channel changed.
                if primed:
                    await self.dispatch(
                        "channel_activity",
                        ChannelActivity(
                            channel_id=channel.id,
                            channel_name=getattr(channel, "name", "") or "",
                            community_id=community_id,
                            last_activity_at=channel.last_activity_at,
                        ),
                    )
                return
            for message in msgs:
                await _dispatch(message)
            if mark_read:
                try:
                    await self.mark_channel_read(
                        channel.id, community_id=community_id
                    )
                except Exception:
                    pass

        async def _sweep_community(community, seen: dict, primed: bool) -> None:
            """One GetExtended -> fetch only the channels that changed."""
            try:
                async with limiter:
                    ext = await self.fetch_community(community.id)
            except Exception as exc:
                log.debug(
                    "watch_unread: GetExtended %s failed: %s", community.id, exc
                )
                return
            changed = []
            for group in ext.channel_groups:
                for channel in group.channels:
                    activity = channel.last_activity_at
                    if activity is None:
                        continue
                    # Only text-capable channels hold messages. Voice (4) and
                    # app (8) channels have no message container, and calling
                    # the message RPCs against them fails server-side.
                    if int(getattr(channel, "channel_type", 0) or 0) not in (
                        1,  # TEXT
                        2,  # THREADED_TEXT
                    ):
                        continue
                    prev = seen.get(channel.id)
                    seen[channel.id] = activity
                    if primed and prev is not None and activity > prev:
                        changed.append((channel, prev))
            if changed:
                await asyncio.gather(
                    *(
                        _fetch_new(channel, community.id, prev, primed)
                        for channel, prev in changed
                    ),
                    return_exceptions=True,
                )

        async def _sweep_dms(dm_seen: dict, primed: bool) -> None:
            try:
                dms = await self.direct_messages.list()
            except Exception as exc:
                log.debug("watch_unread: DM list failed: %s", exc)
                return

            async def _one(dm) -> None:
                try:
                    async with limiter:
                        msgs = await self.messages.list(
                            dm.id, direction="older", limit=20
                        )
                except Exception:
                    return
                seen_ids = dm_seen.setdefault(dm.id, set())
                for message in msgs:
                    if not message.id or message.id in seen_ids:
                        continue
                    seen_ids.add(message.id)
                    if primed:
                        await _dispatch(message)
                if len(seen_ids) > 2000:
                    dm_seen[dm.id] = set(list(seen_ids)[-1000:])

            await asyncio.gather(*(_one(dm) for dm in dms), return_exceptions=True)

        async def _loop() -> None:
            seen: dict = {}
            dm_seen: dict = {}
            primed = False
            communities: tuple = ()
            last_refresh = 0.0
            sweeps = 0

            # Don't start sweeping until login has completed, otherwise the
            # first passes just fail with "not logged in".
            while self.user_id is None:
                await asyncio.sleep(0.25)

            while True:
                started = asyncio.get_event_loop().time()
                try:
                    if not communities or (started - last_refresh) > community_refresh:
                        try:
                            communities = tuple(await self.get_communities())
                            last_refresh = started
                        except Exception as exc:
                            log.debug("watch_unread: community list failed: %s", exc)

                    if communities:
                        await asyncio.gather(
                            *(
                                _sweep_community(community, seen, primed)
                                for community in communities
                            ),
                            return_exceptions=True,
                        )

                    if include_dms and (sweeps % max(1, dm_every) == 0):
                        await _sweep_dms(dm_seen, primed)

                    if not primed:
                        log.debug(
                            "watch_unread: baseline set for %d channels across "
                            "%d communities",
                            len(seen),
                            len(communities),
                        )
                    primed = True
                    sweeps += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("watch_unread sweep failed: %s", exc)

                # Keep a steady cadence: subtract the time the sweep took.
                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(0.5, interval - elapsed))

        task = asyncio.create_task(_loop())
        self._watch_tasks.append(task)
        return task

    async def mark_channel_read(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
    ) -> None:
        """Mark a channel/DM as viewed up to now (SetViewTime)."""
        await self.messages.set_view_time(
            normalize_root_guid(container_id),
            community_id=normalize_root_guid(community_id) if community_id else None,
        )

    # ------------------------------------------------------------------ #
    # message listeners / auto-react
    # ------------------------------------------------------------------ #
    def add_message_listener(self, listener):
        """Register an ``async (message) -> None`` called on each new message.

        Unlike ``@client.event`` (one handler per event), you can register any
        number of listeners. Each runs in its own background task, so one
        slow or failing listener doesn't block the others or the gateway.
        Returns the listener so it can be used as a decorator.
        """
        import inspect

        if not inspect.iscoroutinefunction(listener):
            raise TypeError("message listener must be an async function")
        self._message_listeners.append(listener)
        return listener

    def remove_message_listener(self, listener) -> bool:
        """Remove a previously added listener. Returns True if it was present."""
        try:
            self._message_listeners.remove(listener)
            return True
        except ValueError:
            return False

    def enable_auto_react(
        self,
        emojis,
        *,
        only_self: bool = True,
        containers=None,
        stop_on_error: bool = False,
    ):
        """Automatically react to new messages with ``emojis``.

        By default this reacts only to *your own* messages -- the "hearts under
        everything I post" effect -- adding each emoji in ``emojis`` once. Root
        renders each distinct reaction separately, so pass several heart
        variants (e.g. ``["❤️","🧡","💛","💚","💙","💜"]``) for a burst.

        Parameters
        ----------
        emojis:
            A single emoji or an iterable of them. Any form ``react`` accepts
            works: unicode (``"❤️"``), shortcode (``":heart:"``), or a Root
            custom-emoji mention.
        only_self:
            When True (default) only your own messages are reacted to. Set
            False to react to every message you can see in scope -- use this
            sparingly and only where it's welcome; blanket-reacting to other
            people's messages is exactly the kind of noise that gets a client
            flagged.
        containers:
            Optional iterable of channel/DM ids to limit the effect to. When
            omitted, all containers in scope are eligible.
        stop_on_error:
            When False (default) a failed reaction is ignored (e.g. the emoji
            isn't available in that community); when True the error propagates.

        Calling this again replaces the previous configuration. Returns the
        registered listener. Pair with :meth:`disable_auto_react`.
        """
        if isinstance(emojis, str):
            emoji_list = [emojis]
        else:
            emoji_list = [e for e in emojis if isinstance(e, str) and e.strip()]
        if not emoji_list:
            raise ValueError("Provide at least one emoji")

        allowed = None
        if containers is not None:
            allowed = {normalize_root_guid(c) for c in containers}

        # Replace any existing auto-react listener.
        self.disable_auto_react()

        async def _auto_react(message: Message) -> None:
            if only_self:
                if self.user_id is None or message.user_id != self.user_id:
                    return
            if allowed is not None:
                if normalize_root_guid(message.container_id) not in allowed:
                    return
            for emoji in emoji_list:
                try:
                    await self.react(message, emoji)
                except Exception:
                    if stop_on_error:
                        raise
                    # Otherwise skip this emoji (unavailable/rate-limited) and
                    # keep going with the rest.
                    continue

        self._auto_react_listener = _auto_react
        self.add_message_listener(_auto_react)
        return _auto_react

    def disable_auto_react(self) -> bool:
        """Turn off auto-react. Returns True if it was enabled."""
        listener = getattr(self, "_auto_react_listener", None)
        if listener is None:
            return False
        self.remove_message_listener(listener)
        self._auto_react_listener = None
        return True

