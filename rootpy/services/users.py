from __future__ import annotations

from ..validation import validate_username
from typing import Optional

from collections.abc import Callable

from ..identifiers import (
    create_command_idempotency_guid,
    encode_root_guid_parts,
)
from ..models import CurrentUser
from ..identifiers import encode_root_guid, normalize_root_guid
from ..protocol import (
    decode_root_guid_message,
    unwrap_grpc_web,
    encode_varint,
    field_key,
    grpc_frame,
    length_field,
    iter_fields,
    iter_grpc_web_frames,
)
from ..transport import GrpcWebTransport

GET_SELF = "https://api.rootapp.com/root.UserGrpcService/GetSelf"
SET_USERNAME = "https://api.rootapp.com/root.UserGrpcService/SetUsername"
SET_PROFILE_PICTURE = (
    "https://api.rootapp.com/root.UserGrpcService/SetProfilePicture"
)
SET_DESCRIPTION = (
    "https://api.rootapp.com/root.UserGrpcService/SetDescription"
)
SET_BANNER = "https://api.rootapp.com/root.UserGrpcService/SetBanner"
GET_EXTENDED_USERS_BY_ID = (
    "https://api.rootapp.com/root.UserGrpcService/GetExtendedUsersById"
)
SET_MAX_ONLINE_STATUS = (
    "https://api.rootapp.com/root.UserGrpcService/SetMaxOnlineStatus"
)
SET_DEVICE_ONLINE_STATUS = (
    "https://api.rootapp.com/root.UserGrpcService/SetDeviceOnlineStatus"
)
SET_USER_DEFINED_STATUS = (
    "https://api.rootapp.com/"
    "root.UserGrpcService/SetUserDefinedStatus"
)


def _decode_string_wrapper(data: bytes) -> Optional[str]:
    for number, wire_type, value in iter_fields(data):
        if number == 1 and wire_type == 2:
            return value.decode("utf-8", errors="replace")
    return None


