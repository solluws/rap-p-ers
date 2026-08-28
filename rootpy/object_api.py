from __future__ import annotations

from .enums import ChannelType

from typing import Iterable, Optional, Tuple

from .identifiers import normalize_root_guid
from .models import Channel, ChannelGroup, Community, CommunityExtended, CommunityMember, CommunityRole
from .permissions import AccessRule, ChannelOverlay, ChannelPermission, ChannelPermissions, CommunityPermission


class PermissionManager:
    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def channel(**kwargs) -> ChannelPermission:
        return ChannelPermission(**kwargs)

    @staticmethod
    def community(**kwargs) -> CommunityPermission:
        return CommunityPermission(**kwargs)

    @staticmethod
    def overlay(**kwargs) -> ChannelOverlay:
        return ChannelOverlay(**kwargs)

    @staticmethod
    def rule(target_id: str, **permissions) -> AccessRule:
        return AccessRule(
            target_id=normalize_root_guid(target_id),
            overlay=ChannelOverlay(**permissions),
        )

    async def list_rules(self, community_id: str, channel_or_group_id: str):
        return await self.client.community_admin.list_access_rules(
            community_id,
            channel_or_group_id,
        )

    async def create_rule(
        self,
        community_id: str,
        channel_or_group_id: str,
        target_id: str,
        **permissions,
    ) -> None:
        await self.client.community_admin.set_access_rule(
            community_id,
            channel_or_group_id,
            target_id,
            ChannelOverlay(**permissions),
            exists=False,
        )

    async def edit_rule(
        self,
        community_id: str,
        channel_or_group_id: str,
        target_id: str,
        **permissions,
    ) -> None:
        await self.client.community_admin.set_access_rule(
            community_id,
            channel_or_group_id,
            target_id,
            ChannelOverlay(**permissions),
            exists=True,
        )

    async def delete_rule(
        self,
        community_id: str,
        channel_or_group_id: str,
        target_id: str,
    ) -> None:
        await self.client.community_admin.delete_access_rule(
            community_id,
            channel_or_group_id,
            target_id,
        )


