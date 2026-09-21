from __future__ import annotations
from typing import Optional, List

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .permissions import ChannelPermissions, CommunityPermission


# --------------------------------------------------------------------------
# Mentions
#
# Root writes mentions into message content as markdown links, not as the
# Discord-style ``<@id>`` / ``<#id>`` that everyone assumes:
#
#     [@someone](root://user/002cb818-e400-8001-9999-999999999999)
#     [#general](root://channel/002cb818-e400-8004-aaaa-aaaaaaaaaaaa)
#
# These builders produce exactly the shape ``rootpy.commands`` parses
# (``USER_MENTION_RE`` and ``CHANNEL_MENTION_RE``), so a mention built here
# round-trips through the parser.
# --------------------------------------------------------------------------
def build_user_mention(user_id: str, username: Optional[str] = None) -> str:
    """A mention of ``user_id`` in Root's message syntax."""
    label = (username or str(user_id)).lstrip("@")
    return f"[@{label}](root://user/{user_id})"


def build_channel_mention(channel_id: str, name: Optional[str] = None) -> str:
    """A mention of ``channel_id`` in Root's message syntax."""
    label = (name or str(channel_id)).lstrip("#")
    return f"[#{label}](root://channel/{channel_id})"


@dataclass(frozen=True)
class AuthenticationSession:
    token: str
    device_id: str
    hub_url: str
    web_api_url: str


@dataclass(frozen=True)
class CallSession:
    session_id: Optional[str]
    container_id: str
    command_id: str
    audio_bandwidth: int = 0
    video_bandwidth: int = 0
    screen_bandwidth: int = 0
    screen_audio_bandwidth: int = 0
    #: :class:`~rootpy.enums.WebRtcBackend` -- 0 unspecified, 1 V1, 2 V2.
    #: A live server returns **2** today.
    backend: int = 0
    #: LiveKit websocket URL for a v2 session, else None. Root stopped
    #: negotiating media itself: a v2 answer carries no :attr:`session_id`
    #: and no session description, and gives you this plus
    #: :attr:`access_token` to join the room with. rootpy does the
    #: signalling and does not bundle a LiveKit client, so the join is
    #: yours.
    server_url: Optional[str] = None
    #: The JWT that goes with :attr:`server_url`, else None.
    access_token: Optional[str] = None

    @property
    def is_v2(self) -> bool:
        """True when this session is a LiveKit route rather than a v1 id."""
        return bool(self.server_url and self.access_token)


@dataclass(frozen=True)
class MessageAttachment:
    asset_uri: str
    filename: str = ""
    length: int = 0
    mime_type: Optional[str] = None
    modified_at: Optional[object] = None
    file_type: Optional[str] = None
    download_url: Optional[str] = None
    raw: bytes = b""

    @property
    def source(self) -> str:
        return self.download_url or self.asset_uri

    @property
    def display_name(self) -> str:
        if self.filename:
            return self.filename
        path = PurePosixPath(self.asset_uri.split("?", 1)[0])
        return path.name or self.asset_uri

    @property
    def is_audio(self) -> bool:
        if self.mime_type and self.mime_type.casefold().startswith("audio/"):
            return True
        suffix = PurePosixPath(self.display_name.casefold()).suffix
        return suffix in {
            ".aac", ".aiff", ".alac", ".flac", ".m4a",
            ".mp3", ".oga", ".ogg", ".opus", ".wav",
            ".webm", ".wma",
        }


