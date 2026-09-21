from __future__ import annotations

import re
from functools import lru_cache

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .identifiers import (
    create_command_idempotency_guid,
    encode_root_guid,
    encode_root_guid_parts,
    normalize_root_guid,
)
from .protocol import (
    bool_field,
    decode_root_guid_message,
    decode_timestamp_message,
    encode_varint,
    field_key,
    grpc_frame,
    iter_fields,
    iter_grpc_web_frames,
    length_field,
)
from .structured_registry import (
    ENUMS,
    MESSAGES,
    SERVICES,
    SIMPLE_MESSAGES,
)


# Precompiled once rather than re-imported and re-compiled on every call.
_SNAKE_BOUNDARY = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_TAIL = re.compile(r"([a-z0-9])([A-Z])")


@lru_cache(maxsize=4096)
def _snake(name: str) -> str:
    """CamelCase -> snake_case, memoised (names repeat constantly)."""
    value = _SNAKE_BOUNDARY.sub(r"\1_\2", name)
    return _SNAKE_TAIL.sub(r"\1_\2", value).lower()


@lru_cache(maxsize=4096)
def _resolve_enum_cached(
    type_name: str,
    namespace_hint: Optional[str] = None,
) -> Optional[tuple]:
    """Enum descriptor lookup, memoised, for the same reason as ``_snake``.

    A schema names a field's type by its *bare* name far more often than by
    its qualified one, and the bare path falls through to a full scan of
    ``ENUMS.items()``: 0.4 us for the qualified dict hit against roughly
    29 us for the bare-name scan, some seventy times slower -- and that scan
    runs once per enum field on both encode and decode, on the event loop,
    where it blocks every other coroutine including the gateway socket.

    Safe to cache because it is a pure function of its two arguments:
    ``ENUMS`` is a :class:`~rootpy._registry_loader.LazyRegistry`, a read-only
    ``Mapping`` with no ``__setitem__``, loaded once from ``data/enums.json``
    and never mutated anywhere in the package.
    """
    clean = type_name.rstrip("?")
    if clean in ENUMS:
        return clean, ENUMS[clean]
    simple = clean.rsplit(".", 1)[-1]
    candidates = [
        (name, members)
        for name, members in ENUMS.items()
        if name.rsplit(".", 1)[-1] == simple
    ]
    if namespace_hint:
        preferred = [
            item
            for item in candidates
            if item[0].rsplit(".", 1)[0] == namespace_hint
        ]
        if len(preferred) == 1:
            return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


