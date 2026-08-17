from __future__ import annotations

from typing import Iterable, Optional

from .identifiers import normalize_root_guid


class EmojiManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self, community_id: str):
        return (
            await self.client.high.community_emoji.list(
                community_id=community_id,
            )
        ).data

    async def list_mine(self):
        return (
            await self.client.high.community_emoji.list_mine()
        ).data

    async def resolve(self, emoji_ids: Iterable[str]):
        return (
            await self.client.high.community_emoji.resolve(
                ids=list(emoji_ids),
            )
        ).data

    async def create(
        self,
        community_id: str,
        shortcode: str,
        source: str,
    ):

        from pathlib import Path

        path = Path(source).expanduser()
        if path.is_file():
            token_uri = await self.client.assets.upload_file(
                str(path)
            )
        else:
            token_uri = source

        return (
            await self.client.high.community_emoji.create(
                community_id=community_id,
                shortcode=shortcode,
                upload_token_uri=token_uri,
            )
        ).data

    async def delete(self, community_id: str, emoji_id: str):
        return (
            await self.client.high.community_emoji.delete(
                community_id=community_id,
                id=emoji_id,
            )
        ).data


class ModerationManager:
    def __init__(self, client) -> None:
        self.client = client

    async def invite_user(
        self,
        community_id: str,
        user_id: str,
        *,
        role_ids: Optional[Iterable[str]] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "invited_user_id": user_id,
        }
        if role_ids:
            kwargs["community_role_ids"] = list(role_ids)
        return (
            await self.client.high.community_member_invite.create(
                **kwargs
            )
        ).data

    async def kick(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member_ban.kick(
                community_id=community_id,
                user_id=user_id,
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
        kwargs = {
            "community_id": community_id,
            "user_id": user_id,
        }
        if reason is not None:
            kwargs["reason"] = reason
        if expires_at is not None:
            kwargs["expires_at"] = expires_at
        return (
            await self.client.high.community_member_ban.create(
                **kwargs
            )
        ).data

    async def unban(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member_ban.delete(
                community_id=community_id,
                user_id=user_id,
            )
        ).data

    async def list_bans(self, community_id: str):
        return (
            await self.client.high.community_member_ban.list(
                community_id=community_id,
            )
        ).data


class FriendManager:
    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def _field(obj, name):
        """Read a field from an AttrDict / dict / object, or None."""
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    async def groups(self):
        """Return your friendship groups (each carries its own friendships).

        Root has no flat "list friends" RPC -- friendships are grouped, so the
        friend list is the union of every group's ``friendships``.
        """
        return (await self.client.high.friendship_group.list()).data

    async def list(self):
        """Return your friendships, flattened across all friendship groups.

        Each item is a friendship record with at least ``friend_user_id``.
        """
        data = await self.groups()
        groups = self._field(data, "friendship_groups") or []
        friendships = []
        for group in groups:
            for friendship in (self._field(group, "friendships") or []):
                friendships.append(friendship)
        return friendships

    async def friend_ids(self) -> set:
        """Return the set of your friends' user ids (normalized)."""
        ids: set = set()
        for friendship in await self.list():
            raw = self._field(friendship, "friend_user_id")
            if isinstance(raw, str) and raw:
                try:
                    ids.add(normalize_root_guid(raw))
                except (TypeError, ValueError):
                    pass
        return ids

    async def get(self, user_id: str):
        """Return the friendship record for ``user_id``, or None."""
        target = normalize_root_guid(user_id)
        for friendship in await self.list():
            raw = self._field(friendship, "friend_user_id")
            if isinstance(raw, str):
                try:
                    if normalize_root_guid(raw) == target:
                        return friendship
                except (TypeError, ValueError):
                    continue
        return None

    async def is_friend(self, user_id: str) -> bool:
        """True if ``user_id`` is in your friends."""
        return await self.get(user_id) is not None

    async def get_all(self):
        return await self.list()

    async def request(self, username: str):
        return (
            await self.client.high.friendship_invite.create(
                username=username,
            )
        ).data

    async def respond(
        self,
        notification_id: str,
        user_id: str,
        *,
        accept: bool,
    ):
        return (
            await self.client.high.friendship_invite.respond(
                notification_id=notification_id,
                friend_user_id=user_id,
                is_friendship_accepted=accept,
            )
        ).data

    async def accept(self, notification_id: str, user_id: str):
        return await self.respond(
            notification_id,
            user_id,
            accept=True,
        )

    async def reject(self, notification_id: str, user_id: str):
        return await self.respond(
            notification_id,
            user_id,
            accept=False,
        )

    async def remove(self, user_id: str):
        return (
            await self.client.high.friendship.delete(
                user_id=user_id,
            )
        ).data


class BlockManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self):
        return (
            await self.client.high.user.block_list()
        ).data

    async def block(self, user_id: str):
        return (
            await self.client.high.user.block_create(
                block_user_id=user_id,
            )
        ).data

    async def unblock(self, user_id: str):
        return (
            await self.client.high.user.block_delete(
                block_user_id=user_id,
            )
        ).data


class InviteManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        *,
        code: Optional[str] = None,
        max_uses: Optional[int] = None,
        expires_at=None,
    ):
        kwargs = {"community_id": community_id}
        if code is not None:
            kwargs["code"] = code
        if max_uses is not None:
            kwargs["max_uses"] = max_uses
        if expires_at is not None:
            kwargs["expires_at"] = expires_at

        return (
            await self.client.high.link.community_invite_link_create(
                **kwargs
            )
        ).data

    async def info(self, code: str):
        return (
            await self.client.high.link.community_invite_link_get_info(
                code=code,
            )
        ).data

    async def code_exists(self, code: str) -> bool:
        result = (
            await self.client.high.link.community_invite_link_code_exists(
                code=code,
            )
        ).data
        for key in ("exists", "code_exists", "is_exists"):
            if key in result:
                return bool(result[key])
        return bool(result)

    async def list(self, community_id: str):
        return (
            await self.client.high.link.community_invite_link_list(
                community_id=community_id,
            )
        ).data

    async def list_mine(self, community_id: str):
        return (
            await self.client.high.link.community_invite_link_list_mine(
                community_id=community_id,
            )
        ).data

    async def delete(self, community_id: str, invite_id: str):
        return (
            await self.client.high.link.community_invite_link_delete(
                community_id=community_id,
                id=invite_id,
            )
        ).data

    async def join(
        self,
        code: str,
        *,
        age_verified: bool = False,
    ):
        return (
            await self.client.high.community_member_invite.link_join(
                code=code,
                is_age_verified=age_verified,
            )
        ).data