def _unwrap_string(data: bytes):
    """google.protobuf.StringValue -> str (field 1), or the raw text."""
    for number, wire_type, value in iter_fields(data):
        if number == 1 and wire_type == 2:
            return value.decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class UserService:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter: Callable[[], str],
        asset_service=None,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self.asset_service = asset_service

    def _headers(self) -> dict:
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
        }

    @staticmethod
    def _context() -> bytes:
        high64, low64 = create_command_idempotency_guid()
        return length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )

    @staticmethod
    def _string_wrapper(value: str) -> bytes:
        return length_field(1, value.encode("utf-8"))

    async def _set_wrapped_string(
        self,
        endpoint: str,
        operation: str,
        value: Optional[str],
    ):
        payload = bytearray()
        payload += length_field(1, self._context())
        if value is not None:
            payload += length_field(
                10,
                self._string_wrapper(value),
            )
        return await self.transport.unary(
            endpoint=endpoint,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation=operation,
        )

    async def set_username(self, username: str) -> None:
        if not isinstance(username, str):
            raise TypeError("username must be a string")
        if not username:
            raise ValueError("username cannot be empty")
        # Root's rule, checked before the round trip. Without this a bad
        # username comes back as a generic INVALID_ARGUMENT that names the
        # field but not which part of the rule was broken.
        validate_username(username)

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += length_field(10, username.encode("utf-8"))

        await self.transport.unary(
            endpoint=SET_USERNAME,
            body=grpc_frame(bytes(payload)),
            headers=self._headers(),
            operation="UserSetUsername",
        )

    async def set_description(
        self,
        description: Optional[str],
    ) -> None:
        await self._set_wrapped_string(
            SET_DESCRIPTION,
            "UserSetDescription",
            description,
        )

    async def set_status(
        self,
        status: Optional[str],
    ) -> None:
        await self._set_wrapped_string(
            SET_USER_DEFINED_STATUS,
            "UserSetUserDefinedStatus",
            status,
        )

    async def _resolve_asset_source(self, source) -> Optional[str]:
        """Turn whatever the caller gave us into an asset token URI.

        Accepts:
          * a file path (str or Path)      -- uploaded
          * raw bytes                      -- uploaded
          * an http(s) URL                 -- downloaded, then uploaded
          * an existing ``asset://`` token -- passed through
          * None                           -- clears the image
        """
        if source is None:
            return None

        from pathlib import Path

        def _require_assets():
            if self.asset_service is None:
                raise RuntimeError(
                    "No AssetService is attached to UserService"
                )
            return self.asset_service

        # raw bytes
        if isinstance(source, (bytes, bytearray)):
            return await _require_assets().upload_bytes(bytes(source))

        # file-like object
        read = getattr(source, "read", None)
        if callable(read):
            data = read()
            if isinstance(data, str):
                raise TypeError("open image files in binary mode ('rb')")
            name = getattr(source, "name", "upload.png")
            return await _require_assets().upload_bytes(
                data, filename=str(name).rsplit("/", 1)[-1]
            )

        text = str(source)

        # remote image -- fetch it, then upload
        if text.startswith(("http://", "https://")):
            # Through the transport's proxy, not around it: this is the one
            # request in the profile-picture path that goes somewhere other
            # than Root, and it used to be the only one that ignored `proxy=`.
            async with self.transport.open_plain_client() as http:
                response = await http.get(text, timeout=30.0)
                response.raise_for_status()
            filename = text.rsplit("/", 1)[-1].split("?")[0] or "upload.png"
            return await _require_assets().upload_bytes(
                response.content, filename=filename
            )

        path = Path(text).expanduser()
        if path.is_file():
            return await _require_assets().upload_file(str(path))

        # already a token uri (or something the server will reject clearly)
        return text

    async def set_profile_picture(
        self,
        source: Optional[str],
    ) -> Optional[str]:
        token_uri = await self._resolve_asset_source(source)
        response = await self._set_wrapped_string(
            SET_PROFILE_PICTURE,
            "UserSetProfilePicture",
            token_uri,
        )

        for flag, frame in iter_grpc_web_frames(response.content):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 5 and wire_type == 2:
                    return value.decode(
                        "utf-8",
                        errors="replace",
                    )
            break
        return None

    async def set_banner(
        self,
        source: Optional[str],
    ) -> Optional[str]:
        token_uri = await self._resolve_asset_source(source)
        response = await self._set_wrapped_string(
            SET_BANNER,
            "UserSetBanner",
            token_uri,
        )

        for flag, frame in iter_grpc_web_frames(response.content):
            if flag & 0x80:
                continue
            for number, wire_type, value in iter_fields(frame):
                if number == 5 and wire_type == 2:
                    return _decode_string_wrapper(value)
            break
        return None

    @staticmethod
    def _parse_response(body: bytes) -> CurrentUser:
        payload = None
        for flag, frame in iter_grpc_web_frames(body):
            if not (flag & 0x80):
                payload = frame
                break

        if payload is None:
            raise RuntimeError("GetSelf returned no protobuf message")

        values: dict[int, tuple[int, object]] = {}
        for number, wire_type, value in iter_fields(payload):
            if number not in values:
                values[number] = (wire_type, value)

        def guid(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return decode_root_guid_message(item[1])

        def string(number: int) -> str:
            item = values.get(number)
            if item is None or item[0] != 2:
                return ""
            return item[1].decode("utf-8", errors="replace")

        def wrapped_string(number: int) -> Optional[str]:
            item = values.get(number)
            if item is None or item[0] != 2:
                return None
            return _decode_string_wrapper(item[1])

        def integer(number: int) -> int:
            item = values.get(number)
            if item is None or item[0] != 0:
                return 0
            return int(item[1])

        user_id = guid(10)
        if not user_id:
            raise RuntimeError("GetSelf returned no UserId")

        return CurrentUser(
            id=user_id,
            username=string(11),
            email=string(12),
            profile_picture_asset_uri=wrapped_string(13),
            is_email_verified=bool(integer(14)),
            max_online_status=integer(15),
            description=wrapped_string(21),
            banner_asset_uri=wrapped_string(22),
            user_defined_status=wrapped_string(23),
            is_billable=bool(integer(26)),
            raw=payload,
        )

    async def set_online_status(self, status) -> "UserOnlineStatus":
        """Set the account's presence (online / idle / invisible).

        Accepts a :class:`UserOnlineStatus`, its int value, or an everyday
        name like ``"online"``, ``"idle"``, ``"away"``, ``"invisible"``.

        Root has no separate do-not-disturb state -- presence is
        active / inactive / disconnected.

        Request is UserSetMaxOnlineStatusRequest: Context(1) + MaxStatus(10).
        """
        from ..enums import UserOnlineStatus

        if isinstance(status, str):
            resolved = UserOnlineStatus.from_name(status)
        else:
            resolved = UserOnlineStatus.coerce(int(status))

        payload = bytearray()
        payload += length_field(1, self._context())
        payload += field_key(10, 0) + encode_varint(int(resolved))
        await self.transport.unary(
            endpoint=SET_MAX_ONLINE_STATUS,
            body=bytes(payload),
            headers=self._headers(),
            operation="UserSetMaxOnlineStatus",
        )
        return resolved

    async def get_profiles(self, user_ids) -> dict:
        """Fetch public profiles for one or more users, in a single request.

        Returns ``{user_id: UserProfile}``. Ids the server doesn't know are
        simply absent from the result.

        Request is UserGetExtendedUsersByIdRequest: repeated UserUuid at
        field 10; the response carries user_id(10), profile_picture(11),
        username(12), online_status(13), is_deleted(14), description(16),
        banner(17) and custom status(18).
        """
        from ..models import UserProfile

        if isinstance(user_ids, str):
            user_ids = [user_ids]
        normalized = [normalize_root_guid(u) for u in user_ids if u]
        if not normalized:
            return {}

        body = bytearray()
        for user_id in normalized:
            body += length_field(10, encode_root_guid(user_id))

        response = await self.transport.unary(
            endpoint=GET_EXTENDED_USERS_BY_ID,
            body=bytes(body),
            headers=self._headers(),
            operation="UserGetExtendedUsersById",
        )

        payload = unwrap_grpc_web(response.content)
        if payload is None:
            return {}

        profiles: dict = {}
        for number, wire_type, value in iter_fields(payload):
            if wire_type != 2:
                continue
            profile = self._parse_user_profile(bytes(value))
            if profile is not None:
                profiles[profile.user_id] = profile
        return profiles

    async def get_profile(self, user_id: str):
        """Fetch one user's public profile, or None if unknown."""
        profiles = await self.get_profiles([user_id])
        return profiles.get(normalize_root_guid(user_id))

    @staticmethod
    def _parse_user_profile(data: bytes):
        from ..models import UserProfile

        values = {
            "user_id": None, "username": None, "profile_picture_uri": None,
            "banner_uri": None, "description": None, "custom_status": None,
            "online_status": 0, "is_deleted": False,
        }
        for number, wire_type, value in iter_fields(data):
            if number == 10 and wire_type == 2:
                values["user_id"] = decode_root_guid_message(value)
            elif number == 11 and wire_type == 2:
                values["profile_picture_uri"] = _unwrap_string(value)
            elif number == 12 and wire_type == 2:
                values["username"] = value.decode("utf-8", errors="replace")
            elif number == 13 and wire_type == 0:
                values["online_status"] = int(value)
            elif number == 14 and wire_type == 0:
                values["is_deleted"] = bool(value)
            elif number == 16 and wire_type == 2:
                values["description"] = _unwrap_string(value)
            elif number == 17 and wire_type == 2:
                values["banner_uri"] = _unwrap_string(value)
            elif number == 18 and wire_type == 2:
                values["custom_status"] = _unwrap_string(value)
        if not values["user_id"]:
            return None
        return UserProfile(raw=data, **values)

    async def get_self(self) -> CurrentUser:
        response = await self.transport.unary(
            endpoint=GET_SELF,
            body=grpc_frame(b""),
            headers=self._headers(),
            operation="GetSelf",
        )
        return self._parse_response(response.content)