class AttrDict(dict):
    """Dictionary with attribute access for decoded protobuf results."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        self[name] = value


@dataclass(frozen=True)
class EnumValue:
    enum_type: str
    value: int
    name: Optional[str]

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        if self.name:
            return self.name
        return str(self.value)


@dataclass(frozen=True)
class StructuredResult:
    service: str
    method: str
    data: Any
    raw: bytes
    request_type: str
    response_type: str
    http_status: int

    def __getattr__(self, name: str):
        data = self.data
        if isinstance(data, Mapping) and name in data:
            return data[name]
        raise AttributeError(name)


class StructuredProtoCodec:
    UUID_SUFFIXES = (
        "Uuid",
        "Guid",
    )

    INTEGER_TYPES = {
        "int",
        "int?",
        "int32",
        "uint",
        "uint?",
        "uint32",
        "long",
        "long?",
        "int64",
        "ulong",
        "ulong?",
        "uint64",
        "short",
        "ushort",
        "byte",
        "sbyte",
    }

    FLOAT_TYPES = {"float", "float?"}
    DOUBLE_TYPES = {"double", "double?"}
    BOOL_TYPES = {"bool", "bool?"}
    STRING_TYPES = {"string", "string?"}
    BYTES_TYPES = {"ByteString", "byte[]", "bytes"}

    def resolve_message(
        self,
        type_name: str,
        *,
        namespace_hint: Optional[str] = None,
    ) -> Optional[dict]:
        clean = type_name.rstrip("?")
        if clean in MESSAGES:
            return MESSAGES[clean]

        simple = clean.rsplit(".", 1)[-1]
        candidates = SIMPLE_MESSAGES.get(simple, [])
        if namespace_hint:
            preferred = [
                item
                for item in candidates
                if item.rsplit(".", 1)[0] == namespace_hint
            ]
            if len(preferred) == 1:
                return MESSAGES[preferred[0]]
        if len(candidates) == 1:
            return MESSAGES[candidates[0]]
        return None

    def resolve_enum(
        self,
        type_name: str,
        *,
        namespace_hint: Optional[str] = None,
    ) -> Optional[tuple]:
        """Look an enum descriptor up by qualified or bare name.

        Delegates to a memoised module-level function -- see
        :func:`_resolve_enum_cached` for why.
        """
        return _resolve_enum_cached(type_name, namespace_hint)

    @staticmethod
    def _context_bytes() -> bytes:
        high64, low64 = create_command_idempotency_guid()
        return length_field(
            4,
            encode_root_guid_parts(high64, low64),
        )

    @staticmethod
    def _timestamp_bytes(value) -> bytes:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = int(dt.timestamp())
            nanos = int(
                (dt.timestamp() - seconds)
                * 1_000_000_000
            )
        elif isinstance(value, (int, float)):
            seconds = int(value)
            nanos = int(
                (float(value) - seconds)
                * 1_000_000_000
            )
        elif isinstance(value, Mapping):
            seconds = int(value.get("seconds", 0))
            nanos = int(value.get("nanos", 0))
        else:
            raise TypeError(
                "Timestamp value must be datetime, Unix time, "
                "or {'seconds': ..., 'nanos': ...}"
            )

        payload = bytearray()
        if seconds:
            payload += field_key(1, 0) + encode_varint(seconds)
        if nanos:
            payload += field_key(2, 0) + encode_varint(nanos)
        return bytes(payload)

    @staticmethod
    def _duration_bytes(value) -> bytes:
        if isinstance(value, (int, float)):
            seconds = int(value)
            nanos = int(
                (float(value) - seconds)
                * 1_000_000_000
            )
        elif isinstance(value, Mapping):
            seconds = int(value.get("seconds", 0))
            nanos = int(value.get("nanos", 0))
        else:
            raise TypeError(
                "Duration value must be seconds or "
                "{'seconds': ..., 'nanos': ...}"
            )
        payload = bytearray()
        if seconds:
            payload += field_key(1, 0) + encode_varint(seconds)
        if nanos:
            payload += field_key(2, 0) + encode_varint(nanos)
        return bytes(payload)

    def _enum_number(
        self,
        type_name: str,
        value,
        namespace_hint: Optional[str],
    ) -> int:
        if isinstance(value, IntEnum):
            return int(value)
        if isinstance(value, int):
            return value

        resolved = self.resolve_enum(
            type_name,
            namespace_hint=namespace_hint,
        )
        if resolved is None:
            try:
                return int(value)
            except Exception:
                raise ValueError(
                    f"Cannot resolve enum {type_name!r}: {value!r}"
                )

        _, members = resolved
        folded = str(value).replace("_", "").casefold()
        for name, number in members.items():
            if name.replace("_", "").casefold() == folded:
                return int(number)

        raise ValueError(
            f"Unknown {type_name} value {value!r}; "
            f"known values: {', '.join(members)}"
        )

    def _field_value(
        self,
        field: dict,
        value,
        namespace_hint: Optional[str],
    ) -> bytes:
        typ = field["inner_type"].strip()
        base = typ.rstrip("?")
        number = field["field_number"]

        if isinstance(value, (bytes, bytearray, memoryview)):
            return length_field(number, bytes(value))

        simple = base.rsplit(".", 1)[-1]
        if simple.endswith(self.UUID_SUFFIXES):
            return length_field(
                number,
                encode_root_guid(str(value)),
            )

        if simple == "RootContext":
            if value in (None, True, "auto"):
                nested = self._context_bytes()
            elif isinstance(value, Mapping):
                nested = self.encode_message(
                    base,
                    value,
                )
            else:
                nested = bytes(value)
            return length_field(number, nested)

        if simple == "Timestamp":
            return length_field(
                number,
                self._timestamp_bytes(value),
            )

        if simple == "Duration":
            return length_field(
                number,
                self._duration_bytes(value),
            )

        if field.get("wrapper"):
            if base in self.STRING_TYPES or base == "string":
                return length_field(
                    number,
                    length_field(1, str(value).encode("utf-8")),
                )
            if base in self.BOOL_TYPES or base == "bool":
                nested = (
                    field_key(1, 0)
                    + encode_varint(1 if bool(value) else 0)
                )
                return length_field(number, nested)
            if base in self.INTEGER_TYPES:
                nested = field_key(1, 0) + encode_varint(int(value))
                return length_field(number, nested)

        if base in self.STRING_TYPES:
            return length_field(
                number,
                str(value).encode("utf-8"),
            )

        if base in self.BYTES_TYPES:
            return length_field(number, bytes(value))

        if base in self.BOOL_TYPES:
            return (
                field_key(number, 0)
                + encode_varint(1 if bool(value) else 0)
            )

        if base in self.INTEGER_TYPES:
            ivalue = int(value)
            if ivalue < 0:

                ivalue &= (1 << 64) - 1
            return field_key(number, 0) + encode_varint(ivalue)

        if base in self.FLOAT_TYPES:
            return (
                field_key(number, 5)
                + struct.pack("<f", float(value))
            )

        if base in self.DOUBLE_TYPES:
            return (
                field_key(number, 1)
                + struct.pack("<d", float(value))
            )

        enum_info = self.resolve_enum(
            base,
            namespace_hint=namespace_hint,
        )
        if enum_info is not None:
            return (
                field_key(number, 0)
                + encode_varint(
                    self._enum_number(
                        base,
                        value,
                        namespace_hint,
                    )
                )
            )

        nested_schema = self.resolve_message(
            base,
            namespace_hint=namespace_hint,
        )
        if nested_schema is not None:
            if not isinstance(value, Mapping):
                raise TypeError(
                    f"{field['name']} ({base}) must be a mapping "
                    "or raw protobuf bytes"
                )
            nested = self.encode_message(
                base,
                value,
                namespace_hint=nested_schema.get("namespace"),
            )
            return length_field(number, nested)

        # An int -- including a rootpy.enums member, which is an IntEnum -- is
        # already the wire value, so it needs no schema lookup at all. That
        # matters beyond convenience: `UserOnlineStatus` exists in two packages
        # with *different* numbers, and neither matches the real wire value
        # (ACTIVE is 0x10, and the two enums that share the leaf name
        # UserOnlineStatus disagree.) Resolving such a name would encode a
        # wrong number,
        # while the enum the caller passed is right by construction.
        if isinstance(value, int) and not isinstance(value, bool):
            return field_key(number, 0) + encode_varint(int(value))

        raise TypeError(
            f"Unsupported protobuf field type {base!r} for {field['name']}. "
            f"If this is an enum, pass a rootpy.enums member or a plain int -- "
            f"the schema lookup cannot disambiguate every enum name."
        )

    def encode_message(
        self,
        type_name: str,
        values: Optional[Mapping[str, Any]] = None,
        *,
        namespace_hint: Optional[str] = None,
        auto_context: bool = True,
    ) -> bytes:
        values = dict(values or {})
        schema = self.resolve_message(
            type_name,
            namespace_hint=namespace_hint,
        )
        if schema is None:
            if not values:
                return b""
            raise ValueError(
                f"No message schema found for {type_name!r}"
            )

        aliases = {}
        for field in schema["fields"]:
            aliases[field["name"]] = field
            aliases[field["python_name"]] = field
            aliases[field["name"].casefold()] = field

        normalized = {}
        for key, value in values.items():
            field = aliases.get(key)
            if field is None:
                field = aliases.get(str(key).casefold())
            if field is None:
                raise TypeError(
                    f"{schema['name']} has no field {key!r}. "
                    f"Available: "
                    + ", ".join(
                        f["python_name"]
                        for f in schema["fields"]
                        if "field_number" in f
                    )
                )
            normalized[field["name"]] = value

        payload = bytearray()
        for field in schema["fields"]:
            if "field_number" not in field:
                continue

            name = field["name"]
            value = normalized.get(name, None)

            if (
                name == "Context"
                and value is None
                and auto_context
                and field["inner_type"].rstrip("?").endswith("RootContext")
            ):
                value = "auto"

            if value is None:
                continue

            if field.get("map"):
                if not isinstance(value, Mapping):
                    raise TypeError(
                        f"{field['python_name']} must be a mapping"
                    )

                map_match = re.match(
                    r"MapField<\s*([^,]+),\s*([^>]+)>",
                    field["type"],
                )
                if map_match is None:
                    raise TypeError(
                        f"Cannot infer map type {field['type']}"
                    )
                key_type, value_type = map_match.groups()
                for k, v in value.items():
                    fake_key = {
                        "name": "Key",
                        "python_name": "key",
                        "inner_type": key_type.strip(),
                        "field_number": 1,
                        "wrapper": False,
                    }
                    fake_value = {
                        "name": "Value",
                        "python_name": "value",
                        "inner_type": value_type.strip(),
                        "field_number": 2,
                        "wrapper": False,
                    }
                    entry = (
                        self._field_value(
                            fake_key,
                            k,
                            schema["namespace"],
                        )
                        + self._field_value(
                            fake_value,
                            v,
                            schema["namespace"],
                        )
                    )
                    payload += length_field(
                        field["field_number"],
                        entry,
                    )
                continue

            if field.get("repeated"):
                if isinstance(value, (str, bytes, bytearray)):
                    raise TypeError(
                        f"{field['python_name']} is repeated; "
                        "pass a list/tuple"
                    )
                for item in value:
                    payload += self._field_value(
                        field,
                        item,
                        schema["namespace"],
                    )
            else:
                payload += self._field_value(
                    field,
                    value,
                    schema["namespace"],
                )

        return bytes(payload)

    def _decode_scalar(
        self,
        field: dict,
        wire_type: int,
        value,
        namespace_hint: Optional[str],
    ):
        typ = field["inner_type"].strip().rstrip("?")
        simple = typ.rsplit(".", 1)[-1]

        if simple.endswith(self.UUID_SUFFIXES) and wire_type == 2:
            decoded = decode_root_guid_message(value)
            return decoded or AttrDict(raw=value)

        if simple == "Timestamp" and wire_type == 2:
            return decode_timestamp_message(value)

        if simple == "Duration" and wire_type == 2:
            seconds = 0
            nanos = 0
            for n, wt, inner in iter_fields(value):
                if n == 1 and wt == 0:
                    seconds = int(inner)
                elif n == 2 and wt == 0:
                    nanos = int(inner)
            return seconds + nanos / 1_000_000_000

        if field.get("wrapper") and wire_type == 2:
            for n, wt, inner in iter_fields(value):
                if n != 1:
                    continue
                if typ in self.STRING_TYPES and wt == 2:
                    return inner.decode("utf-8", errors="replace")
                if typ in self.BOOL_TYPES and wt == 0:
                    return bool(inner)
                if typ in self.INTEGER_TYPES and wt == 0:
                    return int(inner)
            return None

        if typ in self.STRING_TYPES and wire_type == 2:
            return value.decode("utf-8", errors="replace")

        if typ in self.BYTES_TYPES and wire_type == 2:
            return value

        if typ in self.BOOL_TYPES and wire_type == 0:
            return bool(value)

        if typ in self.INTEGER_TYPES and wire_type == 0:
            return int(value)

        if typ in self.FLOAT_TYPES and wire_type == 5:
            return struct.unpack("<f", value)[0]

        if typ in self.DOUBLE_TYPES and wire_type == 1:
            return struct.unpack("<d", value)[0]

        enum_info = self.resolve_enum(
            typ,
            namespace_hint=namespace_hint,
        )
        if enum_info is not None and wire_type == 0:
            enum_name, members = enum_info
            number = int(value)
            member_name = next(
                (
                    name
                    for name, member_value in members.items()
                    if int(member_value) == number
                ),
                None,
            )
            return EnumValue(
                enum_type=enum_name,
                value=number,
                name=member_name,
            )

        nested = self.resolve_message(
            typ,
            namespace_hint=namespace_hint,
        )
        if nested is not None and wire_type == 2:
            return self.decode_message(
                typ,
                value,
                namespace_hint=nested["namespace"],
            )

        if wire_type == 0:
            return int(value)
        if wire_type in (1, 5):
            return value
        if wire_type == 2:
            return value
        return value

    def decode_message(
        self,
        type_name: str,
        data: bytes,
        *,
        namespace_hint: Optional[str] = None,
    ):
        schema = self.resolve_message(
            type_name,
            namespace_hint=namespace_hint,
        )
        if schema is None:
            return AttrDict(raw=data)

        by_number = {
            f["field_number"]: f
            for f in schema["fields"]
            if "field_number" in f
        }
        result = AttrDict()

        for number, wire_type, value in iter_fields(data):
            field = by_number.get(number)
            if field is None:
                result.setdefault("_unknown", []).append(
                    AttrDict(
                        field_number=number,
                        wire_type=wire_type,
                        value=value,
                    )
                )
                continue

            decoded = self._decode_scalar(
                field,
                wire_type,
                value,
                schema["namespace"],
            )
            key = field["python_name"]

            if field.get("repeated"):
                result.setdefault(key, []).append(decoded)
            elif field.get("map"):

                result.setdefault(key, []).append(decoded)
            else:
                result[key] = decoded

        return result

    def decode_grpc_body(
        self,
        response_type: str,
        body: bytes,
    ):
        for flag, frame in iter_grpc_web_frames(body):
            if flag & 0x80:
                continue
            return self.decode_message(
                response_type,
                frame,
            )
        return AttrDict()


class StructuredMethod:
    def __init__(
        self,
        api,
        service_name: str,
        method_name: str,
    ) -> None:
        self.api = api
        self.service_name = service_name
        self.method_name = method_name

    @property
    def info(self) -> dict:
        return SERVICES[self.service_name][self.method_name]

    @property
    def fields(self) -> List[dict]:
        schema = self.api.codec.resolve_message(
            self.info["request"]
        )
        if schema is None:
            return []
        return [
            dict(field)
            for field in schema["fields"]
            if "field_number" in field
            and field["name"] != "Context"
        ]

    def signature(self) -> str:
        parts = []
        for field in self.fields:
            suffix = "[]" if field.get("repeated") else ""
            parts.append(
                f"{field['python_name']}: "
                f"{field['inner_type']}{suffix}"
            )
        return (
            f"{self.api.service_alias(self.service_name)}."
            f"{_snake(self.method_name)}("
            + ", ".join(parts)
            + ")"
        )

    async def __call__(self, **kwargs) -> StructuredResult:
        info = self.info
        if info["method_type"] != "Unary":
            raise NotImplementedError(
                f"{self.service_name}/{self.method_name} is "
                f"{info['method_type']}, not unary"
            )

        payload = self.api.codec.encode_message(
            info["request"],
            kwargs,
        )

        response = await self.api.transport.unary(
            endpoint=info["endpoint"],
            body=grpc_frame(payload),
            headers=self.api.headers(info["endpoint"]),
            operation=(
                f"{self.service_name}/{self.method_name}"
            ),
        )

        data = self.api.codec.decode_grpc_body(
            info["response"],
            response.content,
        )

        return StructuredResult(
            service=self.service_name,
            method=self.method_name,
            data=data,
            raw=response.content,
            request_type=info["request"],
            response_type=info["response"],
            http_status=response.status_code,
        )


class StructuredService:
    def __init__(self, api, service_name: str) -> None:
        self.api = api
        self.service_name = service_name

    def methods(self) -> List[str]:
        return sorted(
            _snake(name)
            for name in SERVICES[self.service_name]
        )

    def describe(self) -> Dict[str, str]:
        return {
            _snake(name): StructuredMethod(
                self.api,
                self.service_name,
                name,
            ).signature()
            for name in SERVICES[self.service_name]
        }

    def __getattr__(self, name: str) -> StructuredMethod:
        normalized = name.replace("_", "").casefold()
        for method_name in SERVICES[self.service_name]:
            if (
                method_name.replace("_", "").casefold()
                == normalized
                or _snake(method_name).replace("_", "").casefold()
                == normalized
            ):
                return StructuredMethod(
                    self.api,
                    self.service_name,
                    method_name,
                )
        raise AttributeError(
            f"{self.service_name!r} has no method {name!r}"
        )


class StructuredAPI:

    def __init__(self, transport, token_getter) -> None:
        self.transport = transport
        self.token_getter = token_getter
        self.codec = StructuredProtoCodec()
        self._aliases = self._build_aliases()

    def _build_aliases(self) -> Dict[str, str]:
        aliases = {}
        used = {}
        for service_name in SERVICES:
            simple = service_name.rsplit(".", 1)[-1]
            if simple.endswith("GrpcService"):
                simple = simple[:-11]
            alias = _snake(simple)

            if alias in used:
                ns = service_name.rsplit(".", 1)[0].split(".")[-1]
                alias = _snake(simple) + "_" + _snake(ns)

            aliases[alias] = service_name
            used[alias] = service_name
        return aliases

    def service_alias(self, service_name: str) -> str:
        for alias, full in self._aliases.items():
            if full == service_name:
                return alias
        return service_name

    def services(self) -> List[str]:
        return sorted(self._aliases)

    def describe(self, service: Optional[str] = None):
        if service is None:
            return {
                alias: StructuredService(
                    self,
                    full,
                ).describe()
                for alias, full in self._aliases.items()
            }
        return getattr(self, service).describe()

    def headers(self, endpoint: str) -> Dict[str, str]:
        headers = {
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "content-type": "application/grpc-web",
            "accept": "application/grpc-web",
        }

        token = self.token_getter()
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

        return headers

    def __getattr__(self, name: str) -> StructuredService:
        full = self._aliases.get(name)
        if full is None:
            normalized = name.replace("_", "").casefold()
            matches = [
                full_name
                for alias, full_name in self._aliases.items()
                if alias.replace("_", "").casefold()
                == normalized
            ]
            if len(matches) == 1:
                full = matches[0]
        if full is None:
            raise AttributeError(
                f"No Root service alias {name!r}. "
                f"Available: {', '.join(self.services())}"
            )
        return StructuredService(self, full)
