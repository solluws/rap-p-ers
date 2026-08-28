from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import sys
import urllib.request
import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse
from uuid import uuid4

from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    format_root_guid,
    normalize_root_guid,
)
from ..models import CallSession, MessageAttachment
from ..protocol import (
    bool_field,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    length_field,
)
from ..transport import GrpcWebTransport

log = logging.getLogger("rootpy.calls")

BASE = "https://api.rootapp.com/root.WebRtcGrpcService"
SESSION_CREATE = f"{BASE}/SessionCreate"
SET_MUTE_AND_DEAFEN = f"{BASE}/SetMuteAndDeafen"
SET_MUTE_AND_DEAFEN_OTHER = f"{BASE}/SetMuteAndDeafenOther"
SET_PAUSE = f"{BASE}/SetPause"
GET_ICE_INFO = f"{BASE}/GetIceInfo"
DETACH = f"{BASE}/Detach"
KICK = f"{BASE}/Kick"
TRACKS_CREATE = f"{BASE}/TracksCreate"


@dataclass(frozen=True)
class IceInfo:
    urls: tuple[str, ...]
    username: str
    credentials: str
    features_raw: Optional[bytes] = None


@dataclass(frozen=True)
class AudioPlayback:
    source: str
    track_id: str
    mid: Optional[str]


