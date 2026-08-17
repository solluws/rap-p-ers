from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..exceptions import (
    DirectMessageError,
    GrpcNotFound,
    GrpcStatus,
    GrpcWebError,
)
from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    format_root_guid,
    normalize_root_guid,
)
from ..protocol import (
    decode_root_guid_message,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    length_field,
)
from ..transport import GrpcWebTransport

log = logging.getLogger("rootpy.direct_messages")

BASE = "https://api.rootapp.com/root.DirectMessageGrpcService"
DIRECT_MESSAGE_LIST = f"{BASE}/List"
DIRECT_MESSAGE_FIND = f"{BASE}/Find"
DIRECT_MESSAGE_CREATE = f"{BASE}/Create"


@dataclass(frozen=True)
class DirectMessage:
    id: str
    creator_user_id: Optional[str]
    member_user_ids: tuple[str, ...]
    command_id: Optional[str] = None
    raw: bytes = b""
    _client: Optional[object] = field(default=None, repr=False, compare=False)

    def _require_client(self):
        if self._client is None:
            raise RuntimeError(
                "This DirectMessage is not attached to a RootClient"
            )
        return self._client

    async def send(self, content: str, **kwargs):
        """Send a message in this DM conversation.

            dm = await client.open_dm(user_id)
            await dm.send("hey")
        """
        return await self._require_client().messages.send(
            self.id, content, **kwargs
        )

    async def history(self, *, limit: Optional[int] = None, before=None):
        """Fetch this conversation's messages."""
        return await self._require_client().messages.list(
            self.id, direction="both", after=before, limit=limit
        )


