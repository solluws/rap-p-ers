"""High-level direct-message helper.

``DMMemberService`` is the friendly, one-call surface for talking to a
single user. It sits on top of :class:`DirectMessageService` (which owns the
raw List/Find/Create RPCs) and :class:`MessageService` (which owns message
sending), and hides the detail that a Root DM is addressed by its full
member set ``{self, other}`` rather than by the other user alone.

Typical use::

    await client.dm.send("00308891-...-412ed25f", "hello")

    dm = await client.dm.open(user)          # DirectMessage (get-or-create)
    await client.dm.send(user, "hi again")   # reuses the cached DM

Accepted "user" values everywhere: a ``User`` object, a ``CurrentUser``, any
object with an ``.id``, a dashed UUID string, or Root's 22-char base64url id.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Union

from .exceptions import (
    DirectMessageError,
    GrpcWebError,
    GrpcStatus,
)
from .identifiers import normalize_root_guid
from .models import Message, MessageSendResult
from .services.direct_messages import DirectMessage, DirectMessageService
from .services.messages import MessageService

log = logging.getLogger("rootpy.dm")

UserLike = Union[str, object]


def _resolve_user_id(user: UserLike) -> str:
    """Coerce a User / CurrentUser / id-string into a canonical user id."""
    if user is None:
        raise DirectMessageError("A user or user id is required")
    raw = getattr(user, "id", None)
    if raw is None:
        raw = user
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        return normalize_root_guid(raw)
    except ValueError as exc:
        raise DirectMessageError(
            f"{raw!r} is not a valid Root user id (expected a dashed UUID "
            "or a 22-character base64url id)"
        ) from exc


class DMMemberService:
    """One-call direct messaging against a single member.

    Parameters
    ----------
    client:
        The owning :class:`RootClient`. The service reads ``client.dm_service``
        (the low-level :class:`DirectMessageService`) and ``client.messages``
        (the :class:`MessageService`) from it, and ``client.user_id`` to build
        the DM member set.
    """

    def __init__(self, client) -> None:
        self._client = client

    # ------------------------------------------------------------------ #
    # wiring
    # ------------------------------------------------------------------ #
    @property
    def _dms(self) -> DirectMessageService:
        service = getattr(self._client, "dm_service", None) or getattr(
            self._client, "direct_messages", None
        )
        if service is None:
            raise DirectMessageError(
                "The client has no DirectMessageService configured"
            )
        return service

    @property
    def _messages(self) -> MessageService:
        service = getattr(self._client, "messages", None)
        if service is None:
            raise DirectMessageError(
                "The client has no MessageService configured"
            )
        return service

    def _require_login(self) -> None:
        if getattr(self._client, "user_id", None) is None:
            raise DirectMessageError(
                "Not logged in: call client.login(...) (or client.start()) "
                "before sending a direct message."
            )

    # ------------------------------------------------------------------ #
    # DM resolution
    # ------------------------------------------------------------------ #
    async def open(self, user: UserLike) -> DirectMessage:
        """Return the 1:1 DM with ``user``, creating it if necessary.

        Uses the cache populated at login, then Find, then Create. Always
        addresses the DM by its full ``{self, other}`` member set, so it
        never trips the ``INVALID_ARGUMENT`` that a bare target id causes.

        If Root refuses to open the DM with ``PERMISSION_DENIED`` /
        ``FAILED_PRECONDITION`` (the "you can't message this person" gate),
        the raw gRPC error is replaced with a :class:`DirectMessageError`
        that says *why* -- most often that you aren't friends and the target
        only accepts DMs from friends. The original error is kept as
        ``__cause__``.
        """
        self._require_login()
        user_id = _resolve_user_id(user)
        try:
            dm = await self._dms.get_or_create(user_id)
        except GrpcWebError as exc:
            if exc.status in (
                GrpcStatus.PERMISSION_DENIED,
                GrpcStatus.FAILED_PRECONDITION,
            ):
                raise await self._diagnose_dm_gate(user_id, exc) from exc
            raise
        log.debug("opened dm %s with %s", dm.id, user_id)
        return dm

    @staticmethod
    def _extract_ids(data) -> set:
        """Best-effort collection of user-id-like values from a service result.

        Friend/block payloads vary in shape (dicts or objects, differing field
        names), so this scans a handful of likely id fields rather than assuming
        one schema. Anything it can't parse is simply skipped.
        """
        ids: set = set()
        if data is None:
            return ids
        items = data if isinstance(data, (list, tuple)) else [data]
        fields = (
            "user_id", "id", "friend_user_id", "other_user_id",
            "block_user_id", "blocked_user_id", "target_user_id",
        )
        for item in items:
            for field in fields:
                value = (
                    item.get(field)
                    if isinstance(item, dict)
                    else getattr(item, field, None)
                )
                if isinstance(value, str) and value:
                    try:
                        ids.add(normalize_root_guid(value))
                    except (TypeError, ValueError):
                        pass
        return ids

    async def _is_friend(self, user_id: str):
        """True/False if determinable, else None (diagnosis is best-effort)."""
        friends = getattr(self._client, "friends", None)
        if friends is None:
            return None
        try:
            return await friends.is_friend(user_id)
        except Exception:
            return None

    async def _did_i_block(self, user_id: str):
        blocks = getattr(self._client, "blocks", None)
        if blocks is None:
            return None
        try:
            listing = await blocks.list()
        except Exception:
            return None
        return user_id in self._extract_ids(listing)

    async def _diagnose_dm_gate(
        self,
        user_id: str,
        original: GrpcWebError,
    ) -> DirectMessageError:
        """Turn a DM permission gate into an actionable error message."""
        # If Root itself tagged the error with a friend-user payload, that's a
        # definitive "you need to be friends" signal -- no guessing needed.
        if getattr(original, "payload_kind", None) == "friend_user":
            return DirectMessageError(
                f"Can't DM {user_id}: Root reports this needs a friendship "
                "first. Send a friend request with client.add_friend(username), "
                "or wait for them to accept."
            )

        # A block on our side is definitive, so check it next.
        blocked = await self._did_i_block(user_id)
        if blocked:
            return DirectMessageError(
                f"Can't DM {user_id}: you have blocked this user. "
                "Unblock them first with client.unblock(user)."
            )

        is_friend = await self._is_friend(user_id)
        if is_friend is False:
            return DirectMessageError(
                f"Can't DM {user_id}: you're not friends, and Root accounts "
                "commonly only accept direct messages from friends -- that's "
                "the most likely cause. Send a friend request first with "
                "client.add_friend(username). (Other possibilities: they've "
                "blocked you, or their settings restrict who can DM them.)"
            )
        if is_friend is True:
            return DirectMessageError(
                f"Can't DM {user_id}: you're already friends, so the "
                "friend-only requirement isn't the cause. Most likely they've "
                "blocked you, or their privacy settings restrict DMs even from "
                "friends."
            )
        # Friendship couldn't be determined -- give the ranked-likelihood note.
        return DirectMessageError(
            f"Can't DM {user_id}: Root refused to open the conversation "
            f"({original.status.name}). The usual causes, in order: you're not "
            "friends and they only accept DMs from friends (try "
            "client.add_friend(username)); they've blocked you; or their "
            "privacy settings restrict who can DM them."
        )

    async def find(self, user: UserLike) -> Optional[DirectMessage]:
        """Return the existing DM with ``user`` or ``None`` (never creates)."""
        self._require_login()
        return await self._dms.find(_resolve_user_id(user))

    def cached(self, user: UserLike) -> Optional[DirectMessage]:
        """Return a locally cached DM with ``user`` without any network call."""
        try:
            user_id = _resolve_user_id(user)
        except DirectMessageError:
            return None
        return self._dms._by_user_id.get(user_id)

    # ------------------------------------------------------------------ #
    # sending
    # ------------------------------------------------------------------ #
    async def send(
        self,
        user: UserLike,
        content: str,
        *,
        attachment_token_uris: Optional[List[str]] = None,
        parent_message_ids: Optional[List[str]] = None,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """Open (or reuse) the DM with ``user`` and send ``content``.

        This is the shortcut most callers want::

            await client.dm.send(user, "hello")

        ``community_id`` is intentionally never set for a DM -- the container
        is the direct message itself.
        """
        if not isinstance(content, str) or not content.strip():
            raise DirectMessageError("Message content cannot be empty")

        dm = await self.open(user)
        result = await self._messages.send(
            dm.id,
            content,
            community_id=None,
            attachment_token_uris=attachment_token_uris,
            parent_message_ids=parent_message_ids,
            delete_after=delete_after,
        )
        log.debug("sent dm message %s in %s", result.id, dm.id)
        return result

    async def reply(
        self,
        message: Message,
        content: str,
        *,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """Reply within the DM that ``message`` belongs to.

        A received DM message already carries its container, so this needs no
        member-set resolution at all -- it sends straight to the container.
        """
        container_id = getattr(message, "container_id", None)
        if not container_id:
            raise DirectMessageError(
                "message has no container_id to reply into"
            )
        return await self._messages.send(
            container_id,
            content,
            community_id=None,
            parent_message_ids=[message.id],
            needs_parent_notification=True,
            delete_after=delete_after,
        )
