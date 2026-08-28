from __future__ import annotations

from .enums import NotificationType

from typing import Iterable, Optional

from .identifiers import normalize_root_guid
from .responses import field as _field, as_sequence as _as_sequence


def _notification_counterparty(notification):
    """The *other* person in a friend-request notification.

    ``NotificationPacket.UserId`` is the notification's owner -- you -- not
    whoever sent it. Confirmed live: a request from account A arrives in
    account B's inbox carrying B's own id at the top level. The counterparty
    lives in the payload instead:

        NotificationPayloadFriendshipInviteCreated { UserId, FriendUserId }

    Reading the top-level field meant ``respond()`` sent your own id as
    ``FriendUserId``, i.e. "accept friendship with myself".

    Rather than assume which of the two payload fields is the sender, this
    collects both and returns whichever is not the notification's owner.
    """
    owner = _field(notification, "user_id")
    payload = _field(notification, "payload")

    candidates = []
    for key in ("friendship_invite_created", "friendship_invite_responded"):
        block = _field(payload, key)
        if block is None:
            continue
        for name in ("friend_user_id", "user_id"):
            value = _field(block, name)
            if value:
                candidates.append(value)

    # Flat shapes, in case the payload was already unwrapped upstream.
    for name in ("friend_user_id", "sender_user_id"):
        value = _field(notification, name)
        if value:
            candidates.append(value)

    for candidate in candidates:
        if not _same_id(candidate, owner):
            return candidate
    return candidates[0] if candidates else None


def _user_id_of(user):
    """A user id from an id string, a user object, or None for a username."""
    if user is None:
        return None
    if isinstance(user, str):
        return user if "-" in user or len(user) > 20 else None
    return getattr(user, "id", None) or getattr(user, "user_id", None)


def _username_of(user):
    """A casefolded username, if that is what we were given."""
    if isinstance(user, str):
        return None if _user_id_of(user) else user.lstrip("@").casefold()
    name = getattr(user, "username", None)
    return name.lstrip("@").casefold() if name else None


def _same_id(left, right) -> bool:
    """Compare two Root ids without caring about encoding."""
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        from .identifiers import normalize_root_guid

        return normalize_root_guid(left) == normalize_root_guid(right)
    except Exception:
        return False


class EmojiManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self, community_id: str):
        """The community's custom emoji, as a list of records.

        Returns the items, not the ``CommunityEmojiListResponse`` envelope --
        iterating the envelope yields field names.
        """
        from .domain_managers import unwrap_list

        response = await self.client.high.community_emoji.list(
            community_id=community_id,
        )
        return unwrap_list(response.data)

    async def list_mine(self):
        """Emoji across every community you are in, as a list."""
        from .domain_managers import unwrap_list

        response = await self.client.high.community_emoji.list_mine()
        return unwrap_list(response.data)

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
        """The community's bans, as a list.

        Returns the items rather than the ``CommunityMemberBanListResponse``
        envelope -- iterating the envelope yielded field names, so callers got
        strings where they expected ban records.
        """
        from .domain_managers import unwrap_list

        return unwrap_list(
            (
                await self.client.high.community_member_ban.list(
                    community_id=community_id,
                )
            ).data
        )