class FriendRequestManager:
    """Sending and answering friend requests.

        await client.friend_requests.send("someuser")

        # answering one you received
        pending = await client.friend_requests.pending()
        await client.friend_requests.accept(pending[0])
    """

    def __init__(self, client) -> None:
        self.client = client

    async def send(self, username: str):
        """Send a friend request to ``username``.

        Root addresses friend requests by username, not id -- that's what the
        official client sends.
        """
        username = (username or "").strip().lstrip("@")
        if not username:
            raise ValueError("username is required")
        return (
            await self.client.high.friendship_invite.create(
                username=username,
            )
        ).data

    async def respond(self, notification, *, accept: bool):
        """Accept or decline a request.

        ``notification`` may be a notification object from
        :meth:`pending`, or a ``(notification_id, user_id)`` pair.
        """
        notification_id, user_id = self._unpack(notification)
        return (
            await self.client.high.friendship_invite.respond(
                notification_id=notification_id,
                friend_user_id=user_id,
                is_friendship_accepted=accept,
            )
        ).data

    async def accept(self, notification):
        """Accept a friend request."""
        return await self.respond(notification, accept=True)

    async def decline(self, notification):
        """Decline a friend request."""
        return await self.respond(notification, accept=False)

    async def pending(self):
        """Notifications that look like incoming friend requests."""
        notifications = await self.client.notifications.list()
        items = getattr(notifications, "notifications", notifications) or []
        pending = []
        for item in items:
            kind = getattr(item, "notification_type", None)
            name = getattr(kind, "name", str(kind or "")).upper()
            if "FRIEND" in name:
                pending.append(item)
        return pending

    @staticmethod
    def _unpack(notification):
        if isinstance(notification, (tuple, list)) and len(notification) == 2:
            return notification[0], notification[1]
        notification_id = getattr(notification, "id", None)
        user_id = (
            getattr(notification, "user_id", None)
            or getattr(notification, "friend_user_id", None)
            or getattr(notification, "sender_user_id", None)
        )
        if not notification_id or not user_id:
            raise ValueError(
                "pass a notification object, or a (notification_id, user_id) pair"
            )
        return notification_id, user_id


class NotificationManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self, **kwargs):
        return (
            await self.client.high.notification.list(**kwargs)
        ).data

    async def count_unviewed(self):
        return (
            await self.client.high.notification.count_unviewed()
        ).data

    async def mark_viewed(self, notification_id: str):
        return (
            await self.client.high.notification.set_viewed(
                id=notification_id,
            )
        ).data

    async def mark_all_viewed(self):
        return (
            await self.client.high.notification.set_all_viewed()
        ).data

    async def delete(self, notification_id: str):
        return (
            await self.client.high.notification.delete(
                id=notification_id,
            )
        ).data

    async def delete_all(self):
        return (
            await self.client.high.notification.delete_all()
        ).data


class UserSettingsManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get_note(self, user_id: str):
        return (
            await self.client.high.user.get_user_note(
                user_id=user_id,
            )
        ).data

    async def set_note(self, user_id: str, note: str):
        return (
            await self.client.high.user.set_user_note(
                user_id=user_id,
                note=note,
            )
        ).data

    async def set_max_online_status(self, status):
        return (
            await self.client.high.user.set_max_online_status(
                max_status=status,
            )
        ).data

    async def set_device_online_status(self, status):
        return (
            await self.client.high.user.set_device_online_status(
                status=status,
            )
        ).data

    async def resend_verification_email(self):
        return (
            await self.client.high.user.resend_email_verification_code()
        ).data

    async def set_community_invite_requirement(
        self,
        connection,
        required: bool,
    ):
        return (
            await self.client.high.user.set_community_invite_requirement(
                connection=connection,
                is_required=required,
            )
        ).data

    async def set_dm_invite_requirement(
        self,
        connection,
        required: bool,
    ):
        return (
            await self.client.high.user.set_direct_message_invite_requirement(
                connection=connection,
                is_required=required,
            )
        ).data

    async def set_friend_invite_requirement(
        self,
        connection,
        required: bool,
    ):
        return (
            await self.client.high.user.set_friendship_invite_requirement(
                connection=connection,
                is_required=required,
            )
        ).data


class DirectoryManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        container_id: str,
        name: str,
        *,
        parent_directory_id: Optional[str] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "name": name,
        }
        if parent_directory_id is not None:
            kwargs["parent_directory_id"] = parent_directory_id
        return (
            await self.client.high.directory.create(**kwargs)
        ).data

    async def edit(self, **kwargs):
        return (
            await self.client.high.directory.edit(**kwargs)
        ).data

    async def move(self, **kwargs):
        return (
            await self.client.high.directory.move(**kwargs)
        ).data

    async def delete(self, **kwargs):
        return (
            await self.client.high.directory.delete(**kwargs)
        ).data

    async def get(self, **kwargs):
        return (
            await self.client.high.directory.get(**kwargs)
        ).data

    async def list(self, **kwargs):
        return (
            await self.client.high.directory.list(**kwargs)
        ).data


class SearchManager:
    def __init__(self, client) -> None:
        self.client = client

    async def messages(self, **kwargs):

        service = getattr(
            self.client.high,
            "message_v2",
            None,
        )
        if service is None:
            service = self.client.high.message
        return (await service.search(**kwargs)).data

    async def files(self, **kwargs):
        return (
            await self.client.high.file.search_community(**kwargs)
        ).data
