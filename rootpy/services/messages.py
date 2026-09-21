from __future__ import annotations

import logging
import time as _time

import asyncio
from typing import Callable, List, Optional, Tuple

from ..emoji import normalize_reaction
from ..exceptions import GrpcWebError
from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    format_root_guid,
    normalize_root_guid,
    root_guid_datetime,
)
from ..models import Message, MessageSendResult
from ..protocol import (
    bool_field,
    field_key,
    encode_varint,
    decode_root_guid_message,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    unwrap_grpc_web,
    length_field,
)
from ..transport import GrpcWebTransport

log = logging.getLogger("rootpy.messages")

#: How many messages one message may reply to. Root rejects more.
MAX_REPLY_TARGETS = 5


def _oldest_timestamp(messages) -> Optional[float]:
    """The unix time of the oldest message in a page, from its id.

    Root ids are timestamp GUIDs, so a page's lower boundary is exact and does
    not have to be estimated. Ids that will not parse are skipped rather than
    raising -- a single odd row should not stop a history walk.
    """
    stamps = []
    for message in messages:
        identifier = getattr(message, "id", None)
        if not identifier:
            continue
        try:
            stamps.append(root_guid_datetime(identifier).timestamp())
        except Exception:
            continue
    return min(stamps) if stamps else None


def normalise_reply_targets(targets) -> List[str]:
    """Ids for the messages being replied to.

    Accepts message ids, :class:`Message` objects, or a mix -- and a single
    one rather than a list, since replying to one thing is the common case.
    Duplicates are dropped (Root counts them twice otherwise).

    Raises ``ValueError`` above :data:`MAX_REPLY_TARGETS`, because the server
    rejects the whole send and the error doesn't say why.
    """
    if targets is None:
        return []
    if isinstance(targets, (str, bytes)) or hasattr(targets, "id"):
        targets = [targets]

    ids: List[str] = []
    for target in targets:
        raw = getattr(target, "id", target)
        if not raw:
            continue
        normalized = normalize_root_guid(raw)
        if normalized not in ids:
            ids.append(normalized)

    if len(ids) > MAX_REPLY_TARGETS:
        raise ValueError(
            f"a message can reply to at most {MAX_REPLY_TARGETS} messages; "
            f"got {len(ids)}"
        )
    return ids

MESSAGE_CREATE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/Create"
)
MESSAGE_LIST = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/List"
)
MESSAGE_PIN_LIST = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/PinList"
)
MESSAGE_GET = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/Get"
)
MESSAGE_SET_VIEW_TIME = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/SetViewTime"
)
MESSAGE_DELETE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/Delete"
)
MESSAGE_EDIT = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/Edit"
)
MESSAGE_PIN_CREATE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/PinCreate"
)
MESSAGE_PIN_DELETE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/PinDelete"
)
MESSAGE_REACTION_CREATE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/ReactionCreate"
)
MESSAGE_REACTION_DELETE = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/ReactionDelete"
)
MESSAGE_FLAG = (
    "https://api.rootapp.com/root.v2.MessageGrpcService/Flag"
)