@dataclass(frozen=True)
class Message:
    id: str
    container_id: str
    user_id: str
    content: str
    community_id: Optional[str] = None
    packet_type: int = 0
    message_type: int = 0
    deleted_at: Optional[object] = None
    edited_at: Optional[object] = None
    pinned_at: Optional[object] = None
    payload_raw: Optional[bytes] = None
    attachments: tuple[MessageAttachment, ...] = ()
    raw: bytes = b""
    _service: Optional[object] = field(
        default=None,
        repr=False,
        compare=False,
    )
    _client: Optional[object] = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def author(self):
        """Return the message author as a rootpy.User."""
        if self._client is None:
            raise RuntimeError(
                "This Message is not attached to a RootClient"
            )
        return self._client.get_user(self.user_id)

    def _require_service(self):
        if self._service is None:
            raise RuntimeError(
                "This Message is not attached to a RootClient"
            )
        return self._service

    async def delete(self) -> None:
        await self._require_service().delete_message(self)

    async def reply_with(self, others, content: str, **kwargs) -> "MessageSendResult":
        """Reply to this message AND others, in one message.

            await first.reply_with([second, third], "answering all three")

        Root allows up to 5 targets in total, this one included.

        Returns a :class:`MessageSendResult`, not a :class:`Message` -- Root's
        send response carries the new id and container, not a full record.
        These three send methods were annotated ``-> "Message"``, so a type
        checker agreed with code like ``(await m.reply("hi")).author`` right up
        until it raised at runtime.
        """
        from .services.messages import normalise_reply_targets

        if not isinstance(others, (list, tuple)):
            others = [others]
        targets = normalise_reply_targets([self, *others])
        return await self._require_service().send(
            self.container_id,
            content,
            community_id=self.community_id,
            parent_message_ids=targets,
            **kwargs,
        )

    async def reply(self, content: str, **kwargs) -> "MessageSendResult":
        """Reply to this message in its own channel.

            await message.reply("got it")

        Returns a :class:`MessageSendResult` (id + container), not a
        :class:`Message`. To reply to several messages at once, see
        :meth:`reply_with` or ``client.reply_to([...], text)``.
        """
        return await self._require_service().send(
            self.container_id,
            content,
            community_id=self.community_id,
            parent_message_ids=[self.id],
            **kwargs,
        )

    async def send_to_channel(self, content: str, **kwargs) -> "MessageSendResult":
        """Send a new (non-reply) message to the same channel.

        Returns a :class:`MessageSendResult`, not a :class:`Message`.
        """
        return await self._require_service().send(
            self.container_id,
            content,
            community_id=self.community_id,
            **kwargs,
        )

    @property
    def is_dm(self) -> bool:
        """True if this message came from a direct message, not a community."""
        return not self.community_id

    async def edit(
        self,
        content: str,
        *,
        uris: Optional[List[str]] = None,
    ) -> "Message":
        return await self._require_service().edit_message(
            self,
            content,
            uris=uris,
        )

    async def pin(self) -> None:
        await self._require_service().pin_message(self)

    async def unpin(self) -> None:
        await self._require_service().unpin_message(self)

    async def react(self, reaction: str) -> None:
        await self._require_service().add_reaction(
            self,
            reaction,
        )

    async def unreact(self, reaction: str) -> None:
        await self._require_service().remove_reaction(
            self,
            reaction,
        )

    async def flag(self, reason: int) -> None:
        await self._require_service().flag_message(
            self,
            reason,
        )

    @property
    def first_attachment(self) -> Optional[MessageAttachment]:
        return self.attachments[0] if self.attachments else None

    @property
    def first_audio_attachment(self) -> Optional[MessageAttachment]:
        for attachment in self.attachments:
            if attachment.is_audio:
                return attachment
        return None

    @property
    def is_deleted(self) -> bool:
        """True when Root returned this message as a tombstone.

        MessageList includes deleted messages with ``deleted_at`` set rather
        than dropping them, so anything reading history needs to know the
        difference between "still there" and "a record that it was removed".
        """
        return self.deleted_at is not None

    @property
    def first_attachment_url(self) -> Optional[str]:
        attachment = self.first_attachment
        return attachment.source if attachment is not None else None


@dataclass(frozen=True)
class MessageSendResult:
    id: Optional[str]
    container_id: str
    community_id: Optional[str]
    content: str
    command_id: str



