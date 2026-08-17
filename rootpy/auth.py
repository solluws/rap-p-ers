from __future__ import annotations

import base64
import platform
from typing import Optional

from .exceptions import (
    AccountAlreadyExists,
    EmailAlreadyExists,
    GrpcWebError,
    UsernameAlreadyExists,
)

from .identifiers import create_desktop_device_guid, encode_root_guid
from .models import AuthenticationSession
from .protocol import bool_field, grpc_frame, iter_fields, iter_grpc_web_frames, length_field, string_field
from .transport import GrpcWebTransport

PASSWORD_SIGNIN = "https://connect.rootapp.com/connect.ConnectService/PasswordSignIn"
PASSWORD_SIGNUP = "https://connect.rootapp.com/connect.ConnectService/PasswordSignUp"
GET_HUB = "https://api.rootapp.com/root.UserGrpcService/GetNewHubserverEndpoint"
DEFAULT_WEB_API_URL = "https://api.rootapp.com/"


class AuthClient:
    def __init__(self, transport: GrpcWebTransport) -> None:
        self.transport = transport

    @staticmethod
    def _device_description() -> bytes:
        payload = bytearray()
        payload += string_field(1, platform.node() or "Python Root Extension")
        payload += string_field(2, "Windows")
        payload += string_field(3, "0.9.126.0")
        payload += string_field(4, platform.version())
        payload += bool_field(5, False)
        return bytes(payload)

    @staticmethod
    def _parse_auth(body: bytes) -> tuple[str, str, str]:
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for field_number, wire_type, value in iter_fields(frame):
                if field_number != 1 or wire_type != 2:
                    continue
                token = hub = api = ""
                for n, wt, v in iter_fields(value):
                    if wt != 2:
                        continue
                    decoded = v.decode("utf-8")
                    if n == 1:
                        token = decoded
                    elif n == 2:
                        hub = decoded
                    elif n == 3:
                        api = decoded
                return token, hub, api
        raise RuntimeError("PasswordSignIn returned no authentication result")

    @staticmethod
    def _parse_hub(body: bytes) -> str:
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            for n, wt, value in iter_fields(frame):
                if n == 10 and wt == 2:
                    return value.decode("utf-8")
        raise RuntimeError("GetNewHubserverEndpoint returned no HubServerInfo")

    @staticmethod
    def _connect_headers() -> dict[str, str]:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "content-type": "application/grpc-web",
        }

    async def _session_from_auth(
        self,
        *,
        body: bytes,
        device_id: str,
        operation: str,
    ) -> AuthenticationSession:
        token, hub_url, web_api_url = self._parse_auth(body)

        if not hub_url.startswith(("ws://", "wss://")):
            hub_response = await self.transport.unary(
                endpoint=GET_HUB,
                body=grpc_frame(b""),
                headers={
                    **self._connect_headers(),
                    "grpc-timeout": "10S",
                    "authorization": f"Bearer {token}",
                },
                operation="GetNewHubserverEndpoint",
            )
            hub_url = self._parse_hub(hub_response.content)

        if not token:
            raise RuntimeError(f"{operation} returned an empty client token")
        if not web_api_url:
            raise RuntimeError(f"{operation} returned an empty WebApiUrl")

        return AuthenticationSession(
            token,
            device_id,
            hub_url,
            web_api_url,
        )

    @staticmethod
    def _signup_conflict_from_error(
        exc: GrpcWebError,
        username: str,
        email: str,
    ) -> AccountAlreadyExists:
        # Prefer Root's structured error payload: it names the conflicting
        # field directly (username vs email) instead of guessing.
        payload_kind = getattr(exc, "payload_kind", None)
        if payload_kind == "username":
            return UsernameAlreadyExists(username)
        if payload_kind == "email":
            return EmailAlreadyExists(email)

        # Fallback for older servers / undecodable payloads: substring match on
        # the raw exception bytes.
        encoded = exc.response_headers.get("root-exception-bin", "")
        decoded = b""

        if encoded:
            try:
                decoded = base64.b64decode(encoded)
            except Exception:
                decoded = b""

        username_bytes = username.encode("utf-8")
        email_bytes = email.encode("utf-8")

        if username_bytes and username_bytes in decoded:
            return UsernameAlreadyExists(username)

        if email_bytes and email_bytes in decoded:
            return EmailAlreadyExists(email)

        return AccountAlreadyExists()


    async def session_from_token(
        self,
        token: str,
        *,
        device_id: Optional[str] = None,
        web_api_url: str = DEFAULT_WEB_API_URL,
    ) -> AuthenticationSession:
        """Build an authenticated session from an existing client token.

        This does not perform PasswordSignIn.  It asks Root for the current
        Hub endpoint, then uses the supplied token for normal authenticated
        API calls.
        """
        if not isinstance(token, str):
            raise TypeError("token must be a string")

        token = token.strip()
        if not token:
            raise ValueError("token cannot be empty")

        if device_id is None:
            device_id = create_desktop_device_guid()

        response = await self.transport.unary(
            endpoint=GET_HUB,
            body=grpc_frame(b""),
            headers={
                **self._connect_headers(),
                "grpc-timeout": "10S",
                "authorization": f"Bearer {token}",
                "x-root-Device-Id": device_id,
            },
            operation="GetNewHubserverEndpoint",
        )
        hub_url = self._parse_hub(response.content)

        return AuthenticationSession(
            token=token,
            device_id=device_id,
            hub_url=hub_url,
            web_api_url=web_api_url,
        )

    async def signup(
        self,
        username: str,
        password: str,
        email: str,
        *,
        access_token: Optional[str] = None,
    ) -> AuthenticationSession:
        if not username:
            raise ValueError("Username is required")
        if not password:
            raise ValueError("Password is required")
        if not email:
            raise ValueError("Email is required")

        device_id = create_desktop_device_guid()
        payload = bytearray()
        payload += string_field(1, username)
        payload += string_field(2, password)
        payload += length_field(4, self._device_description())
        payload += length_field(5, encode_root_guid(device_id))
        payload += string_field(6, email)
        if access_token:
            payload += string_field(7, access_token)

        try:
            response = await self.transport.unary(
                endpoint=PASSWORD_SIGNUP,
                body=grpc_frame(bytes(payload)),
                headers=self._connect_headers(),
                operation="PasswordSignUp",
            )
        except GrpcWebError as exc:

            if exc.status == "6":
                raise self._signup_conflict_from_error(
                    exc,
                    username,
                    email,
                ) from exc
            raise

        return await self._session_from_auth(
            body=response.content,
            device_id=device_id,
            operation="PasswordSignUp",
        )

    async def login(self, username: str, password: str) -> AuthenticationSession:
        device_id = create_desktop_device_guid()
        payload = bytearray()
        payload += string_field(1, username)
        payload += string_field(2, password)
        payload += length_field(3, self._device_description())
        payload += length_field(5, encode_root_guid(device_id))
        response = await self.transport.unary(
            endpoint=PASSWORD_SIGNIN,
            body=grpc_frame(bytes(payload)),
            headers=self._connect_headers(),
            operation="PasswordSignIn",
        )
        return await self._session_from_auth(
            body=response.content,
            device_id=device_id,
            operation="PasswordSignIn",
        )
