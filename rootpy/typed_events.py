"""Typed, friendly events built from raw gateway packets.

The gateway names every packet (``on_packet_channel_created`` and friends),
but those carry a field dict of raw ids. The events here are the pleasant
layer on top: real objects with resolved names where the client already knows
them, plus the timestamp the event was received.

    @client.event
    async def on_friend_request(event):
        print(f"{event.username or event.user_id} sent a friend request")

    @client.event
    async def on_channel_create(event):
        print(f"#{event.name} created in {event.community_name}")

Every event carries ``.received_at`` (a timezone-aware ``datetime``) and
``.raw`` (the originating :class:`SocketPacket`) so nothing is hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = [
    "RootEvent",
    "MemberEvent",
    "MemberRoleEvent",
    "FriendEvent",
    "ChannelEvent",
    "RoleEvent",
    "ReactionEvent",
    "BlockEvent",
    "CommunityEvent",
    "TypedEventBuilder",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RootEvent:
    """Shared base: when it happened and the packet it came from."""

    received_at: datetime = field(default_factory=_now)
    raw: Optional[Any] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MemberEvent(RootEvent):
    """A member joined, left, was banned from, or came online in a community.

    ``presence`` separates the two families. It is False for the membership
    events (``member_join``/``member_leave`` from COMMUNITY_JOINED and
    COMMUNITY_LEAVE) and True for the online/offline events
    (``member_online``/``member_offline`` from COMMUNITY_MEMBER_ATTACH and
    COMMUNITY_MEMBER_DETACH), which say nothing about membership -- the app
    feeds attach straight into SetAttached(userId, true, OnlineStatus)
    (MemberService.cs:303-315).
    """

    user_id: str = ""
    username: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    role_ids: tuple[str, ...] = ()
    presence: bool = False

    def __str__(self) -> str:
        who = self.username or self.user_id
        where = self.community_name or self.community_id
        return f"{who} @ {where}"


@dataclass(frozen=True)
class MemberRoleEvent(RootEvent):
    """A role was granted to or removed from one or more members."""

    community_id: str = ""
    community_name: Optional[str] = None
    role_id: str = ""
    role_name: Optional[str] = None
    user_ids: tuple[str, ...] = ()

    @property
    def user_id(self) -> Optional[str]:
        """First affected user -- the common case is exactly one."""
        return self.user_ids[0] if self.user_ids else None

    def __str__(self) -> str:
        role = self.role_name or self.role_id
        return f"{role} -> {', '.join(self.user_ids) or '?'}"


@dataclass(frozen=True)
class FriendEvent(RootEvent):
    """A friendship was created (request accepted/received) or removed."""

    user_id: str = ""
    username: Optional[str] = None
    friendship_id: Optional[str] = None
    group_id: Optional[str] = None

    def __str__(self) -> str:
        return self.username or self.user_id


@dataclass(frozen=True)
class ChannelEvent(RootEvent):
    """A channel was created, edited, or deleted."""

    channel_id: str = ""
    name: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    channel_group_id: Optional[str] = None
    category: Optional[str] = None       # the channel group's name
    channel_type: Optional[int] = None

    @property
    def is_text(self) -> Optional[bool]:
        """True/False, or None when nothing here knows the channel's type.

        ChannelType rides on ChannelCreatedPacket only (field 12). The edited
        packet writes fields 3-6/9-11 and the deleted packet only 3/4/5, so a
        flat ``channel_type = 0`` made ``if event.is_text:`` drop 100% of edits
        and deletes. The builder fills the type in from the client's channel
        cache when the packet omits it; None means genuinely unknown (the
        channel was never cached), which is not the same answer as False.
        """
        if self.channel_type is None:
            return None
        return int(self.channel_type) in (1, 2)

    def __str__(self) -> str:
        where = self.community_name or self.community_id
        return f"#{self.name or self.channel_id} in {where}"


@dataclass(frozen=True)
class RoleEvent(RootEvent):
    """A community role was created, edited, deleted, or moved.

    Which one is the event name, not a field: create/edit share one oneof slot
    and are told apart by the packet's own PacketType (see ``SUBROUTES``).
    """

    role_id: str = ""
    name: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    color_hex: Optional[str] = None
    mentionable: bool = False
    # The role this one now sits after, from field 6 of CommunityRoleMovedPacket
    # (field 9 on CommunityRolePacket). It is the only payload a move carries
    # beyond the ids, so on_role_move is useless without it. Empty string means
    # the role moved to the top of the list, which is what the app sends.
    before_role_id: str = ""

    def __str__(self) -> str:
        return self.name or self.role_id


@dataclass(frozen=True)
class ReactionEvent(RootEvent):
    """Somebody added or removed a reaction on a message.

    One oneof slot (173, ``MessageReactionPacket``) carries four packet types:
    channel reactions 5801/5802 and direct-message reactions 204/205. The app
    splits them the same way, in ChannelService.cs:657-666 and
    DirectMessageService.cs:286-294. Add and remove are the event name;
    ``is_direct`` tells you which container ``container_id`` names, because a
    DM reaction carries no community and would otherwise look like a channel
    reaction in a community you cannot find.

    ``shortcode`` is the emoji as Root stores it. Pass it through
    :func:`rootpy.normalize_reaction` if you want to compare it to a literal.
    """

    message_id: str = ""
    user_id: str = ""
    username: Optional[str] = None
    shortcode: str = ""
    container_id: str = ""
    channel_name: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    is_direct: bool = False

    def __str__(self) -> str:
        who = self.username or self.user_id
        return f"{who} {self.shortcode} on {self.message_id}"


@dataclass(frozen=True)
class BlockEvent(RootEvent):
    """A user was blocked or unblocked."""

    user_id: str = ""
    username: Optional[str] = None

    def __str__(self) -> str:
        return self.username or self.user_id


@dataclass(frozen=True)
class CommunityEvent(RootEvent):
    """You joined or left a community."""

    community_id: str = ""
    community_name: Optional[str] = None
    user_id: Optional[str] = None

    def __str__(self) -> str:
        return self.community_name or self.community_id


class TypedEventBuilder:
    """Turns named gateway packets into the typed events above.

    Names are resolved from whatever the client already has cached (community
    names, channel groups, roles, known users). Nothing is fetched -- an
    unresolved name is simply ``None``, so a handler can fall back to the id.
    """

    # packet type -> (event name, builder method)
    #
    # ATTACH/DETACH are presence, not membership: the app routes an attach into
    # SetAttached(userId, true, packet.OnlineStatus) (MemberService.cs:303-315)
    # and CommunityMemberAttachPacket carries a UserOnlineStatus precisely
    # because it is an online/offline signal. COMMUNITY_JOINED and
    # COMMUNITY_LEAVE are the real membership packets, so they are the only
    # things that may fire member_join/member_leave.
    ROUTES = {
        "COMMUNITY_MEMBER_ATTACH": "member_online",
        "COMMUNITY_JOINED": "member_join",
        "COMMUNITY_MEMBER_DETACH": "member_offline",
        "COMMUNITY_LEAVE": "member_leave",
        "COMMUNITY_MEMBER_BAN_CREATED": "member_ban",
        "COMMUNITY_MEMBER_BAN_DELETED": "member_unban",
        "COMMUNITY_MEMBER_ROLE_CREATED": "role_add",
        "COMMUNITY_MEMBER_ROLE_DELETED": "role_remove",
        "FRIENDSHIP_CREATED": "friend_request",
        "FRIENDSHIP_DELETED": "friend_remove",
        "CHANNEL_CREATED": "channel_create",
        "CHANNEL_EDITED": "channel_edit",
        "CHANNEL_DELETED": "channel_delete",
        "COMMUNITY_ROLE": "role_create",
        "COMMUNITY_ROLE_DELETED": "role_delete",
        "COMMUNITY_ROLE_MOVED": "role_move",
        "MESSAGE_REACTION": "reaction_add",
        "USER_BLOCK_CREATED": "block_add",
        "USER_BLOCK_DELETED": "block_remove",
    }

    # A oneof slot is not always one event. Slot 90 (COMMUNITY_ROLE) carries
    # CommunityRolePacket for both create (5401) and edit (5402), and the app
    # discriminates on the packet's own PacketType before deciding what to do
    # with it (RoleService.cs:74-92). Routing on the slot alone meant a rename,
    # a recolour or an is_mentionable toggle all arrived as on_role_create with
    # a RoleEvent indistinguishable from a genuine creation.
    #
    # 5403/5404 have their own slots (91/92) and so never reach here, but they
    # are listed because they are legal values of the same field.
    #
    # Slot 173 (MESSAGE_REACTION) is the same story and worse: one
    # MessageReactionPacket serves add and remove, for channels *and* for DMs.
    # ChannelService.cs:657-666 splits 5801/5802 and DirectMessageService.cs
    # :286-294 splits 204/205, all four casting the same packet class. Without
    # the subroute a removed reaction is indistinguishable from a new one.
    SUBROUTES = {
        "COMMUNITY_ROLE": {
            5401: "role_create",
            5402: "role_edit",
            5403: "role_delete",
            5404: "role_move",
        },
        "MESSAGE_REACTION": {
            204: "reaction_add",       # direct message
            205: "reaction_remove",    # direct message
            5801: "reaction_add",      # channel
            5802: "reaction_remove",   # channel
        },
    }

    # The direct-message half of the subroute above. Kept beside it so the two
    # cannot drift apart: these are the packet types whose container_id names a
    # DM rather than a channel.
    _DM_REACTIONS = (204, 205)

    def __init__(self, client) -> None:
        self.client = client

    # --- name resolution (best effort, cache-only) ----------------------- #
    def _community_name(self, community_id) -> Optional[str]:
        if not community_id:
            return None
        cached = getattr(self.client, "communities", {}).get(community_id)
        return getattr(cached, "name", None)

    def _channel_group_name(self, community_id, group_id) -> Optional[str]:
        if not group_id:
            return None
        cached = getattr(self.client, "communities", {}).get(community_id)
        for group in getattr(cached, "channel_groups", ()) or ():
            if getattr(group, "id", None) == group_id:
                return getattr(group, "name", None)
        return None

    def _role_name(self, community_id, role_id) -> Optional[str]:
        if not role_id:
            return None
        cached = getattr(self.client, "communities", {}).get(community_id)
        for role in getattr(cached, "roles", ()) or ():
            if getattr(role, "id", None) == role_id:
                return getattr(role, "name", None)
        return None

    def _cached_channel(self, channel_id):
        if not channel_id:
            return None
        return getattr(self.client, "channels", {}).get(channel_id)

    def _username(self, user_id) -> Optional[str]:
        """Username from cache if we've seen this user; never fetches."""
        if not user_id:
            return None
        cache = getattr(self.client, "cache", None)
        if cache is None:
            return None
        return cache.username(user_id)

    # --- building --------------------------------------------------------- #
    def build(self, packet) -> Optional[tuple[str, RootEvent]]:
        """Return ``(event_name, event)`` for a packet, or None if unmapped."""
        name = self.ROUTES.get(packet.type.name)
        if name is None:
            return None
        fields = packet.fields or {}
        subroute = self.SUBROUTES.get(packet.type.name)
        if subroute:
            # packet_type is field 1 on every packet message. A schema that
            # does not decode it yields None -- keep the slot's default route
            # rather than guessing.
            packet_type = fields.get("packet_type")
            if packet_type is not None:
                try:
                    name = subroute.get(int(packet_type), name)
                except (TypeError, ValueError):
                    pass
        builder = getattr(self, f"_build_{name}", None)
        if builder is None:
            # Group families that share a shape.
            if name in ("member_join", "member_leave", "member_ban",
                        "member_unban", "member_online", "member_offline"):
                builder = self._build_member
            elif name in ("role_add", "role_remove"):
                builder = self._build_member_role
            elif name in ("friend_request", "friend_remove"):
                builder = self._build_friend
            elif name in ("channel_create", "channel_edit", "channel_delete"):
                builder = self._build_channel
            elif name in ("role_create", "role_edit", "role_delete", "role_move"):
                builder = self._build_role
            elif name in ("reaction_add", "reaction_remove"):
                builder = self._build_reaction
            elif name in ("block_add", "block_remove"):
                builder = self._build_block
            else:
                return None
        return name, builder(fields, packet)

    def _build_member(self, fields, packet) -> MemberEvent:
        community_id = fields.get("community_id") or ""
        user_id = fields.get("user_id") or ""
        roles = fields.get("community_role_ids") or ()
        if isinstance(roles, str):
            roles = (roles,)
        return MemberEvent(
            user_id=user_id,
            username=self._username(user_id),
            community_id=community_id,
            community_name=self._community_name(community_id),
            role_ids=tuple(r for r in roles if r),
            presence=packet.type.name in (
                "COMMUNITY_MEMBER_ATTACH", "COMMUNITY_MEMBER_DETACH",
            ),
            raw=packet,
        )

    def _build_member_role(self, fields, packet) -> MemberRoleEvent:
        community_id = fields.get("community_id") or ""
        role_id = fields.get("community_role_id") or ""
        user_ids = fields.get("user_ids") or ()
        if isinstance(user_ids, str):
            user_ids = (user_ids,)
        return MemberRoleEvent(
            community_id=community_id,
            community_name=self._community_name(community_id),
            role_id=role_id,
            role_name=self._role_name(community_id, role_id),
            user_ids=tuple(u for u in user_ids if u),
            raw=packet,
        )

    def _build_friend(self, fields, packet) -> FriendEvent:
        user_id = fields.get("friend_user_id") or ""
        return FriendEvent(
            user_id=user_id,
            username=self._username(user_id),
            friendship_id=fields.get("id"),
            group_id=fields.get("friendship_group_id"),
            raw=packet,
        )

    def _build_channel(self, fields, packet) -> ChannelEvent:
        community_id = fields.get("community_id") or ""
        group_id = fields.get("channel_group_id")
        channel_id = fields.get("id") or ""
        name = fields.get("name")
        channel_type = fields.get("channel_type")
        # Only ChannelCreatedPacket carries ChannelType (field 12), and the
        # deleted packet carries no name either, so fall back to whatever the
        # client already cached for this channel. The gateway dispatches
        # "packet" before "channel_deleted" evicts it, so the cache is still
        # warm even on a delete. Anything still missing stays None (unknown).
        if channel_type is None or name is None:
            cached = self._cached_channel(channel_id)
            if cached is not None:
                if channel_type is None:
                    channel_type = getattr(cached, "channel_type", None)
                if name is None:
                    name = getattr(cached, "name", None)
        return ChannelEvent(
            channel_id=channel_id,
            name=name,
            community_id=community_id,
            community_name=self._community_name(community_id),
            channel_group_id=group_id,
            category=self._channel_group_name(community_id, group_id),
            channel_type=None if channel_type is None else int(channel_type),
            raw=packet,
        )

    def _build_role(self, fields, packet) -> RoleEvent:
        community_id = fields.get("community_id") or ""
        # Field 4 is the role id on all three slots, but the schemas name it
        # differently: CommunityRolePacket and CommunityRoleMovedPacket call it
        # ``id``, CommunityRoleDeletedPacket calls it ``community_role_id``.
        # Reading only ``id`` gave every on_role_delete an empty role_id.
        role_id = fields.get("id") or fields.get("community_role_id") or ""
        return RoleEvent(
            role_id=role_id,
            name=fields.get("name") or self._role_name(community_id, role_id),
            community_id=community_id,
            community_name=self._community_name(community_id),
            color_hex=fields.get("color_hex"),
            mentionable=bool(fields.get("is_mentionable")),
            before_role_id=fields.get("before_community_role_id") or "",
            raw=packet,
        )

    def _build_reaction(self, fields, packet) -> ReactionEvent:
        community_id = fields.get("community_id") or ""
        container_id = fields.get("container_id") or ""
        user_id = fields.get("user_id") or ""
        packet_type = fields.get("packet_type")
        try:
            is_direct = int(packet_type) in self._DM_REACTIONS
        except (TypeError, ValueError):
            # No decoded packet_type. A DM reaction carries no community, so
            # the absent community id is the only other signal available.
            is_direct = not community_id
        channel = None if is_direct else self._cached_channel(container_id)
        return ReactionEvent(
            message_id=fields.get("message_id") or "",
            user_id=user_id,
            username=self._username(user_id),
            shortcode=fields.get("shortcode") or "",
            container_id=container_id,
            channel_name=getattr(channel, "name", None),
            community_id=community_id,
            community_name=self._community_name(community_id),
            is_direct=is_direct,
            raw=packet,
        )

    def _build_block(self, fields, packet) -> BlockEvent:
        user_id = fields.get("block_user_id") or ""
        return BlockEvent(
            user_id=user_id,
            username=self._username(user_id),
            raw=packet,
        )