@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    email: str = ""
    profile_picture_asset_uri: Optional[str] = None
    is_email_verified: bool = False
    max_online_status: int = 0
    description: Optional[str] = None
    banner_asset_uri: Optional[str] = None
    user_defined_status: Optional[str] = None
    is_billable: bool = False
    raw: bytes = b""
    _service: Optional[object] = field(
        default=None,
        repr=False,
        compare=False,
    )

    def _require_service(self):
        if self._service is None:
            raise RuntimeError(
                "This CurrentUser is not attached to a RootClient"
            )
        return self._service

    async def set_username(self, username: str) -> None:
        await self._require_service().set_username(username)
        object.__setattr__(self, "username", username)

    async def set_description(
        self,
        description: Optional[str],
    ) -> None:
        await self._require_service().set_description(
            description
        )
        object.__setattr__(
            self,
            "description",
            description,
        )

    async def set_status(
        self,
        status: Optional[str],
    ) -> None:
        await self._require_service().set_status(status)
        object.__setattr__(
            self,
            "user_defined_status",
            status,
        )

    async def set_avatar(
        self,
        source: Optional[str],
    ) -> Optional[str]:
        asset_uri = await self._require_service().set_profile_picture(
            source
        )
        object.__setattr__(
            self,
            "profile_picture_asset_uri",
            asset_uri,
        )
        return asset_uri

    async def set_profile_picture(
        self,
        source: Optional[str],
    ) -> Optional[str]:
        return await self.set_avatar(source)

    async def set_banner(
        self,
        source: Optional[str],
    ) -> Optional[str]:
        asset_uri = await self._require_service().set_banner(
            source
        )
        object.__setattr__(
            self,
            "banner_asset_uri",
            asset_uri,
        )
        return asset_uri

    async def edit(
        self,
        *,
        username: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        avatar: Optional[str] = None,
        banner: Optional[str] = None,
    ) -> "CurrentUser":

        if username is not None:
            await self.set_username(username)
        if description is not None:
            await self.set_description(description)
        if status is not None:
            await self.set_status(status)
        if avatar is not None:
            await self.set_avatar(avatar)
        if banner is not None:
            await self.set_banner(banner)
        return self


@dataclass(frozen=True)
class Community:
    id: str
    owner_user_id: Optional[str]
    default_channel_id: Optional[str]
    name: str
    picture_hex: str = ""
    picture_asset_uri: Optional[str] = None
    reject_unverified_email: bool = False
    description: Optional[str] = None
    is_age_restricted: bool = False
    packet_type: int = 0
    raw: bytes = b""
    #: The four below are declared last so
    #: the positional constructor keeps working, the same rule
    #: ``CommunityMember.joined_at`` follows.
    #:
    #: Which directory category the community lists under.
    #: :class:`~rootpy.enums.CommunityCategory`. Carried by every
    #: community message: field 18 of ``CommunityPacket`` and field 27 of
    #: ``CommunityGetExtendedResponse``.
    category: int = 0
    #: The wide banner shown at the top of the community, separate from
    #: :attr:`picture_asset_uri`. A ``StringValue``, so None means the
    #: server sent no banner rather than an empty one.
    banner_asset_uri: Optional[str] = None
    #: Whether the community is listed in Community Discovery. Only
    #: ``CommunityGetResponse`` carries it, so this is meaningful on a
    #: :meth:`~rootpy.services.communities.CommunityService.get` result
    #: and stays False on one built from a packet or from GetExtended.
    is_discoverable: bool = False
    #: The first listing requirement the community does not meet, as
    #: :class:`~rootpy.enums.CommunityDiscoverableRequirement`. Same
    #: restriction as :attr:`is_discoverable`: ``Get`` only.
    #:
    #: ``UNSPECIFIED`` (0) means *nothing is recorded*, which is not the same
    #: as "it qualifies". A community that has never had
    #: :meth:`~rootpy.services.communities.CommunityService.set_discoverable`
    #: refused reads 0, and so does a community that is already listed. The
    #: value only becomes informative after the server turns an attempt down.
    is_discoverable_status: int = 0
    _admin: Optional[object] = field(default=None, repr=False, compare=False)
    _service: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_service(self):
        if self._service is None:
            raise RuntimeError(
                "This Community is not attached to a RootClient"
            )
        return self._service

    def _require_admin(self):
        if self._admin is None:
            raise RuntimeError("This Community is not attached to a RootClient")
        return self._admin

    async def leave(self) -> None:
        """Leave this community as the current authenticated user."""
        await self._require_service().leave(self.id)

    async def edit(self, **kwargs):
        return await self._require_admin().edit_community(self.id, **kwargs)

    async def delete(self) -> None:
        await self._require_admin().delete_community(self.id)

    async def create_channel_group(self, name: str, **kwargs):
        return await self._require_admin().create_channel_group(
            self.id, name, **kwargs
        )

    async def create_channel(
        self,
        channel_group_id: str,
        name: str,
        **kwargs,
    ):
        return await self._require_admin().create_channel(
            self.id,
            channel_group_id,
            name,
            **kwargs,
        )

    # Both take their type from the ChannelType enum rather than a literal.
    # ChannelType is {Unspecified: 0, Text: 1, ThreadedText: 2, Voice: 4,
    # App: 8}: Unspecified is rejected by Root, and a literal 1 means Text,
    # so a voice helper that sends 1 silently makes a text channel -- the
    # worse of the two failures, since it succeeds.
    async def create_text_channel(
        self,
        channel_group_id: str,
        name: str,
        **kwargs,
    ):
        from .enums import ChannelType

        kwargs["channel_type"] = int(ChannelType.TEXT)
        return await self.create_channel(channel_group_id, name, **kwargs)

    async def create_voice_channel(
        self,
        channel_group_id: str,
        name: str,
        **kwargs,
    ):
        from .enums import ChannelType

        kwargs["channel_type"] = int(ChannelType.VOICE)
        return await self.create_channel(channel_group_id, name, **kwargs)

    async def create_role(self, name: str, **kwargs):
        return await self._require_admin().create_role(
            self.id, name, **kwargs
        )

    async def clone(self, **kwargs):
        return await self._require_admin().clone_community(self.id, **kwargs)


