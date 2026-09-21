from __future__ import annotations

import base64
import platform
from typing import Optional

from .exceptions import (
    AccountAlreadyExists,
    EmailAlreadyExists,
    GrpcStatus,
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
        fetch_hub: bool = True,
    ) -> AuthenticationSession:
        """Build an authenticated session from an existing client token.

        This does not perform PasswordSignIn.  It asks Root for the current
        Hub endpoint, then uses the supplied token for normal authenticated
        API calls.

        ``fetch_hub=False`` leaves ``hub_url`` empty and skips that request;
        :meth:`RootClient.connect` fills it in on demand. Worth it only for a
        caller bringing up many accounts that may never open a websocket.
        """
        if not isinstance(token, str):
            raise TypeError("token must be a string")

        token = token.strip()
        if not token:
            raise ValueError("token cannot be empty")

        if device_id is None:
            device_id = create_desktop_device_guid()

        # The hub endpoint is only ever used to open a websocket, and it is a
        # round trip, worth roughly half of a typical login's latency.
        # A caller that may never connect can skip it here and let
        # :meth:`RootClient.connect` fetch it if it turns out to need one. The
        # token is still validated either way: GetSelf runs next.
        hub_url = (
            await self.hub_endpoint(token, device_id) if fetch_hub else ""
        )

        return AuthenticationSession(
            token=token,
            device_id=device_id,
            hub_url=hub_url,
            web_api_url=web_api_url,
        )

    async def hub_endpoint(self, token: str, device_id: str) -> str:
        """The websocket endpoint for this account's hub connection.

        Split out of :meth:`session_from_token` so a session built with
        ``fetch_hub=False`` can fill it in later, at the point something
        actually wants a socket.
        """
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
        return self._parse_hub(response.content)

    async def signup(
        self,
        username: str,
        password: str,
        email: str,
        *,
        access_token: Optional[str] = None,
        turnstile_token: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> AuthenticationSession:
        if not username:
            raise ValueError("Username is required")
        if not password:
            raise ValueError("Password is required")
        if not email:
            raise ValueError("Email is required")

        # The device id MUST be the same across a challenge retry. Root binds
        # the Turnstile challenge to the request it was issued for, and a new
        # device id makes the retry look like a brand-new signup -- so the
        # server mints a fresh challenge and the token you just solved is
        # never checked. Callers pass the id they used the first time.
        device_id = device_id or create_desktop_device_guid()
        # This endpoint is connect.ConnectService/PasswordSignUp -- a DIFFERENT
        # service from root.UserGrpcService/SignUp, with its own field numbers.
        # (Applying the other service's numbering here produces
        # INVALID_ARGUMENT, because none of the fields line up.)
        # PasswordSignUpRequest (connect.ConnectService):
        #   1 username, 2 password, 3 TURNSTILE token, 4 device description,
        #   5 device id, 6 email, 7 access token.
        # 3 and 7 are DIFFERENT fields -- sending the captcha token as 7 means
        # the server never sees a turnstile token and keeps issuing challenges,
        # which looks exactly like the token being rejected.
        payload = bytearray()
        payload += string_field(1, username)
        payload += string_field(2, password)
        if turnstile_token:
            payload += string_field(3, turnstile_token)
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

            # ``exc.status`` is a ``GrpcStatus`` IntEnum -- compare against the
            # enum, not against a string, or this branch silently never fires
            # and a taken username or email raises the bare
            # ``GrpcAlreadyExists`` instead of the typed
            # ``UsernameAlreadyExists``/``EmailAlreadyExists`` that
            # ``__init__`` exports and ``AccountCreator`` catches.
            if exc.status == GrpcStatus.ALREADY_EXISTS:
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
