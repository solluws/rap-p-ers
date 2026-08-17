from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .generated_rpc_registry import MESSAGE_SCHEMAS, RPC_SERVICES
from .protocol import grpc_frame
from .transport import GrpcWebTransport


@dataclass(frozen=True)
class RawRpcResult:
    service: str
    method: str
    request_type: str
    response_type: str
    body: bytes
    http_status: int


class RawMethod:
    def __init__(self, api, service_name: str, method_name: str) -> None:
        self._api = api
        self.service_name = service_name
        self.method_name = method_name

    @property
    def info(self) -> dict:
        return dict(
            RPC_SERVICES[self.service_name][self.method_name]
        )

    @property
    def request_schema(self) -> Optional[dict]:
        request_name = self.info["request"]

        direct = MESSAGE_SCHEMAS.get(request_name)
        if direct is not None:
            return direct
        suffix = "." + request_name
        matches = [
            schema
            for name, schema in MESSAGE_SCHEMAS.items()
            if name.endswith(suffix)
        ]
        return matches[0] if len(matches) == 1 else None

    async def __call__(
        self,
        payload: bytes = b"",
        *,
        already_framed: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ) -> RawRpcResult:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError(
                "Raw RPC payload must be bytes. "
                "Use .request_schema to inspect the request fields."
            )

        info = self.info
        if info["method_type"] != "Unary":
            raise NotImplementedError(
                "Only unary RPC invocation is implemented by RawAPI; "
                f"{self.service_name}/{self.method_name} is "
                f"{info['method_type']}."
            )

        request_body = bytes(payload)
        if not already_framed:
            request_body = grpc_frame(request_body)

        response = await self._api._transport.unary(
            endpoint=info["endpoint"],
            body=request_body,
            headers=self._api._headers(
                info["endpoint"],
                headers,
            ),
            operation=(
                f"{self.service_name}/{self.method_name}"
            ),
        )

        return RawRpcResult(
            service=self.service_name,
            method=self.method_name,
            request_type=info["request"],
            response_type=info["response"],
            body=response.content,
            http_status=response.status_code,
        )

    def __repr__(self) -> str:
        return (
            f"<RawMethod {self.service_name}/"
            f"{self.method_name}>"
        )


class RawService:
    def __init__(self, api, service_name: str) -> None:
        self._api = api
        self.service_name = service_name

    def methods(self) -> List[str]:
        return sorted(RPC_SERVICES[self.service_name])

    def __getattr__(self, name: str) -> RawMethod:
        methods = RPC_SERVICES[self.service_name]

        if name in methods:
            return RawMethod(self._api, self.service_name, name)

        folded = name.replace("_", "").casefold()
        for method_name in methods:
            if method_name.replace("_", "").casefold() == folded:
                return RawMethod(
                    self._api,
                    self.service_name,
                    method_name,
                )

        raise AttributeError(
            f"{self.service_name!r} has no RPC {name!r}"
        )


class RawAPI:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter,
    ) -> None:
        self._transport = transport
        self._token_getter = token_getter

    def services(self) -> List[str]:
        return sorted(RPC_SERVICES)

    def describe(
        self,
        service: str,
        method: Optional[str] = None,
    ):
        service_name = self._resolve_service(service)
        if method is None:
            return {
                name: dict(info)
                for name, info in RPC_SERVICES[
                    service_name
                ].items()
            }
        raw_method = getattr(
            RawService(self, service_name),
            method,
        )
        info = raw_method.info
        info["request_schema"] = raw_method.request_schema
        return info

    def _resolve_service(self, value: str) -> str:
        if value in RPC_SERVICES:
            return value

        folded = value.replace("_", "").replace(".", "").casefold()
        matches = []
        for service_name in RPC_SERVICES:
            simple = service_name.rsplit(".", 1)[-1]
            if simple.endswith("GrpcService"):
                simple = simple[:-11]
            if (
                simple.replace("_", "").casefold() == folded
                or service_name.replace(".", "").replace("_", "").casefold()
                == folded
            ):
                matches.append(service_name)

        if len(matches) == 1:
            return matches[0]

        raise AttributeError(
            f"Unknown or ambiguous Root service: {value!r}"
        )

    def __getattr__(self, name: str) -> RawService:
        service_name = self._resolve_service(name)
        return RawService(self, service_name)

    def _headers(
        self,
        endpoint: str,
        extra: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        headers = {
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "content-type": "application/grpc-web",
            "accept": "application/grpc-web",
        }

        token = self._token_getter()
        if token:
            headers["authorization"] = f"Bearer {token}"

        if "root.WebRtcGrpcService/" in endpoint:
            headers.update({
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36 "
                    "RootPlatform 1.0 v0.9.126 default"
                ),
                "content-type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "accept": "*/*",
            })
        else:
            headers["user-agent"] = (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            )

        if extra:
            headers.update(extra)

        return headers