class CommunityManager:
    # Taken from rootpy.enums.ChannelType rather than redefined. These were
    # previously TEXT = 0 / VOICE = 1, which are simply wrong: 0 is
    # UNSPECIFIED and 1 is TEXT. create_text_channel() therefore sent
    # UNSPECIFIED and Root rejected it with a PredicateValidator on
    # ChannelType, while create_voice_channel() sent TEXT and silently
    # created a text channel -- the worse of the two failures, since it
    # succeeded. Confirmed against the descriptor:
    # ChannelType {Unspecified: 0, Text: 1, ThreadedText: 2, Voice: 4, App: 8}
    TEXT = int(ChannelType.TEXT)
    THREADED_TEXT = int(ChannelType.THREADED_TEXT)
    VOICE = int(ChannelType.VOICE)

    def __init__(self, client) -> None:
        self.client = client
        self.permissions = PermissionManager(client)

    def cached(self) -> Tuple[Community, ...]:
        return tuple(self.client.communities.values())

    async def list(self, *, refresh: bool = True) -> Tuple[Community, ...]:
        """The communities this account is in.

        Honours the client's ``expand_communities`` setting rather than
        forcing expansion. It used to call ``refresh_communities()`` bare,
        taking that method's ``expand=True`` default -- so ``list_communities()``
        on a *lazy* client (the documented default) silently did the eager
        thing: one ListMine plus a full ``CommunityGetExtended`` per community,
        each returning that community's entire member, role and channel dump.

        Measured on an account in 9 communities: **10 requests where 1 would
        do**, and one of those communities has 31,555 members. The return type
        is ``Community``, not ``CommunityExtended``, so none of that data was
        needed to answer the call -- it only warmed a cache the caller may
        never read.

        The login path at ``client.py:1311`` already passed
        ``expand=self.expand_communities``; this now agrees with it, so the
        cost model the README and LLMS.md document ("eager: 3 + one per
        community", opt-in) is true of both. Detail is still fetched and cached
        on first access by ``community_detail`` / ``get_community``.
        """
        if refresh:
            await self.client.refresh_communities(
                expand=self.client.expand_communities
            )
        return self.cached()

    async def servers(self, *, refresh: bool = True) -> Tuple[Community, ...]:
        return await self.list(refresh=refresh)

    def get(self, community_id: str) -> Optional[Community]:
        return self.client.get_community(community_id)

    async def fetch(self, community_id: str) -> CommunityExtended:
        return await self.client.fetch_community(community_id)

    async def create(self, name: str, **kwargs) -> Community:
        return await self.client.create_community(name, **kwargs)

    async def edit(self, community_id: str, **kwargs) -> Community:
        community = await self.client.community_admin.edit_community(
            community_id,
            **kwargs,
        )
        self.client.communities[community.id] = community
        return community

    async def delete(self, community_id: str) -> None:
        community_id = normalize_root_guid(community_id)
        await self.client.community_admin.delete_community(community_id)
        self.client.communities.pop(community_id, None)
        self.client._community_extended.pop(community_id, None)

    async def clone(self, community_id: str, **kwargs) -> Community:
        return await self.client.clone_community(community_id, **kwargs)

    async def attach(self, community_id: str) -> None:
        """Start receiving this community's live packets.

        Being a member is not enough: the hub sends nothing for a community
        until the connection attaches to it. See
        :meth:`rootpy.services.communities.CommunityService.attach` for what
        that costs and who can see it.
        """
        await self.client.community_service.attach(community_id)

    async def detach(self, community_id: str) -> None:
        """Stop receiving this community's live packets."""
        await self.client.community_service.detach(community_id)

    async def detach_many(self, community_ids) -> None:
        """Detach from several communities in a single request."""
        await self.client.community_service.detach_many(community_ids)

    async def get_channel_groups(
        self,
        community_id: str,
        *,
        refresh: bool = False,
    ) -> Tuple[ChannelGroup, ...]:
        community_id = normalize_root_guid(community_id)
        if refresh or community_id not in self.client._community_extended:
            await self.client.fetch_community(community_id)
        extended = self.client._community_extended.get(community_id)
        return extended.channel_groups if extended else ()

    async def get_groups(self, community_id: str, *, refresh: bool = False):
        return await self.get_channel_groups(community_id, refresh=refresh)

    def get_group(self, group_id: str) -> Optional[ChannelGroup]:
        return self.client.get_channel_group(group_id)

    async def create_channel_group(
        self,
        community_id: str,
        name: str,
        *,
        access_rules: Optional[Iterable[AccessRule]] = None,
    ) -> ChannelGroup:
        group = await self.client.community_admin.create_channel_group(
            community_id,
            name,
            access_rules=access_rules,
        )
        self.client.channel_groups[group.id] = group
        return group

    async def create_group(self, community_id: str, name: str, **kwargs):
        return await self.create_channel_group(community_id, name, **kwargs)

    async def edit_channel_group(
        self,
        community_id: str,
        group_id: str,
        *,
        name: str,
    ) -> ChannelGroup:
        group = await self.client.community_admin.edit_channel_group(
            community_id,
            group_id,
            name=name,
        )
        self.client.channel_groups[group.id] = group
        return group

    async def delete_channel_group(self, community_id: str, group_id: str) -> None:
        await self.client.community_admin.delete_channel_group(
            community_id,
            group_id,
        )
        self.client.channel_groups.pop(normalize_root_guid(group_id), None)

    async def move_channel_group(
        self,
        community_id: str,
        group_id: str,
        *,
        before_group_id: Optional[str] = None,
    ) -> None:
        await self.client.community_admin.move_channel_group(
            community_id,
            group_id,
            before_group_id=before_group_id,
        )

    async def get_channels(
        self,
        community_id: str,
        *,
        refresh: bool = False,
    ) -> Tuple[Channel, ...]:
        groups = await self.get_channel_groups(
            community_id,
            refresh=refresh,
        )
        return tuple(
            channel
            for group in groups
            for channel in group.channels
        )

    def get_channel(self, channel_id: str) -> Optional[Channel]:
        return self.client.get_channel(channel_id)

    async def create_channel(
        self,
        community_id: str,
        channel_group_id: str,
        name: str,
        *,
        description: Optional[str] = None,
        # TEXT, for the reason spelled out on the class above: 0 is
        # Unspecified and Root rejects it.
        channel_type: int = int(ChannelType.TEXT),
        use_channel_group_permission: bool = False,
        icon_token_uri: Optional[str] = None,
        access_rules: Optional[Iterable[AccessRule]] = None,
    ) -> Channel:
        channel = await self.client.community_admin.create_channel(
            community_id,
            channel_group_id,
            name,
            description=description,
            channel_type=channel_type,
            use_channel_group_permission=use_channel_group_permission,
            icon_token_uri=icon_token_uri,
            access_rules=access_rules,
        )
        self.client.channels[channel.id] = channel
        return channel

    async def create_text_channel(
        self,
        community_id: str,
        channel_group_id: str,
        name: str,
        **kwargs,
    ) -> Channel:
        kwargs["channel_type"] = self.TEXT
        return await self.create_channel(
            community_id,
            channel_group_id,
            name,
            **kwargs,
        )

    async def create_voice_channel(
        self,
        community_id: str,
        channel_group_id: str,
        name: str,
        **kwargs,
    ) -> Channel:
        kwargs["channel_type"] = self.VOICE
        return await self.create_channel(
            community_id,
            channel_group_id,
            name,
            **kwargs,
        )

    async def edit_channel(
        self,
        community_id: str,
        channel_id: str,
        *,
        name: str,
        description: Optional[str] = None,
        update_icon: bool = False,
        icon_token_uri: Optional[str] = None,
        use_channel_group_permission: bool = False,
    ) -> Channel:
        channel = await self.client.community_admin.edit_channel(
            community_id,
            channel_id,
            name=name,
            description=description,
            update_icon=update_icon,
            icon_token_uri=icon_token_uri,
            use_channel_group_permission=use_channel_group_permission,
        )
        self.client.channels[channel.id] = channel
        return channel

    async def delete_channel(self, community_id: str, channel_id: str) -> None:
        await self.client.community_admin.delete_channel(
            community_id,
            channel_id,
        )
        self.client.channels.pop(normalize_root_guid(channel_id), None)

    async def move_channel(
        self,
        community_id: str,
        channel_id: str,
        *,
        old_group_id: Optional[str] = None,
        new_group_id: Optional[str] = None,
        before_channel_id: Optional[str] = None,
    ) -> None:
        await self.client.community_admin.move_channel(
            community_id,
            channel_id,
            old_group_id=old_group_id,
            new_group_id=new_group_id,
            before_channel_id=before_channel_id,
        )

    async def get_roles(
        self,
        community_id: str,
        *,
        refresh: bool = False,
    ) -> Tuple[CommunityRole, ...]:
        community_id = normalize_root_guid(community_id)
        if refresh or community_id not in self.client._community_extended:
            await self.client.fetch_community(community_id)
        extended = self.client._community_extended.get(community_id)
        return extended.roles if extended else ()

    async def get_role(
        self,
        community_id: str,
        role_id: str,
        *,
        refresh: bool = False,
    ) -> Optional[CommunityRole]:
        role_id = normalize_root_guid(role_id)
        for role in await self.get_roles(community_id, refresh=refresh):
            if role.id == role_id:
                return role
        return None

    async def create_role(
        self,
        community_id: str,
        name: str,
        *,
        color_hex: Optional[str] = None,
        community_permissions: Optional[CommunityPermission] = None,
        channel_permissions: Optional[ChannelPermissions] = None,
        mentionable: bool = False,
        self_assignable: bool = False,
    ) -> CommunityRole:
        return await self.client.community_admin.create_role(
            community_id,
            name,
            color_hex=color_hex,
            community_permissions=community_permissions,
            channel_permissions=channel_permissions,
            mentionable=mentionable,
            self_assignable=self_assignable,
        )

    async def edit_role(
        self,
        community_id: str,
        role_id: str,
        *,
        name: str,
        color_hex: Optional[str] = None,
        community_permissions: Optional[CommunityPermission] = None,
        channel_permissions: Optional[ChannelPermissions] = None,
        mentionable: bool = False,
        self_assignable: bool = False,
    ) -> CommunityRole:
        """Edit a role. Unsupplied fields keep their current values.

        ``color_hex`` defaults to ``None``, not ``""``. The layer below uses
        ``None`` as the sentinel meaning "read the role and carry its colour
        forward" -- ``CommunityRoleEdit`` is a replace, so an omitted colour
        blanks it. Passing ``""`` down defeated that sentinel and
        ``normalize_hex_colour`` then raised ``ValueError: color_hex must not
        be empty`` before the request was built, so
        ``client.community.edit_role(cid, rid, name="x")`` could not be called
        at all while ``client.admin.edit_role`` with the same arguments
        worked. Same bug shape as the three other replace-semantics fixes:
        a default that looks harmless and is not.
        """
        return await self.client.community_admin.edit_role(
            community_id,
            role_id,
            name=name,
            color_hex=color_hex,
            community_permissions=community_permissions,
            channel_permissions=channel_permissions,
            mentionable=mentionable,
            self_assignable=self_assignable,
        )

    async def delete_role(self, community_id: str, role_id: str) -> None:
        await self.client.community_admin.delete_role(
            community_id,
            role_id,
        )

    async def move_role(
        self,
        community_id: str,
        role_id: str,
        *,
        before_role_id: Optional[str] = None,
    ) -> None:
        await self.client.community_admin.move_role(
            community_id,
            role_id,
            before_role_id=before_role_id,
        )

    async def add_role(
        self,
        community_id: str,
        user_id: str,
        role_id: str,
    ):
        """Give a member a role.

        The wire field is ``UserIds`` (repeated), not ``UserId`` -- this sent
        the singular name and died in the encoder with "has no field
        'user_id'" before a request ever left. Root assigns roles in bulk;
        ``client.roles.add_to_members()`` is the same call for several people
        at once.
        """
        return (
            await self.client.high.community_member_role.add(
                community_id=community_id,
                user_ids=[user_id],
                community_role_id=role_id,
            )
        ).data

    async def remove_role(
        self,
        community_id: str,
        user_id: str,
        role_id: str,
    ):
        """Take a role off a member. ``UserIds`` is repeated here too."""
        return (
            await self.client.high.community_member_role.remove(
                community_id=community_id,
                user_ids=[user_id],
                community_role_id=role_id,
            )
        ).data

    async def get_members(
        self,
        community_id: str,
        *,
        refresh: bool = False,
    ) -> Tuple[CommunityMember, ...]:
        community_id = normalize_root_guid(community_id)
        if refresh or community_id not in self.client._community_extended:
            await self.client.fetch_community(community_id)
        extended = self.client._community_extended.get(community_id)
        return extended.members if extended else ()

    async def get_member(
        self,
        community_id: str,
        user_id: str,
        *,
        refresh: bool = False,
    ) -> Optional[CommunityMember]:
        user_id = normalize_root_guid(user_id)
        for member in await self.get_members(
            community_id,
            refresh=refresh,
        ):
            if member.user_id == user_id:
                return member
        return None

    async def fetch_member(self, community_id: str, user_id: str):
        return await self.client.members.get(
            community_id,
            user_id,
        )

    async def kick(self, community_id: str, user_id: str):
        return await self.client.members.kick(
            community_id,
            user_id,
        )

    async def ban(self, community_id: str, user_id: str, **kwargs):
        return await self.client.members.ban(
            community_id,
            user_id,
            **kwargs,
        )

    async def unban(self, community_id: str, user_id: str):
        return await self.client.members.unban(
            community_id,
            user_id,
        )