@dataclass(frozen=True)
class Channel:
    id: str
    community_id: str
    channel_group_id: Optional[str]
    name: str
    description: Optional[str] = None
    icon_asset_uri: Optional[str] = None
    before_channel_id: Optional[str] = None
    use_channel_group_permission: bool = False
    channel_type: int = 0
    position: float = 0.0
    community_app_id: Optional[str] = None
    last_activity_at: Optional[object] = None
    user_last_viewed_at: Optional[object] = None
    permissions: ChannelPermissions = ChannelPermissions()
    role_or_member_ids: tuple[str, ...] = ()
    packet_type: int = 0
    raw: bytes = b""
    _admin: Optional[object] = field(default=None, repr=False, compare=False)
    _client: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_admin(self):
        if self._admin is None:
            raise RuntimeError("This Channel is not attached to a RootClient")
        return self._admin

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("This Channel is not attached to a RootClient")
        return self._client

    @property
    def is_text(self) -> bool:
        """True for channels that hold messages (TEXT or THREADED_TEXT)."""
        return int(self.channel_type or 0) in (1, 2)

    @property
    def mention(self) -> str:
        """The string form that renders as a channel link.

        Root's format is a markdown link, not a Discord-style ``<#id>``::

            [#general](root://channel/002cb818-e400-8004-aaaa-aaaaaaaaaaaa)

        Discord's ``<#{id}>`` is not the shape Root uses;
        ``rootpy.commands.parse_channel_mention`` refuses it, and Root renders
        it as literal text rather than a link.

        Falls back to the id when the channel's name is unknown, since the
        label is part of the syntax.
        """
        return build_channel_mention(self.id, self.name)

    async def reply_to(self, messages, content: str, **kwargs):
        """Reply to several messages in this channel (up to 5).

            await channel.reply_to([msg_a, msg_b], "answering both")
        """
        from .services.messages import normalise_reply_targets

        targets = normalise_reply_targets(messages)
        if not targets:
            raise ValueError("no messages to reply to")
        return await self._require_client().messages.send(
            self.id,
            content,
            community_id=self.community_id,
            parent_message_ids=targets,
            **kwargs,
        )

    async def send(self, content: str, **kwargs):
        """Send a message to this channel.

            await channel.send("hello")
        """
        return await self._require_client().messages.send(
            self.id, content, community_id=self.community_id, **kwargs
        )

    async def history(self, *, limit: Optional[int] = None, before=None,
                      direction: str = "both"):
        """Fetch this channel's messages (see ``client.messages.list``)."""
        return await self._require_client().messages.list(
            self.id,
            community_id=self.community_id,
            direction=direction,
            after=before,
            limit=limit,
        )

    def history_iter(self, *, limit: Optional[int] = 200, before=None,
                     page_size: int = 50):
        """Iterate this channel's history, newest first.

            async for message in channel.history_iter(limit=500):
                ...
        """
        return self._require_client().messages.history(
            self.id,
            community_id=self.community_id,
            limit=limit,
            before=before,
            page_size=page_size,
        )

    async def mark_read(self) -> None:
        """Mark this channel as read up to now."""
        await self._require_client().messages.set_view_time(
            self.id, community_id=self.community_id
        )

    async def edit(self, **kwargs):
        return await self._require_admin().edit_channel(
            self.community_id, self.id, **kwargs
        )

    async def delete(self) -> None:
        await self._require_admin().delete_channel(
            self.community_id, self.id
        )

    async def move(self, **kwargs) -> None:
        await self._require_admin().move_channel(
            self.community_id, self.id, **kwargs
        )



