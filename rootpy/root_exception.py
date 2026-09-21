"""Decoder for Root's structured gRPC error payload (``RootGrpcException``).

When a Root RPC fails, the server attaches a base64 ``RootGrpcException``
protobuf in the ``root-exception-bin`` response trailer. It carries far more
than the gRPC status:

* ``error_code``   -- a Root-specific ``ErrorCodeType`` (finer than the gRPC
  status; exposed here as its integer value since the name table isn't part
  of this schema);
* ``who_id`` / ``what_id`` / ``where_id`` / ``parent_id`` -- the entities the
  error is about (actor, subject, container, parent);
* ``payload``      -- a **typed** detail identifying *why* it failed. This
  module decodes every payload variant fully:

    - ``username``               -> {"username"}
    - ``email``                  -> {"email"}
    - ``access_token``           -> {"access_token"}
    - ``friend_user``            -> {"friend_user_id"}
    - ``upload_status_list``     -> {"upload_statuses": [{token_uri, result}]}
    - ``request_validator_list`` -> {"errors": [{property_name, error_message,
                                     error_code}]}  (per-field validation)
    - ``payment_error``          -> {"message", "decline_code"}

The field numbers match Root's ``RootGrpcException`` and
``*ExceptionPayload`` messages, so the payloads decode exactly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .protocol import decode_root_guid_message, iter_fields
from .enums import ErrorCodeType

# --- RootGrpcException envelope field numbers ------------------------------
_EXC_ERROR_CODE = 10
_EXC_ID = 11
_EXC_WHO_ID = 12
_EXC_WHAT_ID = 13
_EXC_WHERE_ID = 14
_EXC_PARENT_ID = 15
_EXC_PAYLOAD = 16

# RootGrpcExceptionPayload field number -> detail kind. Exactly one is set.
PAYLOAD_KINDS = {
    10: "username",
    11: "email",
    12: "access_token",
    13: "friend_user",
    14: "upload_status_list",
    15: "request_validator_list",
    16: "payment_error",
}

_HEADER_NAME = "root-exception-bin"


# --- low-level protobuf field readers --------------------------------------
def _string(data: bytes, number: int) -> Optional[str]:
    for n, wire_type, value in iter_fields(data):
        if n == number and wire_type == 2 and isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def _messages(data: bytes, number: int) -> List[bytes]:
    out: List[bytes] = []
    for n, wire_type, value in iter_fields(data):
        if n == number and wire_type == 2 and isinstance(value, (bytes, bytearray)):
            out.append(bytes(value))
    return out


def _first_message(data: bytes, number: int) -> Optional[bytes]:
    msgs = _messages(data, number)
    return msgs[0] if msgs else None


def _bool_wrapper(data: bytes, number: int) -> Optional[bool]:
    # google.protobuf.BoolValue is a message with a single bool at field 1.
    inner = _first_message(data, number)
    if inner is None:
        return None
    for n, wire_type, value in iter_fields(inner):
        if n == 1 and wire_type == 0:
            return bool(value)
    return False


# --- typed validation-error view -------------------------------------------
@dataclass(frozen=True)
class ValidationError:
    """One per-field validation failure from ``request_validator_list``."""

    property_name: str = ""
    error_message: str = ""
    error_code: str = ""

    def __str__(self) -> str:
        where = self.property_name or "request"
        code = f" [{self.error_code}]" if self.error_code else ""
        return f"{where}: {self.error_message or 'invalid'}{code}"


def _decode_validation_error(data: bytes) -> ValidationError:
    return ValidationError(
        property_name=_string(data, 10) or "",
        error_message=_string(data, 11) or "",
        error_code=_string(data, 12) or "",
    )


# --- payload decoders (keyed by RootGrpcExceptionPayload field number) ------
def _decode_username(d: bytes) -> Dict[str, Any]:
    return {"username": _string(d, 10)}


def _decode_email(d: bytes) -> Dict[str, Any]:
    return {"email": _string(d, 10)}


def _decode_access_token(d: bytes) -> Dict[str, Any]:
    return {"access_token": _string(d, 10)}


def _decode_friend_user(d: bytes) -> Dict[str, Any]:
    inner = _first_message(d, 10)
    return {
        "friend_user_id": decode_root_guid_message(inner) if inner else None
    }


def _decode_upload_status(d: bytes) -> Dict[str, Any]:
    return {"token_uri": _string(d, 10), "result": _bool_wrapper(d, 11)}


def _decode_upload_status_list(d: bytes) -> Dict[str, Any]:
    return {
        "upload_statuses": [
            _decode_upload_status(m) for m in _messages(d, 10)
        ]
    }


def _decode_request_validator_list(d: bytes) -> Dict[str, Any]:
    return {
        "errors": [
            _decode_validation_error(m) for m in _messages(d, 10)
        ]
    }


def _decode_payment_error(d: bytes) -> Dict[str, Any]:
    return {
        "message": _string(d, 10),
        "decline_code": _string(d, 11),
    }


_PAYLOAD_DECODERS = {
    "username": _decode_username,
    "email": _decode_email,
    "access_token": _decode_access_token,
    "friend_user": _decode_friend_user,
    "upload_status_list": _decode_upload_status_list,
    "request_validator_list": _decode_request_validator_list,
    "payment_error": _decode_payment_error,
}


@dataclass(frozen=True)
class RootExceptionInfo:
    """Structured view of a decoded ``RootGrpcException``."""

    error_code: int = 0
    id: Optional[str] = None
    who_id: Optional[str] = None
    what_id: Optional[str] = None
    where_id: Optional[str] = None
    parent_id: Optional[str] = None
    payload_kind: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    payload_raw: bytes = b""
    raw: bytes = b""

    @property
    def validation_errors(self) -> List[ValidationError]:
        """Per-field validation failures, if this is a validator-list error."""
        if self.payload_kind == "request_validator_list":
            return list(self.payload.get("errors", ()))
        return []

    def summary(self) -> str:
        """A short, human-friendly one-liner describing the detail."""
        kind = self.payload_kind
        p = self.payload or {}
        if kind == "request_validator_list":
            errs = self.validation_errors
            if errs:
                shown = "; ".join(str(e) for e in errs[:3])
                extra = f" (+{len(errs) - 3} more)" if len(errs) > 3 else ""
                return f"validation failed -> {shown}{extra}"
            return "validation failed"
        if kind == "payment_error":
            msg = p.get("message") or "payment error"
            code = p.get("decline_code")
            return f"payment declined: {msg}" + (f" ({code})" if code else "")
        if kind in ("username", "email", "access_token"):
            value = p.get(kind)
            return f"{kind}: {value}" if value else kind
        if kind == "friend_user":
            fid = p.get("friend_user_id")
            return f"friendship required{f' with {fid}' if fid else ''}"
        if kind == "upload_status_list":
            statuses = p.get("upload_statuses", [])
            failed = [s for s in statuses if s.get("result") is False]
            return f"{len(failed)}/{len(statuses)} upload(s) failed"
        if kind:
            return kind
        if self.error_code:
            name = getattr(self.error_code, "label", None)
            return f"root error: {name}" if name else f"root error code {self.error_code}"
        return ""


def decode_root_exception(data: bytes) -> Optional[RootExceptionInfo]:
    """Decode raw ``RootGrpcException`` bytes, or return None on failure."""
    if not data:
        return None
    try:
        error_code = 0
        ids = {
            _EXC_ID: None,
            _EXC_WHO_ID: None,
            _EXC_WHAT_ID: None,
            _EXC_WHERE_ID: None,
            _EXC_PARENT_ID: None,
        }
        payload_kind = None
        payload_data: Dict[str, Any] = {}
        payload_raw = b""

        for number, wire_type, value in iter_fields(data):
            if number == _EXC_ERROR_CODE and wire_type == 0:
                error_code = ErrorCodeType.coerce(int(value))
            elif number in ids and wire_type == 2:
                ids[number] = decode_root_guid_message(value)
            elif number == _EXC_PAYLOAD and wire_type == 2:
                payload_raw = bytes(value)
                # Exactly one sub-field is set; its number names the kind.
                for sub_number, sub_wire, sub_value in iter_fields(value):
                    kind = PAYLOAD_KINDS.get(sub_number)
                    if kind is None:
                        continue
                    payload_kind = kind
                    decoder = _PAYLOAD_DECODERS.get(kind)
                    if (
                        decoder is not None
                        and sub_wire == 2
                        and isinstance(sub_value, (bytes, bytearray))
                    ):
                        try:
                            payload_data = decoder(bytes(sub_value))
                        except Exception:
                            payload_data = {}
                    break

        return RootExceptionInfo(
            error_code=error_code,
            id=ids[_EXC_ID],
            who_id=ids[_EXC_WHO_ID],
            what_id=ids[_EXC_WHAT_ID],
            where_id=ids[_EXC_WHERE_ID],
            parent_id=ids[_EXC_PARENT_ID],
            payload_kind=payload_kind,
            payload=payload_data,
            payload_raw=payload_raw,
            raw=bytes(data),
        )
    except Exception:
        return None


def decode_root_exception_header(
    headers: Optional[Mapping[str, str]],
) -> Optional[RootExceptionInfo]:
    """Decode the ``root-exception-bin`` trailer from a header mapping."""
    if not headers:
        return None
    encoded = None
    for key, value in headers.items():
        if key.lower() == _HEADER_NAME:
            encoded = value
            break
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return None
    return decode_root_exception(raw)