class FriendManager:
    def __init__(self, client) -> None:
        self.client = client

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
        groups = _field(data, "friendship_groups") or []
        friendships = []
        for group in groups:
            for friendship in (_field(group, "friendships") or []):
                friendships.append(friendship)
        return friendships

    async def friend_ids(self) -> set:
        """Return the set of your friends' user ids (normalized)."""
        ids: set = set()
        for friendship in await self.list():
            raw = _field(friendship, "friend_user_id")
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
            raw = _field(friendship, "friend_user_id")
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
        """Block a user. **This destroys an existing friendship.**

        Not a side effect you would guess, and it is not reversible by
        unblocking. Measured directly::

            friends            -> True
            block()            -> friends: False   (both directions)
            unblock()          -> friends: False

        So ``block`` then ``unblock`` is *not* a no-op: it silently removes
        the friendship and leaves it removed. Anything that depends on the
        friendship -- DMs and calls both do, since Root's default privacy
        setting only accepts DMs from friends -- stops working afterwards,
        and the failure surfaces later and elsewhere as ``PERMISSION_DENIED``.

        This cost a live run to find: the two test accounts were friends
        before ``TestBlocking`` and not friends after it, and nothing in
        between looked at the friendship, so every earlier run had been
        quietly tearing it down and re-establishing it on the next run's
        fixture.

        If you need the friendship back, send a fresh request and have it
        accepted.
        """
        return (
            await self.client.high.user.block_create(
                block_user_id=user_id,
            )
        ).data

    async def unblock(self, user_id: str):
        """Unblock a user. Does **not** restore a friendship ``block`` removed."""
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

    async def pending_from(self, user):
        """The pending friend request from ``user``, or None.

        ``pending()`` returns every incoming request, so answering one from a
        person you already know meant listing notifications, filtering by
        type, and matching ids by hand at the call site -- the same handful of
        lines in every caller. Accepts a user id, a user object, or a
        username.
        """
        wanted_id = _user_id_of(user)
        wanted_name = _username_of(user)
        for notification in await self.pending():
            sender_id = _notification_counterparty(notification)
            if wanted_id and _same_id(sender_id, wanted_id):
                return notification
            if wanted_name:
                sender_name = _field(notification, "username")
                if sender_name and sender_name.lstrip("@").casefold() == wanted_name:
                    return notification
        return None

    async def accept_all(self):
        """Accept every pending request; returns how many were accepted.

        Useful when you do not care who sent them -- and, unlike matching on
        a sender, it does not depend on reading the counterparty correctly.
        """
        accepted = 0
        for notification in await self.pending():
            try:
                await self.accept(notification)
            except Exception:
                continue
            accepted += 1
        return accepted

    async def accept_from(self, user):
        """Accept the pending request from ``user``; True if there was one."""
        notification = await self.pending_from(user)
        if notification is None:
            return False
        await self.accept(notification)
        return True

    async def decline_from(self, user):
        """Decline the pending request from ``user``; True if there was one."""
        notification = await self.pending_from(user)
        if notification is None:
            return False
        await self.decline(notification)
        return True

    async def decline(self, notification):
        """Decline a friend request."""
        return await self.respond(notification, accept=False)

    async def pending(self):
        """Incoming friend requests awaiting your answer.

        Two things this used to get wrong, both of which made it return the
        wrong set rather than fail:

        * It read ``notification_type.name`` and matched the substring
          "FRIEND". The wire carries a plain integer, so ``name`` fell back to
          ``str(1)`` and matched nothing -- ``pending()`` was silently always
          empty. ``NotificationType.coerce`` existed for exactly this and was
          not used.
        * Matching on "FRIEND" also caught ``FRIENDSHIP_INVITE_RESPONDED`` --
          somebody answering a request *you* sent. Those are not pending and
          cannot be accepted.

        Now coerces the value and matches the one type that means "someone
        wants to be your friend".
        """
        notifications = await self.client.notifications.list()
        items = getattr(notifications, "notifications", notifications) or []
        wanted = NotificationType.FRIENDSHIP_INVITE_CREATED
        pending = []
        for item in items:
            kind = NotificationType.coerce(_field(item, "notification_type"))
            if kind == wanted:
                pending.append(item)
        return pending

    async def responded(self):
        """Notifications that a request *you* sent was answered."""
        notifications = await self.client.notifications.list()
        items = getattr(notifications, "notifications", notifications) or []
        wanted = NotificationType.FRIENDSHIP_INVITE_RESPONDED
        return [
            item
            for item in items
            if NotificationType.coerce(_field(item, "notification_type")) == wanted
        ]

    @staticmethod
    def _unpack(notification):
        """``(notification_id, counterparty_user_id)`` for the respond RPC.

        The counterparty comes from the payload, not the notification's
        top-level ``user_id`` -- that field is the owner (you). See
        :func:`_notification_counterparty`.
        """
        if isinstance(notification, (tuple, list)) and len(notification) == 2:
            return notification[0], notification[1]
        notification_id = _field(notification, "id")
        user_id = _notification_counterparty(notification)
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

    async def count_unviewed(self) -> int:
        """Total unviewed notifications across every container.

        Root answers with ``NotificationCountListResponse`` -- a repeated list
        of per-container ``{container_id, count}`` -- so this used to hand
        back a raw payload object for something whose whole point is a number.
        Use :meth:`counts_by_container` if you need the breakdown.
        """
        return sum(entry for entry in (await self.counts_by_container()).values())

    async def counts_by_container(self) -> dict:
        """Unviewed notification counts as ``{container_id: count}``."""
        data = (await self.client.high.notification.count_unviewed()).data
        entries = _as_sequence(_field(data, "notification_counts"))
        counts = {}
        for entry in entries:
            container = _field(entry, "container_id")
            value = _field(entry, "count")
            if container is None:
                continue
            counts[str(container)] = int(value or 0)
        return counts

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
        """The private note you have saved about ``user_id``, or None.

        Returns the note text. Root's ``UserNoteResponse`` also echoes
        ``user_id`` and ``note_user_id``, and this used to return that whole
        object -- so callers got a payload dict where they asked for a string,
        and an unset note looked like a populated response.
        """
        data = (
            await self.client.high.user.get_user_note(user_id=user_id)
        ).data
        note = _field(data, "note")
        return note or None

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

    # These three carry *two* independent conditions, not one. ``connection``
    # says how closely someone must already be linked to you (anyone / shares
    # a community / a friend / nobody); ``email_verified`` additionally
    # requires that they have verified their email address. They are ANDed --
    # ``ANY`` plus ``email_verified=True`` means "anyone with a verified
    # address".
    #
    # All three used to send ``is_required``, which is not a field on any of
    # the three requests -- the codec raises ``TypeError`` before the request
    # is built, so none of them could ever have worked. The wire field is
    # ``IsEmailVerified``. See ``devscripts/kwargcheck.py``, which now sweeps
    # every ``high.*`` call site for the same mistake.

    async def set_community_invite_requirement(
        self,
        connection,
        email_verified: bool = False,
    ):
        """Restrict who may invite you to a community.

        ``connection`` is a :class:`~rootpy.enums.UserCommunityInviteConnection`
        (or its int). ``email_verified`` additionally requires the inviter to
        have a verified email address.
        """
        return (
            await self.client.high.user.set_community_invite_requirement(
                connection=connection,
                is_email_verified=email_verified,
            )
        ).data

    async def set_dm_invite_requirement(
        self,
        connection,
        email_verified: bool = False,
    ):
        """Restrict who may open a DM with you.

        ``connection`` is a
        :class:`~rootpy.enums.UserDirectMessageInviteConnection` (or its int).
        This is the gate that answers ``PERMISSION_DENIED`` to
        ``DirectMessageCreate`` between two accounts that are not friends.
        """
        return (
            await self.client.high.user.set_direct_message_invite_requirement(
                connection=connection,
                is_email_verified=email_verified,
            )
        ).data

    async def set_friend_invite_requirement(
        self,
        connection,
        email_verified: bool = False,
    ):
        """Restrict who may send you a friend request.

        ``connection`` is a
        :class:`~rootpy.enums.UserFriendshipInviteConnection` (or its int),
        which has no ``FRIEND`` member -- requiring friendship in order to be
        friended is not a state Root models.
        """
        return (
            await self.client.high.user.set_friendship_invite_requirement(
                connection=connection,
                is_email_verified=email_verified,
            )
        ).data


