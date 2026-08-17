from __future__ import annotations

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
    TEXT = 0
    VOICE = 1

    def __init__(self, client) -> None:
        self.client = client
        self.permissions = PermissionManager(client)

    def cached(self) -> Tuple[Community, ...]:
        return tuple(self.client.communities.values())

    async def list(self, *, refresh: bool = True) -> Tuple[Community, ...]:
        if refresh:
            await self.client.refresh_communities()
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
        channel_type: int = 0,
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
        color_hex: str = "",
        community_permissions: Optional[CommunityPermission] = None,
        channel_permissions: Optional[ChannelPermissions] = None,
        mentionable: bool = False,
        self_assignable: bool = False,
    ) -> CommunityRole:
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
        return (
            await self.client.high.community_member_role.add(
                community_id=community_id,
                user_id=user_id,
                community_role_id=role_id,
            )
        ).data

    async def remove_role(
        self,
        community_id: str,
        user_id: str,
        role_id: str,
    ):
        return (
            await self.client.high.community_member_role.remove(
                community_id=community_id,
                user_id=user_id,
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