@dataclass(frozen=True)
class ChannelGroup:
    id: str
    community_id: str
    name: str
    position: float = 0.0
    permissions: ChannelPermissions = ChannelPermissions()
    role_or_member_ids: tuple[str, ...] = ()
    channels: tuple[Channel, ...] = ()
    raw: bytes = b""
    _admin: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_admin(self):
        if self._admin is None:
            raise RuntimeError("This ChannelGroup is not attached to a RootClient")
        return self._admin

    async def edit(self, **kwargs):
        return await self._require_admin().edit_channel_group(
            self.community_id, self.id, **kwargs
        )

    async def delete(self) -> None:
        await self._require_admin().delete_channel_group(
            self.community_id, self.id
        )

    async def create_channel(self, name: str, **kwargs):
        return await self._require_admin().create_channel(
            self.community_id, self.id, name, **kwargs
        )


@dataclass(frozen=True)
class DetailedMember:
    """A community member together with their public profile.

    Profile fields are readable straight off this object, so you don't have to
    reach through ``.profile`` -- and they're ``None`` rather than an error if
    the profile couldn't be fetched.
    """

    member: "CommunityMember"
    profile: Optional["UserProfile"] = None

    @property
    def user_id(self) -> str:
        return self.member.user_id

    @property
    def role_ids(self) -> tuple:
        return self.member.role_ids

    @property
    def community_id(self):
        return getattr(self.member, "community_id", None)

    @property
    def joined_at(self):
        return getattr(self.member, "joined_at", None)

    @property
    def username(self):
        return getattr(self.profile, "username", None)

    @property
    def profile_picture_uri(self):
        return getattr(self.profile, "profile_picture_uri", None)

    @property
    def avatar_url(self):
        return self.profile_picture_uri

    @property
    def banner_uri(self):
        return getattr(self.profile, "banner_uri", None)

    @property
    def description(self):
        return getattr(self.profile, "description", None)

    @property
    def about_me(self):
        return self.description

    @property
    def custom_status(self):
        return getattr(self.profile, "custom_status", None)

    @property
    def online_status(self) -> int:
        return getattr(self.profile, "online_status", 0)

    @property
    def is_deleted(self) -> bool:
        return getattr(self.profile, "is_deleted", False)

    # member actions pass straight through
    async def add_role(self, role_id):
        return await self.member.add_role(role_id)

    async def remove_role(self, role_id):
        return await self.member.remove_role(role_id)

    async def kick(self):
        return await self.member.kick()

    async def ban(self, **kwargs):
        return await self.member.ban(**kwargs)

    def __str__(self) -> str:
        return self.username or self.user_id


@dataclass(frozen=True)
class UserProfile:
    """A user's public profile, as returned by GetExtendedUsersById.

    This is the information Root shows on someone's profile card. Member
    records carry only ids, so this is what you fetch when you want to put a
    name and picture to one.
    """

    user_id: str
    username: Optional[str] = None
    profile_picture_uri: Optional[str] = None
    banner_uri: Optional[str] = None
    description: Optional[str] = None          # the "about me"
    custom_status: Optional[str] = None
    online_status: int = 0
    is_deleted: bool = False
    raw: bytes = b""

    @property
    def about_me(self) -> Optional[str]:
        """Alias for :attr:`description`."""
        return self.description

    @property
    def avatar_url(self) -> Optional[str]:
        """Alias for :attr:`profile_picture_uri`."""
        return self.profile_picture_uri

    def __str__(self) -> str:
        return self.username or self.user_id


@dataclass(frozen=True)
class CommunityMember:
    user_id: str
    role_ids: tuple[str, ...] = ()
    raw: bytes = b""
    community_id: Optional[str] = None
    #: When this account joined the community, or None if the server did not
    #: send it. Field 12 of CommunityMemberShort.
    #: Declared last so the positional constructor keeps working.
    joined_at: Optional[object] = None
    _client: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_client(self):
        if self._client is None: raise RuntimeError("This CommunityMember is not attached to a RootClient")
        return self._client
    async def kick(self): return await self._require_client().members.kick(self.community_id, self.user_id)
    async def ban(self, **kwargs): return await self._require_client().members.ban(self.community_id, self.user_id, **kwargs)
    async def add_role(self, role_id: str): return await self._require_client().community.add_role(self.community_id, self.user_id, role_id)
    async def remove_role(self, role_id: str): return await self._require_client().community.remove_role(self.community_id, self.user_id, role_id)
    @property
    def user(self): return self._require_client().get_user(self.user_id)