class DirectoryManager:
    """Folders inside a channel.

    These took ``**kwargs`` and forwarded them blind, which hid every required
    argument: ``delete(directory_id=...)`` reads perfectly and the wire field
    is ``Id``. Named parameters now, taken from the request schemas, so the
    signature is the documentation.
    """

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

    async def list(self, community_id: str, container_id: str):
        """Directories in a channel -- the items, not the envelope."""
        from .domain_managers import unwrap_list

        return unwrap_list(
            (
                await self.client.high.directory.list(
                    community_id=community_id,
                    container_id=container_id,
                )
            ).data
        )

    async def get(
        self, community_id: str, container_id: str, directory_id: str
    ):
        return (
            await self.client.high.directory.get(
                community_id=community_id,
                container_id=container_id,
                id=directory_id,
            )
        ).data

    async def edit(
        self, community_id: str, container_id: str, directory_id: str, name: str
    ):
        return (
            await self.client.high.directory.edit(
                community_id=community_id,
                container_id=container_id,
                id=directory_id,
                name=name,
            )
        ).data

    async def move(
        self,
        community_id: str,
        container_id: str,
        directory_id: str,
        *,
        old_parent_directory_id: Optional[str] = None,
        new_parent_directory_id: Optional[str] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "id": directory_id,
        }
        if old_parent_directory_id is not None:
            kwargs["old_parent_directory_id"] = old_parent_directory_id
        if new_parent_directory_id is not None:
            kwargs["new_parent_directory_id"] = new_parent_directory_id
        return (await self.client.high.directory.move(**kwargs)).data

    async def delete(
        self, community_id: str, container_id: str, directory_id: str
    ):
        """Delete a directory. The wire field is ``Id``, not ``DirectoryId``."""
        return (
            await self.client.high.directory.delete(
                community_id=community_id,
                container_id=container_id,
                id=directory_id,
            )
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
