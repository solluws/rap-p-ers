from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional


class RoleManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        name: str,
        *,
        color_hex: str = "",
        community_permission=None,
        channel_permission=None,
        is_mentionable: bool = False,
        is_self_assignable: bool = False,
    ):
        kwargs = {
            "community_id": community_id,
            "name": name,
            "color_hex": color_hex,
            "is_mentionable": is_mentionable,
            "is_self_assignable": is_self_assignable,
        }
        if community_permission is not None:
            kwargs["community_permission"] = community_permission
        if channel_permission is not None:
            kwargs["channel_permission"] = channel_permission

        return (
            await self.client.high.community_role.create(
                **kwargs
            )
        ).data

    async def edit(self, community_id: str, role_id: str, **kwargs):
        kwargs = dict(kwargs)
        kwargs["community_id"] = community_id
        kwargs["id"] = role_id
        return (
            await self.client.high.community_role.edit(
                **kwargs
            )
        ).data

    async def delete(self, community_id: str, role_id: str):
        return (
            await self.client.high.community_role.delete(
                community_id=community_id,
                id=role_id,
            )
        ).data

    async def get(self, community_id: str, role_id: str):
        return (
            await self.client.high.community_role.get(
                community_id=community_id,
                id=role_id,
            )
        ).data

    async def list(self, community_id: str):
        return (
            await self.client.high.community_role.list(
                community_id=community_id,
            )
        ).data

    async def move(
        self,
        community_id: str,
        role_id: str,
        *,
        before_role_id: Optional[str] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "id": role_id,
        }
        if before_role_id is not None:
            kwargs["before_community_role_id"] = before_role_id
        return (
            await self.client.high.community_role.move(
                **kwargs
            )
        ).data

    async def add_to_members(
        self,
        community_id: str,
        role_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_role.add(
                community_id=community_id,
                community_role_id=role_id,
                user_ids=list(user_ids),
            )
        ).data

    async def remove_from_members(
        self,
        community_id: str,
        role_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_role.remove(
                community_id=community_id,
                community_role_id=role_id,
                user_ids=list(user_ids),
            )
        ).data

    async def set_primary(
        self,
        community_id: str,
        user_id: str,
        role_id: str,
    ):
        return (
            await self.client.high.community_member_role.set_primary(
                community_id=community_id,
                user_id=user_id,
                community_role_id=role_id,
            )
        ).data


class MemberManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member.get(
                community_id=community_id,
                user_id=user_id,
            )
        ).data

    async def list(
        self,
        community_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member.list(
                community_id=community_id,
                user_ids=list(user_ids),
            )
        ).data

    async def list_all(self, community_id: str):
        return (
            await self.client.high.community_member.list_all(
                community_id=community_id,
            )
        ).data

    async def edit_nickname(
        self,
        community_id: str,
        user_id: str,
        nickname: str,
    ):
        return (
            await self.client.high.community_member.edit(
                community_id=community_id,
                user_id=user_id,
                nickname=nickname,
            )
        ).data

    async def kick(self, community_id: str, user_id: str):
        return await self.client.moderation.kick(
            community_id,
            user_id,
        )

    async def kick_bulk(
        self,
        community_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_ban.kick_bulk(
                community_id=community_id,
                user_ids=list(user_ids),
            )
        ).data

    async def ban(
        self,
        community_id: str,
        user_id: str,
        *,
        reason: Optional[str] = None,
        expires_at=None,
    ):
        return await self.client.moderation.ban(
            community_id,
            user_id,
            reason=reason,
            expires_at=expires_at,
        )

    async def ban_bulk(
        self,
        community_id: str,
        user_ids: Iterable[str],
        *,
        reason: Optional[str] = None,
        expires_at=None,
    ):
        kwargs = {
            "community_id": community_id,
            "user_ids": list(user_ids),
        }
        if reason is not None:
            kwargs["reason"] = reason
        if expires_at is not None:
            kwargs["expires_at"] = expires_at

        return (
            await self.client.high.community_member_ban.create_bulk(
                **kwargs
            )
        ).data

    async def unban(self, community_id: str, user_id: str):
        return await self.client.moderation.unban(
            community_id,
            user_id,
        )

    async def roles(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member_role.list(
                community_id=community_id,
                user_id=user_id,
            )
        ).data


class CommunityFileManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        container_id: str,
        source: str,
        *,
        directory_id: Optional[str] = None,
    ):
        path = Path(source).expanduser()
        if path.is_file():
            upload_token_uri = await self.client.assets.upload_file(
                str(path)
            )
        else:
            upload_token_uri = source

        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "upload_token_uri": upload_token_uri,
        }
        if directory_id is not None:
            kwargs["directory_id"] = directory_id

        return (
            await self.client.high.file.create(**kwargs)
        ).data

    async def get(self, **kwargs):
        return (await self.client.high.file.get(**kwargs)).data

    async def list(self, **kwargs):
        return (await self.client.high.file.list(**kwargs)).data

    async def edit(self, **kwargs):
        return (await self.client.high.file.edit(**kwargs)).data

    async def move(self, **kwargs):
        return (await self.client.high.file.move(**kwargs)).data

    async def delete(self, **kwargs):
        return (await self.client.high.file.delete(**kwargs)).data

    async def download(self, **kwargs):
        return (await self.client.high.file.download(**kwargs)).data

    async def search(self, **kwargs):
        return (await self.client.high.file.search(**kwargs)).data

    async def search_community(self, **kwargs):
        return (
            await self.client.high.file.search_community(**kwargs)
        ).data


class LogManager:
    def __init__(self, client) -> None:
        self.client = client

    async def community(
        self,
        community_id: str,
        *,
        last_log_id: Optional[str] = None,
    ):
        kwargs = {"community_id": community_id}
        if last_log_id is not None:
            kwargs["last_community_log_id"] = last_log_id
        return (
            await self.client.high.community_log.list(**kwargs)
        ).data

    async def app(self, **kwargs):
        return (
            await self.client.high.community_app_log.list(**kwargs)
        ).data


class CommunityAppManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self, **kwargs):
        return (
            await self.client.high.community_app.get(**kwargs)
        ).data

    async def list(self, **kwargs):
        return (
            await self.client.high.community_app.list(**kwargs)
        ).data

    async def add(self, **kwargs):
        return (
            await self.client.high.community_app.add(**kwargs)
        ).data

    async def remove(self, **kwargs):
        return (
            await self.client.high.community_app.remove(**kwargs)
        ).data

    async def update_version(self, **kwargs):
        return (
            await self.client.high.community_app.update_version(**kwargs)
        ).data

    async def get_settings(self, **kwargs):
        return (
            await self.client.high.community_app.get_settings(**kwargs)
        ).data

    async def set_settings(self, **kwargs):
        return (
            await self.client.high.community_app.set_settings(**kwargs)
        ).data

    async def initialize(self, community_id: str):
        return (
            await self.client.high.community_app.initialize(
                community_id=community_id,
            )
        ).data


class VoiceAdminManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self, community_id: str, container_id: str):
        return (
            await self.client.high.web_rtc.list(
                community_id=community_id,
                container_id=container_id,
            )
        ).data

    async def kick(
        self,
        community_id: str,
        container_id: str,
        user_id: str,
    ):
        return (
            await self.client.high.web_rtc.kick(
                community_id=community_id,
                container_id=container_id,
                user_id=user_id,
            )
        ).data

    async def set_member_mute_deafen(
        self,
        community_id: str,
        container_id: str,
        user_id: str,
        *,
        muted: Optional[bool] = None,
        deafened: Optional[bool] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "user_id": user_id,
        }
        if muted is not None:
            kwargs["is_muted"] = muted
        if deafened is not None:
            kwargs["is_deafened"] = deafened

        return (
            await self.client.high.web_rtc.set_mute_and_deafen_other(
                **kwargs
            )
        ).data


class FriendshipGroupManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self):
        return (
            await self.client.high.friendship_group.list()
        ).data

    async def create(self, name: str):
        return (
            await self.client.high.friendship_group.create(
                name=name,
            )
        ).data

    async def edit(self, group_id: str, name: str):
        return (
            await self.client.high.friendship_group.edit(
                id=group_id,
                name=name,
            )
        ).data

    async def delete(self, group_id: str):
        return (
            await self.client.high.friendship_group.delete(
                id=group_id,
            )
        ).data

    async def move(
        self,
        group_id: str,
        *,
        before_group_id: Optional[str] = None,
    ):
        kwargs = {"id": group_id}
        if before_group_id is not None:
            kwargs["before_friendship_group_id"] = before_group_id
        return (
            await self.client.high.friendship_group.move(
                **kwargs
            )
        ).data