class CallService:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter,
        direct_messages=None,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self.direct_messages = direct_messages
        self.active_session: Optional[CallSession] = None
        self.active_community_id: Optional[str] = None
        self._peer_connection = None
        self._media_player = None
        self._audio_playback: Optional[AudioPlayback] = None
        self._playwright = None
        self._browser = None
        self._browser_context = None
        self._browser_page = None

    def require_active_target(self) -> tuple[str, Optional[str]]:
        if self.active_session is None:
            raise RuntimeError("No active call session")
        return (
            self.active_session.container_id,
            self.active_community_id,
        )

    @staticmethod
    def _context() -> tuple[bytes, str]:
        high64, low64 = create_command_idempotency_guid()
        command_id = format_root_guid(high64, low64)
        context = length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )
        return context, command_id

    @classmethod
    def _base_payload(
        cls,
        container_id: str,
        community_id: Optional[str],
    ) -> tuple[bytearray, str]:
        context, command_id = cls._context()
        payload = bytearray(length_field(1, context))
        if community_id is not None:
            payload += length_field(
                10,
                encode_root_guid(community_id),
            )
        payload += length_field(
            11,
            encode_root_guid(container_id),
        )
        return payload, command_id

    @staticmethod
    def _bool_wrapper(value: bool) -> bytes:
        return bool_field(1, value)

    async def _unary(self, endpoint: str, payload: bytes, operation: str):
        token = self._token_getter()
        if not token:
            raise RuntimeError(
                f"{operation} requires an authenticated Root access token"
            )

        headers = {
            "grpc-timeout": "15000m",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML,like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36 "
                "RootPlatform 1.0 v0.9.126 default"
            ),
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "accept": "*/*",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-encoding": "gzip,deflate,br,zstd",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "authorization": f"Bearer {token}",
            "priority": "u=1, i",
        }

        return await self.transport.unary(
            endpoint=endpoint,
            body=grpc_frame(payload),
            headers=headers,
            operation=operation,
        )

    @staticmethod
    def _decode_wrapper(data: bytes) -> Optional[str]:
        for number, wire_type, value in iter_fields(data):
            if number == 1 and wire_type == 2:
                return value.decode("utf-8", errors="replace")
        return None

    @classmethod
    def _parse_session(cls, body: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_id": None,
            "session_description_type": None,
            "session_description_sdp": None,
            "audio_bandwidth": 0,
            "video_bandwidth": 0,
            "screen_bandwidth": 0,
            "screen_audio_bandwidth": 0,
            "error_code": None,
            "error_description": None,
            # 0.9.128 additions -- WebRtcSessionCreateResponse fields 17/18/19
            # (Backend, ServerUrl, AccessToken). backend is WebRtcBackend:
            # 0=Unspecified, 1=V1, 2=V2.
            "backend": 0,
            "server_url": None,
            "access_token": None,
        }
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 4 and wire_type == 2:
                    description_type, description_sdp = (
                        cls._parse_session_description(value)
                    )
                    result["session_description_type"] = description_type
                    result["session_description_sdp"] = description_sdp
                elif number == 5 and wire_type == 2:
                    result["session_id"] = cls._decode_wrapper(value)
                elif number == 7 and wire_type == 0:
                    result["audio_bandwidth"] = int(value)
                elif number == 8 and wire_type == 0:
                    result["video_bandwidth"] = int(value)
                elif number == 9 and wire_type == 0:
                    result["screen_bandwidth"] = int(value)
                elif number == 10 and wire_type == 0:
                    result["screen_audio_bandwidth"] = int(value)
                elif number == 15 and wire_type == 2:
                    result["error_code"] = cls._decode_wrapper(value)
                elif number == 16 and wire_type == 2:
                    result["error_description"] = cls._decode_wrapper(value)
                elif number == 17 and wire_type == 0:
                    result["backend"] = int(value)
                elif number == 18 and wire_type == 2:
                    # ServerUrl/AccessToken are bare strings here, not the
                    # StringValue wrappers used for session_id and the errors.
                    result["server_url"] = value.decode(
                        "utf-8",
                        errors="replace",
                    )
                elif number == 19 and wire_type == 2:
                    result["access_token"] = value.decode(
                        "utf-8",
                        errors="replace",
                    )
        return result

    async def create_session(
        self,
        container_id: str,
        *,
        community_id: Optional[str] = None,
        session_description: Optional[bytes] = None,
        supports_v2: bool = False,
    ) -> CallSession:
        container_id = normalize_root_guid(container_id)
        if community_id is not None:
            community_id = normalize_root_guid(community_id)

        payload, command_id = self._base_payload(
            container_id,
            community_id,
        )
        if session_description is not None:
            payload += length_field(12, session_description)
        if supports_v2:
            # WebRtcSessionCreateRequest field 13 (SupportsV2). The app omits it
            # when false, so we do too -- default False keeps the bytes on the
            # wire byte-identical to what this client sent before 0.9.128.
            payload += bool_field(13, True)

        response = await self._unary(
            SESSION_CREATE,
            bytes(payload),
            "SessionCreate",
        )
        parsed = self._parse_session(response.content)
        if parsed["error_code"]:
            raise RuntimeError(
                "SessionCreate error "
                f"{parsed['error_code']}: "
                f"{parsed['error_description'] or ''}"
            )
        if not parsed["session_id"]:
            raise RuntimeError("SessionCreate returned no session ID")

        session = CallSession(
            session_id=parsed["session_id"],
            container_id=container_id,
            command_id=command_id,
            audio_bandwidth=parsed["audio_bandwidth"],
            video_bandwidth=parsed["video_bandwidth"],
            screen_bandwidth=parsed["screen_bandwidth"],
            screen_audio_bandwidth=parsed["screen_audio_bandwidth"],
            backend=parsed["backend"],
            server_url=parsed["server_url"],
            access_token=parsed["access_token"],
        )
        self.active_session = session
        self.active_community_id = community_id
        return session

    async def call_user(self, user) -> CallSession:
        if self.direct_messages is None:
            raise RuntimeError(
                "DirectMessageService is not configured"
            )
        direct_message = await self.direct_messages.get_or_create(user)
        return await self.create_session(
            direct_message.id,
            community_id=None,
        )

    async def join_channel(self, channel) -> CallSession:
        channel_id = getattr(channel, "id", None)
        community_id = getattr(channel, "community_id", None)

        if channel_id is None or community_id is None:
            raise ValueError(
                "join_channel requires a resolved Channel object"
            )

        return await self.create_session(
            channel_id,
            community_id=community_id,
        )

    async def disconnect(self) -> None:
        if self.active_session is None:
            raise RuntimeError("No active call session")
        await self.detach(
            container_id=self.active_session.container_id,
            community_id=self.active_community_id,
        )

    async def set_mute_and_deafen(
        self,
        *,
        container_id: str,
        community_id: Optional[str] = None,
        muted: Optional[bool] = None,
        deafened: Optional[bool] = None,
    ) -> None:
        if muted is None and deafened is None:
            raise ValueError("Specify muted and/or deafened")

        container_id = normalize_root_guid(container_id)
        if community_id is not None:
            community_id = normalize_root_guid(community_id)

        payload, _ = self._base_payload(container_id, community_id)
        if muted is not None:
            payload += length_field(13, self._bool_wrapper(muted))
        if deafened is not None:
            payload += length_field(14, self._bool_wrapper(deafened))

        await self._unary(
            SET_MUTE_AND_DEAFEN,
            bytes(payload),
            "SetMuteAndDeafen",
        )

    async def set_mute_and_deafen_other(
        self,
        *,
        container_id: str,
        community_id: Optional[str],
        user_id: str,
        muted: Optional[bool] = None,
        deafened: Optional[bool] = None,
    ) -> None:
        if community_id is None:
            raise ValueError("community_id is required")
        if muted is None and deafened is None:
            raise ValueError("Specify muted and/or deafened")

        container_id = normalize_root_guid(container_id)
        community_id = normalize_root_guid(community_id)
        user_id = normalize_root_guid(user_id)

        payload, _ = self._base_payload(container_id, community_id)
        payload += length_field(12, encode_root_guid(user_id))
        if muted is not None:
            payload += length_field(13, self._bool_wrapper(muted))
        if deafened is not None:
            payload += length_field(14, self._bool_wrapper(deafened))

        await self._unary(
            SET_MUTE_AND_DEAFEN_OTHER,
            bytes(payload),
            "SetMuteAndDeafenOther",
        )

    async def set_pause(
        self,
        *,
        container_id: str,
        community_id: Optional[str] = None,
        video_paused: Optional[bool] = None,
        screen_paused: Optional[bool] = None,
    ) -> None:
        if video_paused is None and screen_paused is None:
            raise ValueError(
                "Specify video_paused and/or screen_paused"
            )

        container_id = normalize_root_guid(container_id)
        if community_id is not None:
            community_id = normalize_root_guid(community_id)

        payload, _ = self._base_payload(container_id, community_id)
        if video_paused is not None:
            payload += length_field(
                12,
                self._bool_wrapper(video_paused),
            )
        if screen_paused is not None:
            payload += length_field(
                13,
                self._bool_wrapper(screen_paused),
            )

        await self._unary(SET_PAUSE, bytes(payload), "SetPause")

    async def detach(
        self,
        *,
        container_id: str,
        community_id: Optional[str] = None,
    ) -> None:
        container_id = normalize_root_guid(container_id)
        if community_id is not None:
            community_id = normalize_root_guid(community_id)
        payload, _ = self._base_payload(container_id, community_id)
        await self.stop_audio(close_peer=True)
        await self._unary(DETACH, bytes(payload), "Detach")
        if (
            self.active_session is not None
            and self.active_session.container_id == container_id
        ):
            self.active_session = None
            self.active_community_id = None

    async def kick(
        self,
        *,
        container_id: str,
        community_id: Optional[str],
        user_id: str,
    ) -> None:
        if community_id is None:
            raise ValueError("community_id is required")

        container_id = normalize_root_guid(container_id)
        community_id = normalize_root_guid(community_id)
        user_id = normalize_root_guid(user_id)

        payload, _ = self._base_payload(container_id, community_id)
        payload += length_field(12, encode_root_guid(user_id))
        await self._unary(KICK, bytes(payload), "Kick")

    async def get_ice_info(self) -> IceInfo:
        response = await self._unary(
            GET_ICE_INFO,
            b"",
            "GetIceInfo",
        )

        urls: list[str] = []
        username = ""
        credentials = ""
        features_raw = None

        for flag, frame in iter_grpc_web_frames(response.content):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 1 and wire_type == 2:
                    urls.append(value.decode("utf-8", errors="replace"))
                elif number == 2 and wire_type == 2:
                    username = value.decode("utf-8", errors="replace")
                elif number == 3 and wire_type == 2:
                    credentials = value.decode(
                        "utf-8",
                        errors="replace",
                    )
                elif number == 4 and wire_type == 2:
                    features_raw = value

        return IceInfo(
            urls=tuple(urls),
            username=username,
            credentials=credentials,
            features_raw=features_raw,
        )


    @staticmethod
    def _session_description_payload(description_type: str, sdp: str) -> bytes:
        payload = bytearray()
        if description_type:
            payload += length_field(4, description_type.encode("utf-8"))
        if sdp:
            payload += length_field(5, sdp.encode("utf-8"))
        return bytes(payload)

    @staticmethod
    def _track_create_payload(
        *,
        location: str,
        track_id: str,
        mid: Optional[str],
        is_audio: bool,
    ) -> bytes:
        payload = bytearray()
        payload += length_field(4, location.encode("utf-8"))
        payload += length_field(5, track_id.encode("utf-8"))
        if mid is not None:
            payload += length_field(
                7,
                length_field(1, mid.encode("utf-8")),
            )
        if is_audio:
            payload += bool_field(8, True)
        return bytes(payload)

    @classmethod
    def _parse_session_description(
        cls,
        data: bytes,
    ) -> tuple[str, str]:
        description_type = ""
        sdp = ""
        for number, wire_type, value in iter_fields(data):
            if number == 4 and wire_type == 2:
                description_type = value.decode(
                    "utf-8",
                    errors="replace",
                )
            elif number == 5 and wire_type == 2:
                sdp = value.decode("utf-8", errors="replace")
        return description_type, sdp

    @classmethod
    def _parse_tracks_create(cls, body: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {
            "description_type": "",
            "sdp": "",
            "requires_immediate_renegotiation": False,
            "error_code": None,
            "error_description": None,
        }
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 5 and wire_type == 2:
                    (
                        result["description_type"],
                        result["sdp"],
                    ) = cls._parse_session_description(value)
                elif number == 6 and wire_type == 0:
                    result["requires_immediate_renegotiation"] = bool(value)
                elif number == 15 and wire_type == 2:
                    result["error_code"] = cls._decode_wrapper(value)
                elif number == 16 and wire_type == 2:
                    result["error_description"] = cls._decode_wrapper(value)
        return result

    async def tracks_create(
        self,
        *,
        track_id: str,
        mid: Optional[str],
        local_description_type: str,
        local_sdp: str,
    ) -> dict[str, Any]:
        container_id, community_id = self.require_active_target()
        payload, _ = self._base_payload(container_id, community_id)
        payload += length_field(
            12,
            self._track_create_payload(
                location="local",
                track_id=track_id,
                mid=mid,
                is_audio=True,
            ),
        )
        payload += length_field(
            13,
            self._session_description_payload(
                local_description_type,
                local_sdp,
            ),
        )
        try:
            response = await self._unary(
                TRACKS_CREATE,
                bytes(payload),
                "TracksCreate",
            )
        except Exception as exc:
            raise RuntimeError(
                "TracksCreate failed for "
                f"track_id={track_id!r}, mid={mid!r}, "
                f"container_id={container_id!r}, "
                f"community_id={community_id!r}: {exc}"
            ) from exc
        parsed = self._parse_tracks_create(response.content)
        if parsed["error_code"]:
            raise RuntimeError(
                "TracksCreate error "
                f"{parsed['error_code']}: "
                f"{parsed['error_description'] or ''}"
            )
        if not parsed["sdp"]:
            raise RuntimeError("TracksCreate returned no SDP answer")
        return parsed

    @staticmethod
    def _normalize_audio_source_text(source: str) -> str:
        source = source.strip()

        if not source:
            return source

        if source.startswith("[") and "](" in source and source.endswith(")"):
            close = source.find("](")
            if close != -1:
                destination = source[close + 2:-1].strip()
                if destination:
                    source = destination

        if (
            len(source) >= 2
            and source.startswith("<")
            and source.endswith(">")
        ):
            source = source[1:-1].strip()

        source = source.replace("\\", "/")

        lowered = source.lower()
        if lowered.startswith("https:/") and not lowered.startswith("https://"):
            source = "https://" + source[len("https:/"):].lstrip("/")
        elif lowered.startswith("http:/") and not lowered.startswith("http://"):
            source = "http://" + source[len("http:/"):].lstrip("/")

        return source

    @staticmethod
    def _validate_audio_source(source: Union[str, MessageAttachment]) -> str:
        if isinstance(source, MessageAttachment):
            if not source.is_audio and source.mime_type is not None:
                raise ValueError(
                    f"Attachment is not audio: {source.mime_type}"
                )
            if source.download_url is None and source.asset_uri.startswith("root://"):
                raise RuntimeError(
                    "The message attachment was decoded, but Root did not "
                    "include a downloadable URL in its reference map"
                )
            source = source.source
        source = CallService._normalize_audio_source_text(source)
        if not source:
            raise ValueError("Audio source cannot be empty")
        parsed = urlparse(source)
        if parsed.scheme:
            if parsed.scheme not in ("http", "https", "file"):
                raise ValueError(
                    "Audio URL must use http, https, or file; "
                    f"got {source!r}"
                )
            return source
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return str(path.resolve())

    @staticmethod
    def _to_root_offer_sdp(sdp: str) -> str:

        normalized = sdp.replace("\r\n", "\n").replace("\r", "\n")
        sections = re.split(r"(?=^m=)", normalized, flags=re.MULTILINE)
        session = sections[0]
        output = [session.rstrip("\r\n")]

        for section in sections[1:]:
            lines = section.replace("\r\n", "\n").splitlines()
            if not lines:
                continue

            media = lines[0]
            body = lines[1:]

            parts = media.split()
            if len(parts) >= 2:
                parts[1] = "9"
            media = " ".join(parts)

            cleaned = []
            has_trickle = False
            has_rtcp_rsize = False
            has_bandwidth = False

            for line in body:
                if line.startswith("a=candidate:") or line == "a=end-of-candidates":
                    continue
                if line.startswith("c=IN IP4 "):
                    line = "c=IN IP4 0.0.0.0"
                if line == "a=ice-options:trickle":
                    has_trickle = True
                if line == "a=rtcp-rsize":
                    has_rtcp_rsize = True
                if line.startswith("b=AS:"):
                    has_bandwidth = True
                cleaned.append(line)

            if media.startswith("m=audio "):
                media = "m=audio 9 UDP/TLS/RTP/SAVPF 111"
                converted = []
                for line in cleaned:
                    if re.match(r"a=rtpmap:(?!96\b)", line):
                        continue
                    if re.match(r"a=(?:rtcp-fb|fmtp):(?!96\b)", line):
                        continue
                    line = re.sub(r"^(a=(?:rtpmap|rtcp-fb|fmtp):)96\b", r"\g<1>111", line)
                    converted.append(line)
                cleaned = converted

                if not has_bandwidth:
                    insert_at = 1 if cleaned and cleaned[0].startswith("c=") else 0
                    cleaned.insert(insert_at, "b=AS:128")
                if not has_rtcp_rsize:
                    try:
                        idx = cleaned.index("a=rtcp-mux") + 1
                    except ValueError:
                        idx = len(cleaned)
                    cleaned.insert(idx, "a=rtcp-rsize")
                if not any(line == "a=rtcp-fb:111 transport-cc" for line in cleaned):
                    cleaned.append("a=rtcp-fb:111 transport-cc")
                if not any(line.startswith("a=fmtp:111 ") for line in cleaned):
                    cleaned.append("a=fmtp:111 minptime=10;useinbandfec=1")

            elif media.startswith("m=video "):

                media = "m=video 9 UDP/TLS/RTP/SAVPF 109 114"
                converted = []
                for line in cleaned:
                    if re.match(r"a=(?:rtpmap|rtcp-fb|fmtp):(99|100)\b", line):
                        continue
                    line = re.sub(r"^(a=(?:rtpmap|rtcp-fb|fmtp):)101\b", r"\g<1>109", line)
                    line = re.sub(r"^(a=(?:rtpmap|rtcp-fb|fmtp):)102\b", r"\g<1>114", line)
                    line = re.sub(r"\bapt=101\b", "apt=109", line)
                    converted.append(line)
                cleaned = converted

                if not any(line == "a=rtpmap:109 H264/90000" for line in cleaned):
                    cleaned.append("a=rtpmap:109 H264/90000")
                if not any(line.startswith("a=fmtp:109 ") for line in cleaned):
                    cleaned.append(
                        "a=fmtp:109 level-asymmetry-allowed=1;"
                        "packetization-mode=1;profile-level-id=42e01f"
                    )
                if not any(line == "a=rtpmap:114 rtx/90000" for line in cleaned):
                    cleaned.append("a=rtpmap:114 rtx/90000")
                if not any(line == "a=fmtp:114 apt=109" for line in cleaned):
                    cleaned.append("a=fmtp:114 apt=109")
                if not has_bandwidth:
                    insert_at = 1 if cleaned and cleaned[0].startswith("c=") else 0
                    cleaned.insert(insert_at, "b=AS:4000")
                if not has_rtcp_rsize:
                    try:
                        idx = cleaned.index("a=rtcp-mux") + 1
                    except ValueError:
                        idx = len(cleaned)
                    cleaned.insert(idx, "a=rtcp-rsize")

            elif media.startswith("m=application "):
                media = (
                    "m=application 9 UDP/DTLS/SCTP "
                    "webrtc-datachannel"
                )
                converted = []
                for line in cleaned:
                    if line.startswith("a=sctpmap:"):
                        converted.append("a=sctp-port:5000")
                    else:
                        converted.append(line)
                cleaned = converted
                if not any(line.startswith("a=sctp-port:") for line in cleaned):
                    cleaned.append("a=sctp-port:5000")

            if not has_trickle:
                try:
                    idx = next(
                        i + 1
                        for i, line in enumerate(cleaned)
                        if line.startswith("a=ice-pwd:")
                    )
                except StopIteration:
                    idx = len(cleaned)
                cleaned.insert(idx, "a=ice-options:trickle")

            output.append("\r\n".join([media] + cleaned))

        return "\r\n".join(output) + "\r\n"

    @staticmethod
    def _from_root_answer_sdp(sdp: str) -> str:

        normalized = sdp.replace("\r\n", "\n").replace("\r", "\n")
        sections = re.split(r"(?=^m=)", normalized, flags=re.MULTILINE)
        output = [sections[0].rstrip("\r\n")]

        for section in sections[1:]:
            lines = section.replace("\r\n", "\n").splitlines()
            if not lines:
                continue

            media = lines[0]
            body = lines[1:]

            if media.startswith("m=audio "):
                media = re.sub(
                    r"^m=audio (\S+) (\S+) .*$",
                    r"m=audio \1 \2 96",
                    media,
                )
                body = [
                    re.sub(
                        r"^(a=(?:rtpmap|rtcp-fb|fmtp):)111\b",
                        r"\g<1>96",
                        line,
                    )
                    for line in body
                ]

            elif media.startswith("m=video "):
                media = re.sub(
                    r"^m=video (\S+) (\S+) .*$",
                    r"m=video \1 \2 101 102",
                    media,
                )
                converted = []
                for line in body:
                    line = re.sub(
                        r"^(a=(?:rtpmap|rtcp-fb|fmtp):)109\b",
                        r"\g<1>101",
                        line,
                    )
                    line = re.sub(
                        r"^(a=(?:rtpmap|rtcp-fb|fmtp):)114\b",
                        r"\g<1>102",
                        line,
                    )
                    line = re.sub(r"\bapt=109\b", "apt=101", line)
                    converted.append(line)
                body = converted

            elif media.startswith("m=application "):
                parts = media.split()
                port = parts[1] if len(parts) > 1 else "9"
                media = f"m=application {port} DTLS/SCTP 5000"
                converted = []
                for line in body:
                    if line.startswith("a=sctp-port:"):
                        converted.append(
                            "a=sctpmap:5000 webrtc-datachannel 65535"
                        )
                    else:
                        converted.append(line)
                body = converted

            output.append("\r\n".join([media] + body))

        return "\r\n".join(output) + "\r\n"

    def _prepare_browser_offer_sdp(self, sdp: str) -> str:
        session = self.active_session
        if session is None:
            return sdp

        def kbps(value: int, fallback: int) -> int:
            if not value:
                return fallback

            return max(1, int(value) // 1000)

        bandwidth_by_mid = {
            "0": kbps(session.audio_bandwidth, 128),
            "1": kbps(session.video_bandwidth, 4000),
            "2": kbps(session.screen_bandwidth, 4000),
            "3": kbps(session.screen_audio_bandwidth, 128),
        }

        normalized = sdp.replace("\r\n", "\n").replace("\r", "\n")
        sections = re.split(r"(?=^m=)", normalized, flags=re.MULTILINE)
        output = [sections[0].rstrip("\n")]

        for section in sections[1:]:
            lines = section.splitlines()
            if not lines:
                continue

            mid = None
            for line in lines:
                if line.startswith("a=mid:"):
                    mid = line[6:].strip()
                    break

            lines = [line for line in lines if not line.startswith("b=AS:")]

            bandwidth = bandwidth_by_mid.get(mid)
            if bandwidth is not None:
                insert_at = 1
                if len(lines) > 1 and lines[1].startswith("c="):
                    insert_at = 2
                lines.insert(insert_at, f"b=AS:{bandwidth}")

            output.append("\r\n".join(lines))

        return "\r\n".join(output) + "\r\n"

    @staticmethod
    def _read_audio_bytes(source: str, max_bytes: int = 100 * 1024 * 1024):
        parsed = urlparse(source)

        if parsed.scheme in ("http", "https"):
            request = urllib.request.Request(
                source,
                headers={"User-Agent": "rootpy"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = (
                    response.headers.get_content_type()
                    or "application/octet-stream"
                )
                data = response.read(max_bytes + 1)
        else:
            if parsed.scheme == "file":
                path = Path(
                    urllib.request.url2pathname(parsed.path)
                )
            else:
                path = Path(source)
            data = path.read_bytes()
            content_type = "application/octet-stream"

        if len(data) > max_bytes:
            raise ValueError(
                "Audio source is larger than the 100 MiB playback limit"
            )

        return data, content_type

    async def _ensure_browser_voice(self, ice: IceInfo):
        if self._browser_page is not None:
            return self._browser_page

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            from ..media_bootstrap import MediaDependencyMissing

            raise MediaDependencyMissing(
                "Playwright",
                hint=(
                    'pip install "rootpy[voice]" '
                    "&& python -m playwright install chromium"
                ),
            ) from exc

        self._playwright = await async_playwright().start()

        launch_args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]

        try:
            self._browser = await self._playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=launch_args,
            )
        except Exception:
            # Chrome channel unavailable; fall back to Playwright's bundled
            # Chromium. If that is not downloaded either, the error below
            # names the one-off command to fetch it.
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=launch_args,
            )

        self._browser_context = await self._browser.new_context()
        self._browser_page = await self._browser_context.new_page()
        await self._browser_page.set_content(
            """
<!doctype html>
<html>
<body>
<script>
(() => {
  let pc = null;
  let audioContext = null;
  let microphoneDestination = null;
  let screenAudioDestination = null;
  let currentSource = null;
  let cameraStream = null;
  let screenStream = null;

  function makeVideoTrack(label) {
    const canvas = document.createElement("canvas");
    canvas.width = 16;
    canvas.height = 16;
    const context = canvas.getContext("2d");
    context.fillStyle = "black";
    context.fillRect(0, 0, canvas.width, canvas.height);
    const stream = canvas.captureStream(1);
    const track = stream.getVideoTracks()[0];
    Object.defineProperty(track, "label", { value: label });
    return { stream, track };
  }

  window.rootVoice = {
    async createOffer(iceServers) {
      if (pc) {
        try { pc.close(); } catch (_) {}
      }

      pc = new RTCPeerConnection({
        iceServers,
        bundlePolicy: "max-bundle",
        rtcpMuxPolicy: "require",
      });

      audioContext = new AudioContext({ sampleRate: 48000 });
      await audioContext.resume();

      microphoneDestination =
        audioContext.createMediaStreamDestination();
      screenAudioDestination =
        audioContext.createMediaStreamDestination();

      const camera = makeVideoTrack("camera");
      const screen = makeVideoTrack("screen");
      cameraStream = camera.stream;
      screenStream = screen.stream;

      const microphoneTrack =
        microphoneDestination.stream.getAudioTracks()[0];
      const screenAudioTrack =
        screenAudioDestination.stream.getAudioTracks()[0];

      // Root's desktop offer uses streamless tracks (a=msid:- <track-id>).
      const microphoneSender = pc.addTrack(microphoneTrack);
      const cameraSender = pc.addTrack(camera.track);
      const screenSender = pc.addTrack(screen.track);
      const screenAudioSender = pc.addTrack(screenAudioTrack);

      const capabilities = {
        audio: RTCRtpSender.getCapabilities("audio"),
        video: RTCRtpSender.getCapabilities("video"),
      };

      const opus = capabilities.audio.codecs.filter(
        codec => codec.mimeType.toLowerCase() === "audio/opus"
      );
      const h264 = capabilities.video.codecs.filter(codec => {
        if (codec.mimeType.toLowerCase() !== "video/h264") {
          return false;
        }
        const params = codec.sdpFmtpLine || "";
        return (
          params.includes("profile-level-id=42e01f") ||
          params.includes("profile-level-id=42001f") ||
          !params.includes("profile-level-id=")
        );
      });

      for (const transceiver of pc.getTransceivers()) {
        if (
          (transceiver.sender === microphoneSender ||
           transceiver.sender === screenAudioSender) &&
          opus.length
        ) {
          transceiver.setCodecPreferences(opus);
        }
        if (
          (transceiver.sender === cameraSender ||
           transceiver.sender === screenSender) &&
          h264.length
        ) {
          transceiver.setCodecPreferences(h264);
        }
      }

      pc.createDataChannel("root");

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      return pc.localDescription.sdp;
    },

    async setAnswer(type, sdp) {
      if (!pc) {
        throw new Error("Peer connection has not been created");
      }
      await pc.setRemoteDescription({ type, sdp });
      return {
        connectionState: pc.connectionState,
        iceConnectionState: pc.iceConnectionState,
      };
    },

    async play(base64Data, loop) {
      if (!audioContext || !microphoneDestination) {
        throw new Error("Voice connection is not initialized");
      }

      if (currentSource) {
        try { currentSource.stop(); } catch (_) {}
        currentSource.disconnect();
        currentSource = null;
      }

      const binary = atob(base64Data);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index++) {
        bytes[index] = binary.charCodeAt(index);
      }

      const audioBuffer = await audioContext.decodeAudioData(
        bytes.buffer.slice(0)
      );
      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.loop = Boolean(loop);
      source.connect(microphoneDestination);
      source.start();
      currentSource = source;

      source.onended = () => {
        if (currentSource === source) {
          currentSource = null;
        }
      };

      return {
        duration: audioBuffer.duration,
        sampleRate: audioBuffer.sampleRate,
        channels: audioBuffer.numberOfChannels,
      };
    },

    async stop() {
      if (currentSource) {
        try { currentSource.stop(); } catch (_) {}
        currentSource.disconnect();
        currentSource = null;
      }
    },

    async close() {
      await this.stop();
      if (pc) {
        pc.close();
        pc = null;
      }
      if (cameraStream) {
        cameraStream.getTracks().forEach(track => track.stop());
        cameraStream = null;
      }
      if (screenStream) {
        screenStream.getTracks().forEach(track => track.stop());
        screenStream = null;
      }
      if (audioContext) {
        await audioContext.close();
        audioContext = null;
      }
      microphoneDestination = null;
      screenAudioDestination = null;
    },
  };
})();
</script>
</body>
</html>
"""
        )
        return self._browser_page

    async def play_audio(
        self,
        source: Union[str, MessageAttachment],
        *,
        loop: bool = False,
    ) -> AudioPlayback:

        if self.active_session is None:
            raise RuntimeError("Join or create a call before playing audio")

        source = self._validate_audio_source(source)

        page = self._browser_page
        if page is not None and self._peer_connection is not None:
            await self.stop_audio(close_peer=False)

            data, _ = await asyncio.to_thread(
                self._read_audio_bytes,
                source,
            )
            encoded = base64.b64encode(data).decode("ascii")
            await page.evaluate(
                """async ({data, loop}) => {
                    return await window.rootVoice.play(data, loop);
                }""",
                {"data": encoded, "loop": loop},
            )

            previous = self._audio_playback
            self._media_player = source
            self._peer_connection = page
            self._audio_playback = AudioPlayback(
                source=source,
                track_id=(
                    previous.track_id
                    if previous is not None
                    else ""
                ),
                mid=(
                    previous.mid
                    if previous is not None
                    else "0"
                ),
            )
            return self._audio_playback

        await self.stop_audio(close_peer=True)

        ice = await self.get_ice_info()
        ice_servers = []
        if ice.urls:
            server = {"urls": list(ice.urls)}
            if ice.username:
                server["username"] = ice.username
            if ice.credentials:
                server["credential"] = ice.credentials
            ice_servers.append(server)

        page = await self._ensure_browser_voice(ice)
        local_sdp = await page.evaluate(
            "(servers) => window.rootVoice.createOffer(servers)",
            ice_servers,
        )
        local_sdp = self._prepare_browser_offer_sdp(local_sdp)

        ice_ufrags = {
            line.split(":", 1)[1].strip()
            for line in local_sdp.splitlines()
            if line.startswith("a=ice-ufrag:")
        }
        if len(ice_ufrags) != 1:
            raise RuntimeError(
                "Chromium did not create Root's max-bundle transport; "
                f"got {len(ice_ufrags)} ICE usernames"
            )

        required = (
            "a=group:BUNDLE 0 1 2 3 4",
            "a=extmap-allow-mixed",
            "m=audio 9 UDP/TLS/RTP/SAVPF",
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
            "b=AS:128",
            "b=AS:4000",
        )
        missing = [item for item in required if item not in local_sdp]
        if missing:
            raise RuntimeError(
                "Chromium offer does not match Root's expected layout; "
                "missing " + ", ".join(repr(item) for item in missing)
            )

        # This used to write ~/Desktop/rootpy_offer.sdp and print two lines to
        # stdout on *every* play_audio call -- leftover scaffolding from
        # working out Root's SDP layout. It dropped an unrequested file in the
        # user's home directory, hard-failed on any host without a Desktop
        # folder (write_text does not mkdir -- containers, headless servers,
        # most Linux setups), and corrupted the stdout of any CLI or piped
        # program that played audio. It is a log line now.
        #
        #     logging.getLogger("rootpy.calls").setLevel(logging.DEBUG)
        media_lines = [
            line
            for line in local_sdp.splitlines()
            if line.startswith("m=")
        ]
        log.debug("Chromium SDP media lines: %s", media_lines)
        log.debug("Chromium offer SDP:\n%s", local_sdp)

        microphone_mid = "0"
        sections = re.split(r"(?=^m=)", local_sdp, flags=re.MULTILINE)
        for section in sections:
            if not section.startswith("m=audio "):
                continue
            match = re.search(r"^a=mid:(.+)$", section, flags=re.MULTILINE)
            if match:
                microphone_mid = match.group(1).strip()
                break

        target_container_id, target_community_id = self.require_active_target()
        await self.create_session(
            target_container_id,
            community_id=target_community_id,
        )

        root_track_id = str(uuid4())
        try:
            parsed = await self.tracks_create(
                track_id=root_track_id,
                mid=microphone_mid,
                local_description_type="offer",
                local_sdp=local_sdp,
            )
        except RuntimeError as exc:
            if "TracksCreate error session_error" not in str(exc):
                raise
            await self.create_session(
                target_container_id,
                community_id=target_community_id,
            )
            root_track_id = str(uuid4())
            parsed = await self.tracks_create(
                track_id=root_track_id,
                mid=microphone_mid,
                local_description_type="offer",
                local_sdp=local_sdp,
            )

        await page.evaluate(
            """async ({type, sdp}) => {
                return await window.rootVoice.setAnswer(type, sdp);
            }""",
            {
                "type": parsed["description_type"] or "answer",
                "sdp": parsed["sdp"],
            },
        )

        data, _ = await asyncio.to_thread(
            self._read_audio_bytes,
            source,
        )
        encoded = base64.b64encode(data).decode("ascii")
        await page.evaluate(
            """async ({data, loop}) => {
                return await window.rootVoice.play(data, loop);
            }""",
            {"data": encoded, "loop": loop},
        )

        self._peer_connection = page
        self._media_player = source
        self._audio_playback = AudioPlayback(
            source=source,
            track_id=root_track_id,
            mid=microphone_mid,
        )
        return self._audio_playback

    async def stop_audio(self, *, close_peer: bool = True) -> None:
        page = self._browser_page
        self._media_player = None
        self._audio_playback = None
        if close_peer:
            self._peer_connection = None

        if page is not None:
            try:
                if close_peer:
                    await page.evaluate(
                        "() => window.rootVoice.close()"
                    )
                else:
                    await page.evaluate(
                        "() => window.rootVoice.stop()"
                    )
            except Exception:
                pass

        if close_peer:
            if self._browser_context is not None:
                try:
                    await self._browser_context.close()
                except Exception:
                    pass
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass

            self._browser_page = None
            self._browser_context = None
            self._browser = None
            self._playwright = None

    @property
    def audio_playback(self) -> Optional[AudioPlayback]:
        return self._audio_playback