class DirectMessageService:
    """Low-level DirectMessage RPCs (List / Find / Create).

    Root models a direct message as the *set* of its members. A 1:1 DM
    between the current user and one other user therefore has the member
    set ``{self, other}`` -- not ``{other}``. Sending only the other user's
    id is what the server rejects with a generic ``INVALID_ARGUMENT``
    ("root-error"), because that set does not describe any DM that can be
    found or created.

    To build the correct set this service needs the caller's own user id.
    It is supplied lazily via ``self_id_getter`` (the client passes
    ``lambda: self.user_id``) so the service keeps working even though the
    id only becomes known after login.
    """

    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter: Callable[[], str],
        self_id_getter: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self._self_id_getter = self_id_getter
        self._by_user_id: Dict[str, DirectMessage] = {}
        self._by_id: Dict[str, DirectMessage] = {}

    # ------------------------------------------------------------------ #
    # membership
    # ------------------------------------------------------------------ #
    def _self_id(self) -> Optional[str]:
        if self._self_id_getter is None:
            return None
        try:
            raw = self._self_id_getter()
        except Exception:  # pragma: no cover - defensive
            return None
        if not raw:
            return None
        return normalize_root_guid(raw)

    def _member_set(self, user_id: str) -> List[str]:
        """Return the full DM member set for a 1:1 DM with ``user_id``.

        The caller's own id is included first. Duplicates are removed while
        order is preserved (so opening a DM "with yourself" degrades to a
        single-member set rather than a rejected ``{self, self}``).
        """
        other = normalize_root_guid(user_id)
        self_id = self._self_id()

        if self_id is None:
            raise DirectMessageError(
                "Cannot resolve the current user's id for the DM member "
                "set. Call client.login(...) before opening a DM, or "
                "construct DirectMessageService with a self_id_getter."
            )

        ordered = [self_id, other]
        seen: set[str] = set()
        members: List[str] = []
        for member in ordered:
            if member not in seen:
                seen.add(member)
                members.append(member)
        return members

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "grpc-timeout": "30S",
            "authorization": f"Bearer {self._token_getter()}",
            "content-type": "application/grpc-web",
            "accept": "application/grpc-web",
        }

    async def _unary(
        self,
        endpoint: str,
        payload: bytes,
        operation: str,
    ):
        return await self.transport.unary(
            endpoint=endpoint,
            body=grpc_frame(payload),
            headers=self._headers(),
            operation=operation,
        )

    @staticmethod
    def _explain_invalid_argument(operation: str, members: List[str]) -> str:
        return (
            f"{operation} was rejected by Root (INVALID_ARGUMENT). The "
            f"request was well-formed and used member set {members}. The "
            "usual remaining causes are: (a) the target user id is not a "
            "real user, (b) the target restricts who may DM them "
            "(friend/invite requirement), or (c) you are blocked. Verify "
            "the id via client.high.user.find_by_username(...) and try a "
            "target you control with open DM settings to isolate the gate."
        )

    async def _dm_unary(
        self,
        endpoint: str,
        payload: bytes,
        operation: str,
        members: List[str],
    ):
        """``_unary`` with DM-aware error translation and logging."""
        log.debug(
            "%s endpoint=%s members=%s bytes=%d",
            operation,
            endpoint,
            members,
            len(payload),
        )
        try:
            return await self._unary(endpoint, payload, operation)
        except GrpcWebError as exc:
            if exc.status == GrpcStatus.INVALID_ARGUMENT:
                message = self._explain_invalid_argument(operation, members)
                log.warning("%s", message)
                raise DirectMessageError(message) from exc
            # NOT_FOUND on a Find is normal control flow ("no DM yet"); the
            # caller turns it into None. Keep it at debug so it doesn't look
            # like a failure. Everything else is a real problem -> warning.
            level = (
                logging.DEBUG
                if exc.status == GrpcStatus.NOT_FOUND
                else logging.WARNING
            )
            log.log(
                level,
                "%s failed: %s (%s)",
                operation,
                exc.status.name,
                exc.grpc_message,
            )
            raise

    # ------------------------------------------------------------------ #
    # request builders
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_create_request(member_ids: List[str]) -> tuple[bytes, str]:
        high64, low64 = create_command_idempotency_guid()
        command_id = format_root_guid(high64, low64)
        context = length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )
        payload = bytearray()
        payload += length_field(1, context)
        # DirectMessageCreateRequest.MemberUserIds = repeated field 10.
        for member_id in member_ids:
            payload += length_field(10, encode_root_guid(member_id))
        return bytes(payload), command_id

    @staticmethod
    def _build_find_request(member_ids: List[str]) -> bytes:
        # DirectMessageFindRequest.MemberUserIds = repeated field 10.
        payload = bytearray()
        for member_id in member_ids:
            payload += length_field(10, encode_root_guid(member_id))
        return bytes(payload)

    # ------------------------------------------------------------------ #
    # response parsers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _message_frame(body: bytes) -> Optional[bytes]:
        for flag, frame in iter_grpc_web_frames(body):
            if not (flag & 0x80):
                return frame
        return None

    @classmethod
    def _parse_create_response(
        cls,
        body: bytes,
        command_id: str,
    ) -> DirectMessage:
        frame = cls._message_frame(body)
        if frame is None:
            raise DirectMessageError(
                "DirectMessageCreate returned no protobuf message"
            )

        creator_user_id = None
        direct_message_id = None
        member_user_ids = []

        for number, wire_type, value in iter_fields(frame):
            if wire_type != 2:
                continue
            if number == 4:
                creator_user_id = decode_root_guid_message(value)
            elif number == 5:
                direct_message_id = decode_root_guid_message(value)
            elif number == 6:
                member = decode_root_guid_message(value)
                if member:
                    member_user_ids.append(member)

        if direct_message_id is None:
            raise DirectMessageError(
                "DirectMessageCreate returned no direct-message ID"
            )

        return DirectMessage(
            id=direct_message_id,
            creator_user_id=creator_user_id,
            member_user_ids=tuple(member_user_ids),
            command_id=command_id,
            raw=frame,
        )

    @classmethod
    def _parse_find_response(
        cls,
        body: bytes,
    ) -> Optional[DirectMessage]:
        frame = cls._message_frame(body)
        if frame is None:
            return None

        direct_message_id = None
        creator_user_id = None
        member_user_ids = []

        for number, wire_type, value in iter_fields(frame):
            if wire_type != 2:
                continue
            if number == 10:
                direct_message_id = decode_root_guid_message(value)
            elif number == 12:
                creator_user_id = decode_root_guid_message(value)
            elif number == 13:
                member = decode_root_guid_message(value)
                if member:
                    member_user_ids.append(member)

        # Find can legitimately return an empty response when no DM exists.
        if direct_message_id is None:
            return None

        return DirectMessage(
            id=direct_message_id,
            creator_user_id=creator_user_id,
            member_user_ids=tuple(member_user_ids),
            raw=frame,
        )

    @classmethod
    def _parse_list_response(
        cls,
        body: bytes,
    ) -> tuple[DirectMessage, ...]:
        frame = cls._message_frame(body)
        if frame is None:
            return ()

        direct_messages = []
        for number, wire_type, value in iter_fields(frame):
            if number != 10 or wire_type != 2:
                continue

            direct_message_id = None
            creator_user_id = None
            member_user_ids = []
            for inner_number, inner_wire, inner_value in iter_fields(value):
                if inner_wire != 2:
                    continue
                # DirectMessageResponse follows the same stable ID/member
                # numbering used by FindResponse.
                if inner_number == 10:
                    direct_message_id = decode_root_guid_message(inner_value)
                elif inner_number == 12:
                    creator_user_id = decode_root_guid_message(inner_value)
                elif inner_number == 13:
                    member = decode_root_guid_message(inner_value)
                    if member:
                        member_user_ids.append(member)

            if direct_message_id:
                direct_messages.append(
                    DirectMessage(
                        id=direct_message_id,
                        creator_user_id=creator_user_id,
                        member_user_ids=tuple(member_user_ids),
                        raw=value,
                    )
                )

        return tuple(direct_messages)

    # ------------------------------------------------------------------ #
    # cache
    # ------------------------------------------------------------------ #
    def _cache(self, direct_message: DirectMessage) -> DirectMessage:
        self._by_id[direct_message.id] = direct_message
        for member_id in direct_message.member_user_ids:
            self._by_user_id[member_id] = direct_message
        # Also key by the "other" participant so a 1:1 DM created with the
        # full {self, other} set is retrievable by the other id alone, even
        # when the server echoes the members in a different order.
        self_id = self._self_id()
        for member_id in direct_message.member_user_ids:
            if member_id != self_id:
                self._by_user_id[member_id] = direct_message
        return direct_message

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def list(self) -> tuple[DirectMessage, ...]:
        response = await self._unary(
            DIRECT_MESSAGE_LIST,
            b"",
            "DirectMessageList",
        )
        result = self._parse_list_response(response.content)
        for direct_message in result:
            self._cache(direct_message)
        log.debug("DirectMessageList cached %d conversations", len(result))
        return result

    async def find(self, user_id: str) -> Optional[DirectMessage]:
        user_id = normalize_root_guid(user_id)
        cached = self._by_user_id.get(user_id)
        if cached is not None:
            return cached

        members = self._member_set(user_id)
        try:
            response = await self._dm_unary(
                DIRECT_MESSAGE_FIND,
                self._build_find_request(members),
                "DirectMessageFind",
                members,
            )
        except GrpcNotFound:
            # NOT_FOUND from a *Find* is the expected answer when no DM has
            # ever existed between these members. That is not an error -- it
            # is exactly the None case in this method's contract. Returning
            # None lets get_or_create fall through to Create.
            log.debug("No existing DM with %s (server returned NOT_FOUND)", user_id)
            return None
        direct_message = self._parse_find_response(response.content)
        if direct_message is None:
            return None
        self._cache(direct_message)
        self._by_user_id[user_id] = direct_message
        return direct_message

    async def create(self, user_id: str) -> DirectMessage:
        user_id = normalize_root_guid(user_id)
        members = self._member_set(user_id)
        payload, command_id = self._build_create_request(members)
        response = await self._dm_unary(
            DIRECT_MESSAGE_CREATE,
            payload,
            "DirectMessageCreate",
            members,
        )
        direct_message = self._parse_create_response(
            response.content,
            command_id,
        )
        self._cache(direct_message)
        self._by_user_id[user_id] = direct_message
        return direct_message

    async def get_or_create(self, user) -> DirectMessage:
        user_id = normalize_root_guid(
            user.id if hasattr(user, "id") else str(user)
        )
        cached = self._by_user_id.get(user_id)
        if cached is not None:
            return cached

        # Find is an optimisation: it avoids issuing a Create for a DM that
        # already exists. Create is itself idempotent (Root returns the
        # existing DM), so a failing Find must never be fatal here -- we log
        # it and fall through to Create. NOT_FOUND is the normal "no DM yet"
        # answer; DirectMessageError covers a wrapped INVALID_ARGUMENT.
        try:
            existing = await self.find(user_id)
        except (DirectMessageError, GrpcNotFound) as exc:
            log.debug(
                "Find failed for %s, falling back to Create: %s",
                user_id,
                exc,
            )
            existing = None

        if existing is not None:
            return existing

        return await self.create(user_id)
