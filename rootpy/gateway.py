from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, Optional

# websockets is only needed when a gateway is actually opened. Scripts that
# just make API calls (the majority) shouldn't pay for importing it, so it's
# imported on first connect -- see _import_websockets().
websockets = None


def _import_websockets():
    global websockets
    if websockets is None:
        import websockets as _ws

        websockets = _ws
    return websockets

from .events import (
    CallDetachedEvent,
    ChannelDeletedEvent,
    ChannelEvent,
    CommunityDeletedEvent,
    CommunityEvent,
    CommunityLeaveEvent,
    MessageEvent,
    MessageAction,
    ChannelAction,
    NotificationEvent,
)
from .enums import PacketErrorCode
from .models import Channel, Community, Message, MessageAttachment
from .packets import PacketType, SocketPacket
from .packet_schemas import decode_packet
from .protocol import (
    decode_root_guid_message,
    decode_timestamp_message,
    encode_varint,
    field_key,
    iter_fields,
    length_field,
)

PACKET_FIELD_CHANNEL_CREATED = 30
PACKET_FIELD_CHANNEL_EDITED = 31
PACKET_FIELD_CHANNEL_DELETED = 33
PACKET_FIELD_COMMUNITY = 50
PACKET_FIELD_COMMUNITY_LEAVE = 52
PACKET_FIELD_COMMUNITY_DELETED = 53
PACKET_FIELD_MESSAGE = 170
PACKET_FIELD_WEBRTC_USER_DETACH = 201


log = logging.getLogger("rootpy.gateway")