class MessageService:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter: Callable[[], str],
        sent_message_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self._sent_message_callback = sent_message_callback

        self._reaction_lock = asyncio.Lock()


    def _headers(self) -> dict:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "authorization": f"Bearer {self._token_getter()}",
            "content-type": "application/grpc-web",
            "accept": "application/grpc-web",
        }

    @staticmethod
    def _command_context() -> bytes:
        high64, low64 = create_command_idempotency_guid()
        return length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )

    @classmethod
    def _message_target_payload(
        cls,
        *,
        container_id: str,
        community_id: Optional[str],
        message_id: str,
        message_id_field: int = 12,
    ) -> bytearray:
        payload = bytearray()
        payload += length_field(1, cls._command_context())
        payload += length_field(
            10,
            encode_root_guid(normalize_root_guid(container_id)),
        )
        if community_id is not None:
            payload += length_field(
                11,
                encode_root_guid(normalize_root_guid(community_id)),
            )
        payload += length_field(
            message_id_field,
            encode_root_guid(normalize_root_guid(message_id)),
        )
        return payload

    async def _message_action(
        self,
        *,
        endpoint: str,
        operation: str,
        payload: bytes,
    ) -> None:
        await self.transport.unary(
            endpoint=endpoint,
            body=grpc_frame(payload),
            headers=self._headers(),
            operation=operation,
        )

    async def delete_message(self, message: Message) -> None:
        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        await self._message_action(
            endpoint=MESSAGE_DELETE,
            operation="MessageDelete",
            payload=bytes(payload),
        )

    async def _delete_sent_later(
        self,
        delay: float,
        message: Message,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self.delete_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:

            pass

    async def edit_message(
        self,
        message: Message,
        content: str,
        *,
        uris: Optional[List[str]] = None,
    ) -> Message:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if not content.rstrip():
            raise ValueError("Message content cannot be empty")

        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        payload += length_field(13, content.encode("utf-8"))
        for uri in uris or []:
            payload += length_field(14, str(uri).encode("utf-8"))

        await self._message_action(
            endpoint=MESSAGE_EDIT,
            operation="MessageEdit",
            payload=bytes(payload),
        )

        from dataclasses import replace
        return replace(message, content=content)

    async def pin_message(self, message: Message) -> None:
        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        await self._message_action(
            endpoint=MESSAGE_PIN_CREATE,
            operation="MessagePinCreate",
            payload=bytes(payload),
        )

    async def unpin_message(self, message: Message) -> None:

        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        await self._message_action(
            endpoint=MESSAGE_PIN_DELETE,
            operation="MessagePinDelete",
            payload=bytes(payload),
        )

    async def _reaction_action(
        self,
        *,
        endpoint: str,
        operation: str,
        payload: bytes,
    ) -> None:

        delays = (0.0, 0.15, 0.35, 0.75, 1.25)

        async with self._reaction_lock:
            last_error = None

            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)

                try:
                    await self._message_action(
                        endpoint=endpoint,
                        operation=operation,
                        payload=payload,
                    )
                    return
                except GrpcWebError as exc:
                    last_error = exc

                    if str(exc.status) not in {
                        "8",
                        "14",
                        "429",
                        "503",
                    }:
                        raise

            if last_error is not None:
                raise last_error

    async def add_reaction(
        self,
        message: Message,
        reaction: str,
    ) -> None:
        shortcode = normalize_reaction(reaction)
        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        payload += length_field(
            13,
            shortcode.encode("utf-8"),
        )
        await self._reaction_action(
            endpoint=MESSAGE_REACTION_CREATE,
            operation="MessageReactionCreate",
            payload=bytes(payload),
        )

    async def remove_reaction(
        self,
        message: Message,
        reaction: str,
    ) -> None:
        shortcode = normalize_reaction(reaction)

        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        payload += length_field(
            13,
            shortcode.encode("utf-8"),
        )
        await self._reaction_action(
            endpoint=MESSAGE_REACTION_DELETE,
            operation="MessageReactionDelete",
            payload=bytes(payload),
        )

    # --- history (MessageGrpcService/List) -------------------------------- #
    _DIRECTIONS = {"unspecified": 0, "newer": 1, "older": 2, "both": 3}

    #: MessageList's Limit(15) window, straight from Root's own validator
    #: message: "Limit: Must be between 10 and 50 [InclusiveBetweenValidator]".
    #: 9 and 51 are both refused; 10 and 50 both work.
    LIMIT_MIN = 10
    LIMIT_MAX = 50

    async def list(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        direction: str = "both",
        after: Optional[float] = None,
        limit: Optional[int] = None,
        include_deleted: bool = False,
    ) -> List[Message]:
        """Fetch message history for a channel or DM.

        The request carries ContainerId(10), CommunityId(11),
        MessageDirectionTake(12) and DateAt(13). Limit(15) is optional and
        omitted by default, which returns the server's own page size of 50.

        direction:
            ``"both"``  (default) messages around ``after``
            ``"newer"`` only messages after ``after`` -- use this to poll
            ``"older"`` history going backwards from ``after``
        after:
            cursor as epoch seconds; defaults to now.
        limit:
            omitted unless you ask for it. When given it must be **between 10
            and 50 inclusive** -- Root applies an InclusiveBetweenValidator and
            answers INVALID_ARGUMENT otherwise ("Limit: Must be between 10 and
            50"). Anything below 10 or above 50 is refused; 10-50 are accepted
            and return exactly that many. Omitting it returns 50. Checked here
            so a bad value costs nothing rather than failing
            on every page of a walk.

        Returns :class:`Message` objects, oldest-first.
        """
        if limit is not None and not (
            self.LIMIT_MIN <= int(limit) <= self.LIMIT_MAX
        ):
            raise ValueError(
                f"limit must be between {self.LIMIT_MIN} and {self.LIMIT_MAX} "
                f"inclusive (got {limit}); Root rejects anything else with "
                "INVALID_ARGUMENT. Pass limit=None to omit it, which returns "
                f"{self.LIMIT_MAX}."
            )
        container_id = normalize_root_guid(container_id)
        community = normalize_root_guid(community_id) if community_id else None
        direction_value = self._DIRECTIONS.get(direction.lower())
        if direction_value is None:
            raise ValueError(
                f"direction must be one of {sorted(self._DIRECTIONS)}"
            )
        cursor = after if after is not None else _time.time()

        body = bytearray()
        body += length_field(10, encode_root_guid(container_id))
        if community is not None:
            body += length_field(11, encode_root_guid(community))
        body += field_key(12, 0) + encode_varint(direction_value)

        seconds = int(cursor)
        nanos = int(round((cursor - seconds) * 1_000_000_000))
        timestamp = field_key(1, 0) + encode_varint(seconds)
        if nanos:
            timestamp += field_key(2, 0) + encode_varint(nanos)
        body += length_field(13, bytes(timestamp))

        if limit is not None:
            body += length_field(
                15, field_key(1, 0) + encode_varint(int(limit))
            )

        response = await self.transport.unary(
            endpoint=MESSAGE_LIST,
            body=bytes(body),
            headers=self._headers(),
            operation="MessageList",
        )
        messages = self._parse_message_list(response.content)
        if include_deleted:
            return messages
        # Root returns deleted messages as tombstones with DeletedAt(7) set
        # instead of omitting them, so "list the history" used to include
        # things the user had already deleted. Filtered by default; pass
        # include_deleted=True to see the tombstones (they carry
        # ``deleted_at`` and ``is_deleted``).
        return type(messages)(m for m in messages if not m.is_deleted)

    async def history(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        limit: Optional[int] = 200,
        before: Optional[float] = None,
        page_size: int = 50,
    ):
        """Walk a container's history backwards, yielding messages one by one.

            async for message in client.messages.history(channel_id, limit=500):
                print(message.content)

        Pages the ``DateAt`` cursor internally (OLDER from ``before``, or from
        now), de-duplicates across page boundaries, and stops when the server
        runs out or ``limit`` is reached. Yields newest-first.

        limit: total messages to yield; None means "keep going until the
            server stops returning new ones".
        page_size: how many to request per round-trip. Goes out as
            MessageList's Limit, so it must be between 10 and 50 inclusive --
            see :meth:`list`. Checked once here rather than discovered on the
            first page, because every page would fail the same way.
        """
        if not (self.LIMIT_MIN <= int(page_size) <= self.LIMIT_MAX):
            raise ValueError(
                f"page_size must be between {self.LIMIT_MIN} and "
                f"{self.LIMIT_MAX} inclusive (got {page_size}); it is sent as "
                "MessageList's Limit, which Root range-checks. Use limit= to "
                "cap the total yielded instead."
            )

        seen: set = set()
        cursor = before if before is not None else _time.time()
        produced = 0

        while limit is None or produced < limit:
            batch = await self.list(
                container_id,
                community_id=community_id,
                direction="older",
                after=cursor,
                limit=page_size,
            )
            if not batch:
                return

            # list() returns oldest-first; history walks backwards.
            fresh = [m for m in reversed(batch) if m.id and m.id not in seen]
            if not fresh:
                return   # the server repeated a page -- we're at the end

            short_page = len(batch) < page_size

            for message in fresh:
                seen.add(message.id)
                yield message
                produced += 1
                if limit is not None and produced >= limit:
                    return

            # A page shorter than we asked for means there's nothing older --
            # stop here rather than spending another round-trip to find that
            # out. (The duplicate-page check above stays as a backstop for
            # servers that pad the last page.)
            if short_page:
                return

            # Step the cursor to just before this page's oldest message.
            #
            # Step by the page's own oldest timestamp, not by a guessed
            # interval. A fixed step in seconds puts the next cursor back
            # inside the page just read whenever a page spans more time than
            # the step: the server returns the same rows, `fresh` comes back
            # empty, and the walk stops believing it has reached the end.
            #
            # Root ids are timestamp GUIDs, so the exact boundary is available
            # and there is no need to guess an interval.
            oldest = _oldest_timestamp(batch)
            if oldest is not None and oldest < cursor:
                cursor = oldest - 0.001
            else:
                # Unparseable ids, or a page that did not move the boundary.
                # Fall back to a fixed step so the walk still terminates
                # rather than spinning on the same cursor forever.
                cursor = cursor - max(1.0, float(page_size))

    async def pin_list(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
    ) -> List[Message]:
        """List pinned messages in a container.

        Takes only ContainerId(10) + CommunityId(11) -- the same identifiers
        MessageList uses, which makes it a handy control when diagnosing a
        failing List call.
        """
        body = bytearray()
        body += length_field(10, encode_root_guid(normalize_root_guid(container_id)))
        if community_id:
            body += length_field(
                11, encode_root_guid(normalize_root_guid(community_id))
            )
        response = await self.transport.unary(
            endpoint=MESSAGE_PIN_LIST,
            body=bytes(body),
            headers=self._headers(),
            operation="MessagePinList",
        )
        return self._parse_message_list(response.content)

    def _parse_message_list(self, body: bytes) -> List[Message]:
        """MessageListResponse(field 5) -> MessageContainerResponse
        -> Messages(field 11, repeated MessagePacket) -> [Message], oldest-first.

        The response is grpc-web framed: one or more length-prefixed frames,
        where the frame with the 0x80 flag is the trailer. Unwrap the data
        frame before parsing (parsing the raw body yields "unsupported wire
        type" errors on the frame header).
        """
        from ..packet_schemas import decode_packet

        payload = unwrap_grpc_web(body)
        if payload is None:
            return []

        container = None
        for n, wt, value in iter_fields(payload):
            if n == 5 and wt == 2:
                container = value
                break
        if container is None:
            return []

        messages: List[Message] = []
        for n, wt, value in iter_fields(container):
            if n == 11 and wt == 2:
                f = decode_packet("MESSAGE", bytes(value))
                # Carry every field the packet actually decodes. This used to
                # keep only id/container/user/content/community and drop the
                # rest, so `deleted_at` was always None -- which made
                # `Message.is_deleted` permanently False and let deleted
                # messages come back from history looking live. `edited_at`
                # and `pinned_at` were lost the same way.
                messages.append(
                    Message(
                        id=f.get("id") or "",
                        container_id=f.get("container_id") or "",
                        user_id=f.get("user_id") or "",
                        content=f.get("message_content") or "",
                        community_id=f.get("community_id"),
                        deleted_at=f.get("deleted_at"),
                        edited_at=f.get("edited_at"),
                        pinned_at=f.get("pinned_at"),
                        payload_raw=f.get("payload"),
                        _service=self,
                    )
                )
        messages.reverse()  # API returns newest-first; present oldest-first
        return messages

    async def set_view_time(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
    ) -> None:
        """Mark a container viewed up to now (clears its unread state).

        MessageSetViewTimeRequest: context(1), container(10), community(11).
        The server stamps the view time itself.
        """
        body = bytearray()
        body += length_field(1, self._command_context())
        body += length_field(10, encode_root_guid(normalize_root_guid(container_id)))
        if community_id is not None:
            body += length_field(
                11, encode_root_guid(normalize_root_guid(community_id))
            )
        await self.transport.unary(
            endpoint=MESSAGE_SET_VIEW_TIME,
            body=grpc_frame(bytes(body)),
            headers=self._headers(),
            operation="MessageSetViewTime",
        )

    async def flag_message(
        self,
        message: Message,
        reason,
    ) -> None:
        # Accept a ContentFlagReason enum or a raw int.
        reason = int(reason)
        if reason < 0:
            raise ValueError("reason cannot be negative")

        payload = self._message_target_payload(
            container_id=message.container_id,
            community_id=message.community_id,
            message_id=message.id,
        )
        if reason != 0:
            payload += field_key(13, 0) + encode_varint(reason)

        await self._message_action(
            endpoint=MESSAGE_FLAG,
            operation="MessageFlag",
            payload=bytes(payload),
        )

    @staticmethod
    def _string_wrapper(value: str) -> bytes:
        return length_field(1, value.encode("utf-8"))

    @classmethod
    def _build_request(
        cls,
        *,
        container_id: str,
        community_id: Optional[str],
        content: str,
        attachment_token_uris: List[str],
        parent_message_ids: List[str],
        needs_parent_notification: bool,
    ) -> Tuple[bytes, str]:
        high64, low64 = create_command_idempotency_guid()
        command_id = format_root_guid(high64, low64)

        context = length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )

        payload = bytearray()
        payload += length_field(1, context)
        payload += length_field(10, encode_root_guid(container_id))

        if community_id is not None:
            payload += length_field(
                11,
                encode_root_guid(community_id),
            )

        payload += length_field(
            12,
            cls._string_wrapper(content.rstrip()),
        )

        for token_uri in attachment_token_uris:
            payload += length_field(
                13,
                token_uri.encode("utf-8"),
            )

        for parent_id in parent_message_ids:
            payload += length_field(
                14,
                encode_root_guid(parent_id),
            )

        if needs_parent_notification:
            payload += bool_field(15, True)

        return grpc_frame(bytes(payload)), command_id

    @staticmethod
    def _parse_response(body: bytes) -> Optional[str]:
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 10 and wire_type == 2:
                    return decode_root_guid_message(value)
        return None

    async def send(
        self,
        container_id: str,
        content: str,
        *,
        community_id: Optional[str] = None,
        attachment_token_uris: Optional[List[str]] = None,
        parent_message_ids: Optional[List[str]] = None,
        needs_parent_notification: bool = False,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        """Send a message.

        ``parent_message_ids`` makes it a reply. Root allows replying to up to
        :data:`MAX_REPLY_TARGETS` messages at once -- pass several ids to
        reply to several messages with one message.
        """
        container_id = normalize_root_guid(container_id)
        if community_id is not None:
            community_id = normalize_root_guid(community_id)

        normalized_parents = normalise_reply_targets(parent_message_ids)

        if not content.rstrip():
            raise ValueError("Message content cannot be empty")

        body, command_id = self._build_request(
            container_id=container_id,
            community_id=community_id,
            content=content,
            attachment_token_uris=attachment_token_uris or [],
            parent_message_ids=normalized_parents,
            needs_parent_notification=needs_parent_notification,
        )

        response = await self.transport.unary(
            endpoint=MESSAGE_CREATE,
            body=body,
            headers=self._headers(),
            operation="MessageCreate",
        )

        message_id = self._parse_response(response.content)

        if (
            message_id is not None
            and self._sent_message_callback is not None
        ):
            self._sent_message_callback(message_id)

        result = MessageSendResult(
            id=message_id,
            container_id=container_id,
            community_id=community_id,
            content=content.rstrip(),
            command_id=command_id,
        )

        if delete_after is not None and message_id is not None:
            transient = Message(
                id=message_id,
                container_id=container_id,
                user_id="",
                content=content.rstrip(),
                community_id=community_id,
                _service=self,
            )
            asyncio.create_task(
                self._delete_sent_later(
                    delete_after,
                    transient,
                )
            )

        return result