@dataclass(frozen=True)
class CommunityRole:
    id: str
    permissions: ChannelPermissions = ChannelPermissions()
    name: str = ""
    position: float = 0.0
    raw: bytes = b""
    community_permissions: CommunityPermission = CommunityPermission()
    color_hex: str = ""
    is_mentionable: bool = False
    is_self_assignable: bool = False
    channel_permission_raw: bytes = b""
    community_permission_raw: bytes = b""
    community_id: Optional[str] = None
    _client: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_client(self):
        if self._client is None: raise RuntimeError("This CommunityRole is not attached to a RootClient")
        return self._client
    async def edit(self, **kwargs):
        kwargs.setdefault("name", self.name)
        return await self._require_client().community.edit_role(self.community_id, self.id, **kwargs)
    async def delete(self): return await self._require_client().community.delete_role(self.community_id, self.id)
    async def move(self, *, before_role_id=None): return await self._require_client().community.move_role(self.community_id, self.id, before_role_id=before_role_id)
    async def add_to(self, user_id: str): return await self._require_client().community.add_role(self.community_id, user_id, self.id)
    async def remove_from(self, user_id: str): return await self._require_client().community.remove_role(self.community_id, user_id, self.id)


@dataclass(frozen=True)
class CommunityExtended:
    community: Community
    channel_groups: tuple[ChannelGroup, ...]
    members: tuple[CommunityMember, ...] = ()
    roles: tuple[CommunityRole, ...] = ()
    raw: bytes = b""

    @property
    def channels(self) -> tuple:
        """Every channel across all groups, flattened."""
        return tuple(
            channel for group in self.channel_groups
            for channel in group.channels
        )

    @property
    def text_channels(self) -> tuple:
        """Only the channels that hold messages."""
        return tuple(c for c in self.channels if getattr(c, "is_text", False))

    @property
    def attached_user_ids(self) -> tuple:
        """The users the server currently reports as attached.

        This is the only way to see whether an attach worked. The attach
        request returns nothing, and a lost attach is silent: the account
        stays logged in and leaves the member list. Until now a caller had to
        decode field 20 of the raw response to find this out.

        The client shows these users as present in the community. See
        :meth:`rootpy.object_api.CommunityManager.held` to keep a place here.
        """
        from .protocol import decode_root_guid_message, iter_fields

        found = []
        for number, wire_type, value in iter_fields(self.raw):
            if number != 20 or wire_type != 2:
                continue
            try:
                decoded = decode_root_guid_message(value)
            except Exception:                                 # noqa: BLE001
                continue
            if decoded:
                found.append(decoded)
        return tuple(found)

    def is_attached(self, user_id: str) -> bool:
        """Is this user attached to the community now?"""
        from .identifiers import normalize_root_guid

        target = normalize_root_guid(user_id)
        return any(
            normalize_root_guid(found) == target
            for found in self.attached_user_ids
        )

    def member(self, user_id: str):
        """Find one member by user id, or None."""
        for entry in self.members:
            if entry.user_id == user_id:
                return entry
        return None

    def role(self, role_id: str):
        """Find one role by id, or None."""
        for entry in self.roles:
            if entry.id == role_id:
                return entry
        return None


# --------------------------------------------------------------------------
# Community Discovery
# --------------------------------------------------------------------------
@dataclass
class DiscoveredCommunity:
    """One row from ``client.discovery.search``.

    This is deliberately *not* a :class:`Community`. A directory row is what
    the search index holds -- counts, a category, an ``indexed_at`` -- and it
    carries neither ``owner_user_id`` nor ``default_channel_id``, so giving it
    the same type would hand callers a Community with holes in it. Join it,
    then fetch the real thing.

    ``description``, ``picture_asset_uri`` and ``banner_asset_uri`` are
    ``StringValue`` wrappers on the wire, so ``None`` means the server omitted
    the field and ``""`` means it sent an empty one.
    """

    id: Optional[str]
    name: str
    description: Optional[str] = None
    picture_asset_uri: Optional[str] = None
    banner_asset_uri: Optional[str] = None
    category: int = 0
    member_count: int = 0
    app_count: int = 0
    core_count: int = 0
    is_discoverable: bool = False
    is_discoverable_status: int = 0
    indexed_at: Optional[object] = None
    raw: bytes = field(default=b"", repr=False, compare=False)