class Gateway:
    def __init__(
        self,
        *,
        hub_url: str,
        token: str,
        device_id: str,
        dispatch: Callable[[str, object], Awaitable[None]],
        auto_reconnect: bool = True,
        reconnect_base: float = 1.0,
        reconnect_max: float = 30.0,
        max_reconnect_attempts: Optional[int] = None,
        contention_pause: float = 0.0,
        contention_threshold: int = 3,
    ) -> None:
        self.hub_url = hub_url
        self.token = token
        self.device_id = device_id
        self.dispatch = dispatch
        self.auto_reconnect = bool(auto_reconnect)
        self.reconnect_base = max(0.0, float(reconnect_base))
        self.reconnect_max = max(self.reconnect_base, float(reconnect_max))
        self.max_reconnect_attempts = max_reconnect_attempts
        # A run of 4016 closes usually means a *second* client is live on this
        # account: both sides advance the hub's sequence cursor, so whichever
        # one resumes second is always out of range. Reconnecting harder makes
        # that worse -- each attempt is another cursor fight -- and the normal
        # backoff caps out at reconnect_max, so it settles into a permanent
        # once-a-minute retry that never recovers on its own.
        #
        # Standing off for a few minutes lets the other client finish. Off by
        # default (0.0) because it is a policy an application picks, not
        # something a library should impose on a caller that would rather see
        # the errors.
        self.contention_pause = max(0.0, float(contention_pause))
        self.contention_threshold = max(1, int(contention_threshold))
        self._contention_streak = 0
        self.current_sequence: Optional[int] = None
        self.last_non_ping_sequence: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        # Set once the run loop terminates for good (clean stop or giving up).
        # start() waits on this alongside _ready so a failed first connect
        # can never leave the caller awaiting _ready forever.
        self._stopped = asyncio.Event()
        self._stopping = False
        self._connect_error: Optional[BaseException] = None
        self._just_connected = False
        self._got_data = False
        # Delay between resync cycles when the last connection delivered data.
        # 0 means reconnect immediately (the hub closes each batch with 1000).
        self.resync_delay = 0.0
        self._last_close_code = None
        self._last_close_reason = None
        # Heartbeat cadence. Lead with a fast first ping so each (re)connect
        # doesn't race the hub's idle timeout; then ping well inside the window.
        self.ping_first_delay = 3.0
        self.ping_interval_min = 6.0
        self.ping_interval_max = 8.0
        # Force a reconnect after this many seconds so the hub delivers a fresh
        # resync batch (new messages arrive in batches, not pushed to an idle
        # open socket). 0 disables. This is what makes channel messages flow.
        self.resync_interval = 4.0
        # Optional StateCache -- set by the client so decoded
        # notifications can teach it usernames. None is fine.
        self.client_cache = None
        # Optional SOCKS/HTTP proxy for the websocket, matching the
        # transport's. None means a direct connection.
        self.proxy = None
        # Build the two exploratory protobuf-to-JSON trees that
        # ``_socket_response`` puts on ``data["notification"]`` and
        # ``data["packet_container"]``. Off by default because they are a
        # debugging aid that nothing reads: no library code, test, devscript
        # or example in this repo touches either key, and the decoded
        # ``fields`` (plus ``raw_hex``/``packet_raw_hex``) carry the same
        # information in the form callers actually use.
        #
        # They are not cheap. ``_bytes_to_json`` speculatively hexes and
        # UTF-8-decodes every length-delimited field and recurses six levels
        # deep, and ``packet_container``'s tree is a strict *subtree* of
        # ``notification``'s, recomputed from scratch. Measured on a 556-byte
        # MessageCreate frame: 107.7 us of the 169.1 us this function costs --
        # 64% -- and it was paid on **every** frame, keepalive pings included,
        # on the event loop.
        #
        # Set True when you are reverse-engineering an unfamiliar packet:
        #     client.gateway.decode_debug_trees = True
        self.decode_debug_trees = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        # Return as soon as we're connected, OR the loop has permanently
        # stopped (e.g. the very first connection failed and we're not
        # reconnecting). Without the _stopped arm this could deadlock.
        ready = asyncio.ensure_future(self._ready.wait())
        stopped = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait(
                {ready, stopped},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            ready.cancel()
            stopped.cancel()
        if not self._ready.is_set() and self._stopped.is_set():
            if self._connect_error is not None:
                raise self._connect_error
            raise ConnectionError("Gateway stopped before it became ready")

    async def close(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._closed.set()
        self._stopped.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    #: Close codes that mean "your resume cursor is not valid", which is what
    #: a second client on the same account looks like from here.
    CONTENTION_CLOSE_CODES = (4016,)

    def _is_contention(self, exc: BaseException) -> bool:
        """Was this drop a sequence-cursor fight rather than a network fault?

        Prefers the close code the server actually sent. Falls back to the
        exception text because ``websockets`` only populates ``close_code`` on
        the connection object once the closing handshake completes, and a
        half-closed socket can raise with it still unset -- in which case the
        code is still in the message ("received 4016 (private use) Sequence out
        of range").
        """
        if self._last_close_code in self.CONTENTION_CLOSE_CODES:
            return True
        text = str(exc)
        return any(str(code) in text for code in self.CONTENTION_CLOSE_CODES)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at reconnect_max."""
        ceiling = min(
            self.reconnect_max,
            self.reconnect_base * (2 ** max(0, attempt - 1)),
        )
        return random.uniform(0.0, ceiling) if ceiling > 0 else 0.0

    async def _run(self) -> None:
        """Connect, and (when enabled) transparently reconnect on drops.

        A clean stop via :meth:`close` ends the loop. Any other disconnect --
        network error, server restart, idle close -- triggers a backoff and a
        fresh connection. The sequence counters are preserved across attempts.
        """
        attempt = 0
        try:
            while not self._stopping:
                try:
                    await self._run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # connection/protocol failure
                    self._connect_error = exc
                    log.warning(
                        "gateway connection error: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    healthy = False
                    if self._is_contention(exc):
                        self._contention_streak += 1
                    else:
                        self._contention_streak = 0
                else:
                    # Clean end of the message stream. This hub is a resync-on-
                    # connect design: it delivers a batch of packets and then
                    # closes normally (code 1000), expecting the client to
                    # reconnect for the next batch. A connection that received
                    # data is "healthy" and should cycle immediately; an empty
                    # one means we're caught up (or failing), so we back off.
                    self._connect_error = None
                    healthy = self._got_data
                    # Any clean end of stream -- with data or without -- proves
                    # the cursor was accepted, so it is not contention. Only
                    # resetting on `healthy` treated an ordinary caught-up
                    # cycle as if it were another failure, and a flaky link
                    # could accumulate its way to a pause it had not earned.
                    self._contention_streak = 0

                if self._stopping or not self.auto_reconnect:
                    break

                self._ready.clear()

                # Stand off rather than keep fighting for the cursor. The
                # threshold matters: a single 4016 is ordinary -- the cursor
                # went stale over a quiet period, _run_once has already dropped
                # it, and the next attempt succeeds. Only a *run* of them means
                # something else is advancing the cursor as fast as we reset it.
                if (
                    self.contention_pause > 0
                    and self._contention_streak >= self.contention_threshold
                ):
                    log.warning(
                        "gateway sequence contention (%d closes in a row); "
                        "pausing %.0fs -- this usually means another client is "
                        "live on this account",
                        self._contention_streak,
                        self.contention_pause,
                    )
                    self._contention_streak = 0
                    attempt = 0
                    await self.dispatch("gateway_paused", self.contention_pause)
                    try:
                        await asyncio.sleep(self.contention_pause)
                    except asyncio.CancelledError:
                        raise
                    log.info("gateway resuming after contention pause")
                    await self.dispatch("gateway_resumed", None)
                    # Resume clean: whatever cursor we held is certainly stale
                    # after five minutes of someone else using the account.
                    self.current_sequence = None
                    self.last_non_ping_sequence = None
                    continue

                if healthy:
                    # Normal resync cycle -- reconnect right away, quietly, and
                    # reset the backoff ladder (matches the reference client's
                    # "if healthy: reset delays" behavior).
                    attempt = 0
                    log.debug(
                        "gateway batch delivered (seq=%s); resyncing",
                        self.current_sequence,
                    )
                    try:
                        await asyncio.sleep(self.resync_delay)
                    except asyncio.CancelledError:
                        raise
                    continue

                # Empty or failed connection. In resync mode we deliberately
                # close idle connections to pull fresh batches, so an empty
                # cycle is expected -- reconnect at a steady cadence rather than
                # backing off (backoff would slow message delivery).
                if self.resync_interval and self.resync_interval > 0 and self._connect_error is None:
                    attempt = 0
                    try:
                        await asyncio.sleep(self.resync_delay)
                    except asyncio.CancelledError:
                        raise
                    continue

                attempt += 1
                if (
                    self.max_reconnect_attempts is not None
                    and attempt > self.max_reconnect_attempts
                ):
                    log.error(
                        "gateway giving up after %d reconnect attempts",
                        attempt - 1,
                    )
                    break

                delay = self._backoff_delay(attempt)
                if self._last_close_code not in (None, 1000):
                    log.info(
                        "gateway reconnecting in %.1fs (attempt %d) "
                        "[last close: code=%s reason=%r]",
                        delay,
                        attempt,
                        self._last_close_code,
                        self._last_close_reason,
                    )
                else:
                    log.debug(
                        "gateway idle; polling again in %.1fs (attempt %d)",
                        delay,
                        attempt,
                    )
                await self.dispatch("gateway_reconnecting", attempt)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
        finally:
            self._closed.set()
            self._stopped.set()

    @staticmethod
    def _bytes_to_json(
        data: bytes,
        *,
        depth: int,
        max_depth: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "hex": data.hex(),
            "length": len(data),
        }

        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None

        if decoded is not None and decoded.isprintable():
            result["text"] = decoded

        if depth >= max_depth or not data:
            return result

        try:
            nested_fields = Gateway._protobuf_fields_to_json(
                data,
                depth=depth + 1,
                max_depth=max_depth,
            )
        except (ValueError, OverflowError):
            nested_fields = []

        if nested_fields:
            result["fields"] = nested_fields

        return result

    @staticmethod
    def _protobuf_fields_to_json(
        data: bytes,
        *,
        depth: int = 0,
        max_depth: int = 6,
    ) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []

        for field_number, wire_type, value in iter_fields(data):
            field: dict[str, Any] = {
                "field": field_number,
                "wire_type": wire_type,
            }

            if wire_type == 0:
                field["value"] = int(value)
            elif wire_type in (1, 5):
                field["hex"] = value.hex()
                field["little_endian"] = int.from_bytes(
                    value,
                    "little",
                )
                field["big_endian"] = int.from_bytes(
                    value,
                    "big",
                )
            elif wire_type == 2:
                field["value"] = Gateway._bytes_to_json(
                    value,
                    depth=depth,
                    max_depth=max_depth,
                )

            fields.append(field)

        return fields

    @classmethod
    def _socket_response(
        cls,
        message: bytes,
        sequence: Optional[int],
        packet_case: Optional[int],
        packet_container: Optional[bytes],
        debug_trees: bool = False,
        error_code: Optional[int] = None,
    ) -> dict[str, Any]:
        packet_payload = None

        if packet_container is not None and packet_case is not None:
            packet_payload = cls._packet_payload(
                packet_container,
                packet_case,
            )

        packet_type_name = (
            PacketType.from_case(packet_case).name
            if packet_case is not None
            else None
        )

        return {
            "sequence": sequence,
            "packet_case": packet_case,
            "error_code": error_code,
            "raw_hex": message.hex(),
            # Both keys always exist; they carry their tree only when asked
            # for. See ``decode_debug_trees`` in __init__ for the measurement.
            "notification": (
                cls._protobuf_fields_to_json(message) if debug_trees else None
            ),
            "packet_container": (
                cls._protobuf_fields_to_json(packet_container)
                if debug_trees and packet_container is not None
                else None
            ),
            "packet": (
                cls._protobuf_fields_to_json(packet_payload)
                if packet_payload is not None
                else None
            ),
            "fields": (
                decode_packet(packet_type_name, packet_payload)
                if packet_type_name is not None
                else {}
            ),
            "packet_raw_hex": (
                packet_payload.hex()
                if packet_payload is not None
                else None
            ),
        }

    @staticmethod
    def _notification_info(
        message: bytes,
    ) -> tuple[Optional[int], Optional[int], Optional[bytes], Optional[int]]:
        """Split a ``ClientNotification`` into its three wire fields.

        The error code is returned last so the long-standing three-value
        unpack keeps working; it is ``None`` on the overwhelming majority of
        frames, because the server only writes field 1 when it is non-zero
        (``InternalWriteTo`` skips ``PACKET_ERROR_CODE_UNSPECIFIED``).
        """
        sequence = None
        packet_case = None
        packet_container = None
        error_code = None
        for n, wt, value in iter_fields(message):
            if n == 1 and wt == 0:
                error_code = PacketErrorCode.coerce(int(value))
            elif n == 2 and wt == 0:
                sequence = int(value)
            elif n == 3 and wt == 2:
                packet_container = value
        if packet_container is not None:
            for n, _, _ in iter_fields(packet_container):
                packet_case = n
                break
        return sequence, packet_case, packet_container, error_code

    @staticmethod
    def _detach_container(packet_container: Optional[bytes]) -> Optional[str]:
        if packet_container is None:
            return None
        for n, wt, value in iter_fields(packet_container):
            if n != PACKET_FIELD_WEBRTC_USER_DETACH or wt != 2:
                continue
            for inner_n, inner_wt, inner_value in iter_fields(value):
                if inner_n == 4 and inner_wt == 2:
                    return decode_root_guid_message(inner_value)
        return None


    @staticmethod
    def _packet_payload(
        packet_container: Optional[bytes],
        field_number: int,
    ) -> Optional[bytes]:
        if packet_container is None:
            return None
        for number, wire_type, value in iter_fields(packet_container):
            if number == field_number and wire_type == 2:
                return value
        return None

    @classmethod
    def _decode_community_packet(
        cls,
        packet_container: Optional[bytes],
    ) -> Optional[Community]:
        packet = cls._packet_payload(
            packet_container,
            PACKET_FIELD_COMMUNITY,
        )
        if packet is None:
            return None

        values = {
            number: (wire_type, value)
            for number, wire_type, value in iter_fields(packet)
        }

        def guid(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        def text(number: int) -> str:
            item = values.get(number)
            if item is None or item[0] != 2:
                return ""
            return item[1].decode("utf-8", errors="replace")

        def wrapped(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            for sub_number, sub_wire, sub_value in iter_fields(item[1]):
                if sub_number == 1 and sub_wire == 2:
                    return sub_value.decode("utf-8", errors="replace")
            return None

        def integer(number: int) -> int:
            item = values.get(number)
            return int(item[1]) if item is not None and item[0] == 0 else 0

        community_id = guid(3)
        if not community_id:
            return None

        return Community(
            id=community_id,
            owner_user_id=guid(4),
            default_channel_id=guid(5),
            name=text(6),
            picture_hex=text(7),
            picture_asset_uri=wrapped(8),
            reject_unverified_email=bool(integer(9)),
            description=wrapped(11),
            is_age_restricted=bool(integer(17)),
            packet_type=integer(1),
            raw=packet,
        )

    @classmethod
    def _decode_channel_packet(
        cls,
        packet_container: Optional[bytes],
        *,
        packet_field: int,
    ) -> Optional[Channel]:
        packet = cls._packet_payload(
            packet_container,
            packet_field,
        )
        if packet is None:
            return None

        values = {
            number: (wire_type, value)
            for number, wire_type, value in iter_fields(packet)
        }

        def guid(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        def text(number: int) -> str:
            item = values.get(number)
            if item is None or item[0] != 2:
                return ""
            return item[1].decode("utf-8", errors="replace")

        def wrapped(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            for sub_number, sub_wire, sub_value in iter_fields(item[1]):
                if sub_number == 1 and sub_wire == 2:
                    return sub_value.decode("utf-8", errors="replace")
            return None

        def integer(number: int) -> int:
            item = values.get(number)
            return int(item[1]) if item is not None and item[0] == 0 else 0

        community_id = guid(3)
        channel_id = guid(4)
        if not community_id or not channel_id:
            return None

        if packet_field == PACKET_FIELD_CHANNEL_CREATED:
            return Channel(
                id=channel_id,
                community_id=community_id,
                channel_group_id=guid(5),
                name=text(6),
                description=wrapped(7),
                icon_asset_uri=wrapped(8),
                before_channel_id=guid(9),
                use_channel_group_permission=bool(integer(10)),
                channel_type=integer(12),
                community_app_id=guid(14),
                packet_type=integer(1),
                raw=packet,
            )

        return Channel(
            id=channel_id,
            community_id=community_id,
            channel_group_id=guid(5),
            name=text(6),
            description=wrapped(7),
            icon_asset_uri=wrapped(8),
            use_channel_group_permission=bool(integer(9)),
            packet_type=integer(1),
            raw=packet,
        )

    @classmethod
    def _decode_channel_deleted_packet(
        cls,
        packet_container: Optional[bytes],
    ) -> Optional[tuple[str, str, Optional[str]]]:
        packet = cls._packet_payload(
            packet_container,
            PACKET_FIELD_CHANNEL_DELETED,
        )
        if packet is None:
            return None

        values = {
            number: (wire_type, value)
            for number, wire_type, value in iter_fields(packet)
        }

        def guid(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        community_id = guid(3)
        channel_id = guid(4)
        if not community_id or not channel_id:
            return None
        return channel_id, community_id, guid(5)

    @classmethod
    def _decode_community_deleted_packet(
        cls,
        packet_container: Optional[bytes],
    ) -> Optional[str]:
        packet = cls._packet_payload(
            packet_container,
            PACKET_FIELD_COMMUNITY_DELETED,
        )
        if packet is None:
            return None
        for number, wire_type, value in iter_fields(packet):
            if number == 3 and wire_type == 2:
                return decode_root_guid_message(value)
        return None

    @classmethod
    def _decode_community_leave_packet(
        cls,
        packet_container: Optional[bytes],
    ) -> Optional[tuple[str, Optional[str], int]]:
        packet = cls._packet_payload(
            packet_container,
            PACKET_FIELD_COMMUNITY_LEAVE,
        )
        if packet is None:
            return None

        community_id = None
        user_id = None
        leave_reason = 0
        for number, wire_type, value in iter_fields(packet):
            if number == 3 and wire_type == 2:
                community_id = decode_root_guid_message(value)
            elif number == 4 and wire_type == 2:
                user_id = decode_root_guid_message(value)
            elif number == 5 and wire_type == 0:
                leave_reason = int(value)

        if community_id is None:
            return None
        return community_id, user_id, leave_reason

    @staticmethod
    def _safe_text(value: bytes) -> Optional[str]:
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not text or any(ord(ch) < 9 for ch in text):
            return None
        return text

    @classmethod
    def _collect_strings(cls, data: bytes, *, depth: int = 0) -> list[str]:
        if depth > 5:
            return []
        strings: list[str] = []
        try:
            fields = list(iter_fields(data))
        except (ValueError, TypeError):
            return strings
        for _, wire_type, value in fields:
            if wire_type != 2:
                continue
            text = cls._safe_text(value)
            if text is not None:
                strings.append(text)
            strings.extend(cls._collect_strings(value, depth=depth + 1))
        return strings

    @classmethod
    def _asset_reference_urls(cls, reference_maps: Optional[bytes]) -> dict[str, str]:
        result: dict[str, str] = {}
        if reference_maps is None:
            return result
        try:
            entries = [value for _, wt, value in iter_fields(reference_maps) if wt == 2]
        except ValueError:
            return result
        for entry in entries:
            strings = cls._collect_strings(entry)
            asset_uris = [item for item in strings if item.startswith("root://")]
            urls = [item for item in strings if item.startswith(("https://", "http://"))]
            for asset_uri in asset_uris:
                if urls:
                    result.setdefault(asset_uri, urls[0])
        return result

    @classmethod
    def _decode_message_attachments(
        cls,
        message_uri_blobs: list[bytes],
        reference_maps: Optional[bytes],
    ) -> tuple[MessageAttachment, ...]:
        reference_urls = cls._asset_reference_urls(reference_maps)
        attachments: list[MessageAttachment] = []
        seen: set[str] = set()
        mime_re = re.compile(r"^[\w.+-]+/[\w.+-]+$")
        filename_re = re.compile(r"[^/\\]+\.[A-Za-z0-9]{1,10}$")

        for blob in message_uri_blobs:
            strings = cls._collect_strings(blob)
            asset_uri = next(
                (item for item in strings if item.startswith("root://")),
                None,
            )
            if asset_uri is None:
                continue
            # Mentions and channel links are MessageUri entries too, not attachments.
            if asset_uri.startswith(("root://user/", "root://channel/", "root://role/")):
                continue
            if asset_uri in seen:
                continue

            mime_type = next((item for item in strings if mime_re.fullmatch(item)), None)
            filename = next(
                (
                    item for item in strings
                    if filename_re.search(item)
                    and not item.startswith(("http://", "https://", "root://"))
                ),
                "",
            )
            direct_url = next(
                (item for item in strings if item.startswith(("https://", "http://"))),
                None,
            )

            length = 0
            try:
                for _, wire_type, value in iter_fields(blob):
                    if wire_type == 0 and int(value) > length:
                        length = int(value)
            except ValueError:
                pass

            attachments.append(
                MessageAttachment(
                    asset_uri=asset_uri,
                    filename=filename,
                    length=length,
                    mime_type=mime_type,
                    file_type=(mime_type.split("/", 1)[0] if mime_type else None),
                    download_url=direct_url or reference_urls.get(asset_uri),
                    raw=blob,
                )
            )
            seen.add(asset_uri)
        return tuple(attachments)

    @classmethod
    def _decode_message_packet(
        cls,
        packet_container: Optional[bytes],
    ) -> Optional[Message]:
        if packet_container is None:
            return None

        packet = None
        for number, wire_type, value in iter_fields(
            packet_container
        ):
            if (
                number == PACKET_FIELD_MESSAGE
                and wire_type == 2
            ):
                packet = value
                break

        if packet is None:
            return None

        values: dict[int, tuple[int, object]] = {}
        message_uri_blobs: list[bytes] = []
        reference_maps = None
        for number, wire_type, value in iter_fields(packet):
            if number == 13 and wire_type == 2:
                message_uri_blobs.append(value)
            elif number == 14 and wire_type == 2:
                reference_maps = value
            if number not in values:
                values[number] = (wire_type, value)

        def guid(field_number: int) -> Optional[str]:
            item = values.get(field_number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        def timestamp(field_number: int):
            item = values.get(field_number)
            if item is None or item[0] != 2:
                return None
            return decode_timestamp_message(item[1])

        def integer(field_number: int) -> int:
            item = values.get(field_number)
            if item is None or item[0] != 0:
                return 0
            return int(item[1])

        def text(field_number: int) -> str:
            item = values.get(field_number)
            if item is None or item[0] != 2:
                return ""
            return item[1].decode("utf-8", errors="replace")

        message_id = guid(6)
        container_id = guid(4)
        user_id = guid(5)

        if not message_id or not container_id or not user_id:
            return None

        payload_raw = None
        payload_item = values.get(12)
        if payload_item is not None and payload_item[0] == 2:
            payload_raw = payload_item[1]

        return Message(
            id=message_id,
            community_id=guid(3),
            container_id=container_id,
            user_id=user_id,
            content=text(10),
            packet_type=integer(1),
            message_type=integer(11),
            deleted_at=timestamp(7),
            edited_at=timestamp(8),
            pinned_at=timestamp(9),
            payload_raw=payload_raw,
            attachments=cls._decode_message_attachments(
                message_uri_blobs,
                reference_maps,
            ),
            raw=packet,
        )

    @staticmethod
    def _message_action(message: Message) -> str:
        if message.deleted_at is not None:
            return "delete"
        if message.edited_at is not None:
            return "edit"
        return "create"

    def _build_ping(self) -> Optional[bytes]:
        # Always send a heartbeat, even before the first message arrives -- the
        # hub idle-closes (code 1000) if it doesn't hear from us in time, and
        # gating on a received sequence caused exactly that. Fall back to 0.
        current = self.current_sequence if self.current_sequence is not None else 0
        nested_sequence = self.last_non_ping_sequence
        if nested_sequence is None:
            nested_sequence = current
        ping = (
            field_key(1, 0)
            + encode_varint(1)
            + field_key(2, 0)
            + encode_varint(nested_sequence)
        )
        return (
            field_key(2, 0)
            + encode_varint(current)
            + length_field(4, ping)
        )

    def _message_from_notification(self, socket_packet):
        """Extract the message embedded in a NOTIFICATION packet.

        The hub pushes NOTIFICATION (case 180) in real time for anything
        directed at this account -- mentions, DM messages -- and embeds the
        whole originating message inside ``payload``:

            payload.f6.f4 = the message (same field layout as MessagePacket:
                            3=community, 4=container, 5=user, 6=id, 10=content)

        Returns a :class:`Message` or None if this notification doesn't carry
        one (e.g. friend-request notifications).
        """
        from .models import Message
        from .packet_schemas import decode_packet

        fields = socket_packet.fields or {}
        payload = fields.get("payload")
        if not isinstance(payload, (bytes, bytearray)):
            return None

        # Walk payload -> f6 -> f4 to reach the embedded message.
        def _sub(data, number):
            for n, wt, value in iter_fields(data):
                if n == number and wt == 2 and isinstance(value, (bytes, bytearray)):
                    return bytes(value)
            return None

        outer = _sub(payload, 6)
        if outer is None:
            return None
        embedded = _sub(outer, 4)
        if embedded is None:
            return None

        decoded = decode_packet("MESSAGE", embedded)
        content = decoded.get("message_content")
        if not content:
            return None

        # The notification also carries the author's display name (Root's
        # message objects only have ids), so grab it while we're here -- this
        # is the main way the state cache learns usernames.
        author_id = decoded.get("user_id") or fields.get("user_id")
        try:
            # f14 lives inside the embedded message (f4), not the
            # outer wrapper -- confirmed against a real payload.
            profile = _sub(embedded, 14)
            inner_profile = _sub(profile, 10) if profile else None
            if inner_profile:
                for n, wt, value in iter_fields(inner_profile):
                    if n == 11 and wt == 2 and value:
                        name = bytes(value).decode("utf-8", errors="replace")
                        if name and name.isprintable():
                            cache = getattr(self.client_cache, "remember_user", None)
                            if cache is not None:
                                cache(author_id, name)
                        break
        except Exception:
            pass
        return Message(
            id=decoded.get("id") or "",
            container_id=(
                decoded.get("container_id")
                or fields.get("sub_container_id")
                or ""
            ),
            user_id=decoded.get("user_id") or fields.get("user_id") or "",
            content=content,
            community_id=(
                decoded.get("community_id") or fields.get("container_id")
            ),
        )

    async def _force_resync(self, websocket) -> None:
        """Close the socket after ``resync_interval`` to pull a fresh batch.

        The hub holds an idle connection open sending only pings; new messages
        arrive in the *next* resync batch, which requires reconnecting. This
        timer forces that cycle so message delivery stays near-real-time. The
        sequence cursor persists, so each fresh connection resumes correctly
        and no message is missed or duplicated.
        """
        try:
            await asyncio.sleep(self.resync_interval)
            try:
                await websocket.close(code=1000)
            except Exception:
                pass
        except asyncio.CancelledError:
            raise

    async def _keepalive(self, websocket) -> None:
        # First heartbeat goes out quickly (well under the hub's idle window),
        # then repeats on a short fuzzy interval. The C# client pings on a
        # ~10s timer; we lead with a fast first ping because each (re)connect
        # otherwise races the idle timeout.
        try:
            await asyncio.sleep(self.ping_first_delay)
            while True:
                packet = self._build_ping()
                if packet is not None:
                    await websocket.send(packet)
                await asyncio.sleep(
                    random.uniform(self.ping_interval_min, self.ping_interval_max)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("keepalive stopped: %s", exc)

    def _connect_url(self) -> str:
        """Build the hub URL for this attempt, carrying the resume cursor.

        The hub streams notifications from a ``sequence_number`` query param:
        given the highest sequence we've seen, ``sequence_number = highest + 1``
        tells the server to stream everything *after* it and hold the socket
        open (live tail). Without the param the server delivers a bounded
        backlog and then closes normally (code 1000) -- which looked like a
        2-second reconnect loop. On the very first connect we have no cursor,
        so we omit it and adopt whatever sequence the first delivery carries.
        """
        # Match the reference handler: the cursor is the highest SequenceNumber
        # of *any* received packet (it updates from every packet, before the
        # ping check), and resume is cursor + 1.
        cursor = self.current_sequence
        if cursor is None:
            cursor = self.last_non_ping_sequence
        if cursor is None:
            log.info("gateway first connect (no resume cursor)")
            return self.hub_url

        from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

        parts = urlparse(self.hub_url)
        query = dict(parse_qsl(parts.query))
        resume = int(cursor) + 1
        query["sequence_number"] = str(resume)
        log.info("gateway resuming from sequence_number=%s", resume)
        return urlunparse(parts._replace(query=urlencode(query)))

    def _open_websocket(self, url: str, headers: dict):
        """Open the hub websocket in a way that works across websockets versions.

        The library renamed the header parameter (``extra_headers`` ->
        ``additional_headers``) and has changed which timeout/size kwargs it
        accepts over time. Rather than hard-code one spelling (which raises
        ``TypeError: ... unexpected keyword argument`` on the other), we
        introspect ``connect`` and pass only what it supports, preferring the
        newer names.
        """
        import inspect

        connect = _import_websockets().connect
        proxy_url = getattr(self, "proxy", None)
        try:
            params = set(inspect.signature(connect).parameters)
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in inspect.signature(connect).parameters.values()
            )
        except (TypeError, ValueError):
            params, has_var_kw = set(), True

        def supported(name: str) -> bool:
            return has_var_kw or name in params

        kwargs: dict = {}
        # Header kwarg: prefer the newer 'additional_headers', fall back to
        # 'extra_headers'. If we can't introspect, we try new-then-old below.
        if supported("additional_headers"):
            kwargs["additional_headers"] = headers
        elif supported("extra_headers"):
            kwargs["extra_headers"] = headers

        for name, value in (
            ("open_timeout", 20),
            ("ping_timeout", 20),
            ("ping_interval", 45),
            ("max_size", None),
        ):
            if supported(name):
                kwargs[name] = value

        # Route the websocket through the same proxy as the API, when one is
        # set. websockets >= 13 takes `proxy=`; older versions need
        # python-socks and a custom sock, so we degrade rather than fail.
        if proxy_url:
            if supported("proxy"):
                kwargs["proxy"] = proxy_url
            else:
                log.warning(
                    "websockets %s can't proxy directly; the gateway will "
                    "connect straight out. Upgrade websockets (>=13) to route "
                    "it through %s",
                    getattr(_import_websockets(), "__version__", "?"), proxy_url,
                )

        # If introspection couldn't tell us the header name, try both spellings.
        if "additional_headers" not in kwargs and "extra_headers" not in kwargs:
            try:
                return connect(url, additional_headers=headers, **kwargs)
            except TypeError:
                return connect(url, extra_headers=headers, **kwargs)

        try:
            return connect(url, **kwargs)
        except TypeError:
            # Last-resort: swap the header kwarg spelling and retry once.
            if "additional_headers" in kwargs:
                kwargs["extra_headers"] = kwargs.pop("additional_headers")
            elif "extra_headers" in kwargs:
                kwargs["additional_headers"] = kwargs.pop("extra_headers")
            return connect(url, **kwargs)

    async def _run_once(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "x-root-Device-Id": self.device_id,
            "User-Agent": "Root/0.9.126",
        }
        try:
            async with self._open_websocket(self._connect_url(), headers) as websocket:
                self._ready.set()
                self._connect_error = None
                self._just_connected = True
                self._got_data = False
                keepalive = asyncio.create_task(self._keepalive(websocket))
                resync = None
                if self.resync_interval and self.resync_interval > 0:
                    resync = asyncio.create_task(
                        self._force_resync(websocket)
                    )
                try:
                    async for message in websocket:
                        if not isinstance(message, bytes):
                            continue
                        (
                            sequence,
                            packet_case,
                            packet_container,
                            error_code,
                        ) = self._notification_info(message)
                        if error_code == PacketErrorCode.SYNC_LOST:
                            # The server telling us our cursor is stale, in
                            # band. Everything downstream still runs -- a
                            # SYNC_LOST frame can carry a packet -- this is an
                            # extra signal beside the 4016 close-code path,
                            # not a replacement for it.
                            log.warning(
                                "gateway sync lost at seq=%s; the server "
                                "rejected our resume cursor",
                                sequence,
                            )
                            await self.dispatch("sync_lost", sequence)
                        if sequence is not None:
                            self._got_data = True
                            log.debug(
                                "recv seq=%s case=%s", sequence, packet_case
                            )
                            self.current_sequence = sequence
                            if packet_case != 1:
                                self.last_non_ping_sequence = sequence
                        socket_data = self._socket_response(
                            message,
                            sequence,
                            packet_case,
                            packet_container,
                            debug_trees=self.decode_debug_trees,
                            error_code=error_code,
                        )
                        socket_packet = SocketPacket(
                            sequence=sequence,
                            type=PacketType.from_case(packet_case),
                            case=packet_case,
                            data=socket_data,
                            raw=message,
                        )

                        await self.dispatch(
                            "socket_response",
                            socket_packet,
                        )
                        await self.dispatch(
                            "packet",
                            socket_packet,
                        )
                        if socket_packet.type is not PacketType.UNKNOWN:
                            await self.dispatch(
                                "packet_" + socket_packet.type.name.casefold(),
                                socket_packet,
                            )

                        if socket_packet.type is PacketType.MESSAGE:
                            await self.dispatch(
                                "message_packet",
                                socket_packet,
                            )
                        elif socket_packet.type is (
                            PacketType.MESSAGE_SET_TYPING_INDICATOR
                        ):
                            await self.dispatch(
                                "typing_packet",
                                socket_packet,
                            )
                        elif socket_packet.type is PacketType.UNKNOWN:
                            await self.dispatch(
                                "unknown_packet",
                                socket_packet,
                            )

                        event = NotificationEvent(
                            sequence,
                            packet_case,
                            message,
                        )
                        await self.dispatch("notification", event)

                        # A NOTIFICATION embeds the whole message that caused
                        # it (mentions, DMs). The hub delivers these in
                        # real time, so surface them as normal message events
                        # rather than leaving them as opaque notifications.
                        if socket_packet.type is PacketType.NOTIFICATION:
                            embedded = self._message_from_notification(
                                socket_packet
                            )
                            if embedded is not None:
                                await self.dispatch(
                                    "message",
                                    MessageEvent(
                                        sequence,
                                        embedded,
                                        MessageAction.CREATE,
                                        message,
                                    ),
                                )

                        community = self._decode_community_packet(
                            packet_container
                        )
                        if community is not None:
                            await self.dispatch(
                                "community",
                                CommunityEvent(
                                    sequence=sequence,
                                    community=community,
                                    raw=message,
                                ),
                            )

                        channel_created = self._decode_channel_packet(
                            packet_container,
                            packet_field=PACKET_FIELD_CHANNEL_CREATED,
                        )
                        if channel_created is not None:
                            await self.dispatch(
                                "channel",
                                ChannelEvent(
                                    sequence=sequence,
                                    channel=channel_created,
                                    action=ChannelAction.CREATE,
                                    raw=message,
                                ),
                            )

                        channel_edited = self._decode_channel_packet(
                            packet_container,
                            packet_field=PACKET_FIELD_CHANNEL_EDITED,
                        )
                        if channel_edited is not None:
                            await self.dispatch(
                                "channel",
                                ChannelEvent(
                                    sequence=sequence,
                                    channel=channel_edited,
                                    action=ChannelAction.EDIT,
                                    raw=message,
                                ),
                            )

                        channel_deleted = (
                            self._decode_channel_deleted_packet(
                                packet_container
                            )
                        )
                        if channel_deleted is not None:
                            (
                                deleted_channel_id,
                                deleted_community_id,
                                deleted_group_id,
                            ) = channel_deleted
                            await self.dispatch(
                                "channel_deleted",
                                ChannelDeletedEvent(
                                    sequence=sequence,
                                    channel_id=deleted_channel_id,
                                    community_id=deleted_community_id,
                                    channel_group_id=deleted_group_id,
                                    cached_channel=None,
                                    raw=message,
                                ),
                            )

                        community_deleted = (
                            self._decode_community_deleted_packet(
                                packet_container
                            )
                        )
                        if community_deleted is not None:
                            await self.dispatch(
                                "community_deleted",
                                CommunityDeletedEvent(
                                    sequence=sequence,
                                    community_id=community_deleted,
                                    cached_community=None,
                                    removed_channels=(),
                                    raw=message,
                                ),
                            )

                        community_leave = (
                            self._decode_community_leave_packet(
                                packet_container
                            )
                        )
                        if community_leave is not None:
                            (
                                left_community_id,
                                leaving_user_id,
                                leave_reason,
                            ) = community_leave
                            await self.dispatch(
                                "community_leave",
                                CommunityLeaveEvent(
                                    sequence=sequence,
                                    community_id=left_community_id,
                                    user_id=leaving_user_id,
                                    leave_reason=leave_reason,
                                    is_self=False,
                                    cached_community=None,
                                    removed_channels=(),
                                    raw=message,
                                ),
                            )

                        decoded_message = self._decode_message_packet(
                            packet_container
                        )
                        if decoded_message is not None:
                            action = self._message_action(
                                decoded_message
                            )
                            await self.dispatch(
                                "message",
                                MessageEvent(
                                    sequence=sequence,
                                    message=decoded_message,
                                    action=MessageAction.from_value(action),
                                    raw=message,
                                ),
                            )

                        detached = self._detach_container(packet_container)
                        if detached:
                            await self.dispatch(
                                "call_detached",
                                CallDetachedEvent(sequence, detached, message),
                            )
                finally:
                    keepalive.cancel()
                    try:
                        await keepalive
                    except asyncio.CancelledError:
                        pass
                    if resync is not None:
                        resync.cancel()
                        try:
                            await resync
                        except asyncio.CancelledError:
                            pass
                    # Record why the server closed us -- invaluable for
                    # diagnosing immediate disconnects (auth, sequence, protocol).
                    self._last_close_code = getattr(
                        websocket, "close_code", None
                    )
                    self._last_close_reason = getattr(
                        websocket, "close_reason", None
                    )
                    if self._last_close_code is not None:
                        log.info(
                            "gateway closed by server: code=%s reason=%r",
                            self._last_close_code,
                            self._last_close_reason,
                        )
                    # 4016 = the resume cursor is stale/out of range. Drop it so
                    # the next attempt reconnects fresh (no sequence_number) and
                    # re-adopts a valid cursor from the next delivery.
                    if self._last_close_code == 4016:
                        log.info("gateway resume cursor stale (4016); resetting")
                        self.current_sequence = None
                        self.last_non_ping_sequence = None
        finally:
            self._ready.clear()
