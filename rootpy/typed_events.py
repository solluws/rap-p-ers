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
    """A member joined, left, or was banned from a community."""

    user_id: str = ""
    username: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    role_ids: tuple[str, ...] = ()

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
    channel_type: int = 0

    @property
    def is_text(self) -> bool:
        return int(self.channel_type or 0) in (1, 2)

    def __str__(self) -> str:
        where = self.community_name or self.community_id
        return f"#{self.name or self.channel_id} in {where}"


@dataclass(frozen=True)
class RoleEvent(RootEvent):
    """A community role was created, edited, or deleted."""

    role_id: str = ""
    name: Optional[str] = None
    community_id: str = ""
    community_name: Optional[str] = None
    color_hex: Optional[str] = None
    mentionable: bool = False

    def __str__(self) -> str:
        return self.name or self.role_id


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
    ROUTES = {
        "COMMUNITY_MEMBER_ATTACH": "member_join",
        "COMMUNITY_JOINED": "member_join",
        "COMMUNITY_MEMBER_DETACH": "member_leave",
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
        "USER_BLOCK_CREATED": "block_add",
        "USER_BLOCK_DELETED": "block_remove",
    }

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
        builder = getattr(self, f"_build_{name}", None)
        if builder is None:
            # Group families that share a shape.
            if name in ("member_join", "member_leave", "member_ban", "member_unban"):
                builder = self._build_member
            elif name in ("role_add", "role_remove"):
                builder = self._build_member_role
            elif name in ("friend_request", "friend_remove"):
                builder = self._build_friend
            elif name in ("channel_create", "channel_edit", "channel_delete"):
                builder = self._build_channel
            elif name in ("role_create", "role_delete"):
                builder = self._build_role
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
        return ChannelEvent(
            channel_id=fields.get("id") or "",
            name=fields.get("name"),
            community_id=community_id,
            community_name=self._community_name(community_id),
            channel_group_id=group_id,
            category=self._channel_group_name(community_id, group_id),
            channel_type=int(fields.get("channel_type") or 0),
            raw=packet,
        )

    def _build_role(self, fields, packet) -> RoleEvent:
        community_id = fields.get("community_id") or ""
        return RoleEvent(
            role_id=fields.get("id") or "",
            name=fields.get("name"),
            community_id=community_id,
            community_name=self._community_name(community_id),
            color_hex=fields.get("color_hex"),
            mentionable=bool(fields.get("is_mentionable")),
            raw=packet,
        )

    def _build_block(self, fields, packet) -> BlockEvent:
        user_id = fields.get("block_user_id") or ""
        return BlockEvent(
            user_id=user_id,
            username=self._username(user_id),
            raw=packet,
        )
