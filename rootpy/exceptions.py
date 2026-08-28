from enum import IntEnum
from typing import Mapping, Optional


class RootError(Exception):
    """Base exception for the library."""


class AuthenticationError(RootError):
    """Authentication failed."""


class TurnstileRequired(AuthenticationError):
    def __init__(
        self,
        challenge_url: str,
        action: Optional[str] = None,
    ) -> None:
        message = "Turnstile challenge required"
        if action:
            message += f" for {action}"
        super().__init__(f"{message}: {challenge_url}")
        self.challenge_url = challenge_url
        self.action = action


class SignUpError(AuthenticationError):
    """Base exception for account-creation failures."""


class AccountAlreadyExists(SignUpError):
    def __init__(self) -> None:
        super().__init__(
            "An account with the requested username or email already exists"
        )


class UsernameAlreadyExists(AccountAlreadyExists):
    def __init__(self, username: str) -> None:
        AuthenticationError.__init__(
            self,
            f"Username already exists: {username}",
        )
        self.username = username


class EmailAlreadyExists(AccountAlreadyExists):
    def __init__(self, email: str) -> None:
        AuthenticationError.__init__(
            self,
            f"Email already exists: {email}",
        )
        self.email = email


class GrpcStatus(IntEnum):
    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16

    @classmethod
    def from_value(cls, value) -> "GrpcStatus":
        try:
            return cls(int(value))
        except (TypeError, ValueError):
            return cls.UNKNOWN



_GRPC_STATUS_DESCRIPTIONS = {
    GrpcStatus.CANCELLED: "The operation was cancelled before it completed.",
    GrpcStatus.UNKNOWN: "Root returned an unspecified server-side error.",
    GrpcStatus.INVALID_ARGUMENT: "One or more request values were rejected by Root.",
    GrpcStatus.DEADLINE_EXCEEDED: "Root did not finish the request before its deadline.",
    GrpcStatus.NOT_FOUND: "The requested Root object could not be found.",
    GrpcStatus.ALREADY_EXISTS: "The requested object or relationship already exists.",
    GrpcStatus.PERMISSION_DENIED: "The account is authenticated but is not allowed to perform this operation.",
    GrpcStatus.RESOURCE_EXHAUSTED: "A Root quota, rate limit, or server resource limit was reached.",
    GrpcStatus.FAILED_PRECONDITION: "The request is valid but Root requires another condition to be satisfied first.",
    GrpcStatus.ABORTED: "Root aborted the operation, usually because of a conflicting state change.",
    GrpcStatus.OUT_OF_RANGE: "A request value was outside the range accepted by Root.",
    GrpcStatus.UNIMPLEMENTED: "This Root endpoint or operation is not implemented.",
    GrpcStatus.INTERNAL: "Root encountered an internal server error.",
    GrpcStatus.UNAVAILABLE: "The Root service is temporarily unavailable or the connection could not be serviced.",
    GrpcStatus.DATA_LOSS: "Root reported unrecoverable protocol/data loss.",
    GrpcStatus.UNAUTHENTICATED: "Root did not accept the authentication attached to this request.",
}

_GRPC_STATUS_HINTS = {
    GrpcStatus.CANCELLED: "Retry only if the cancellation was not intentional.",
    GrpcStatus.UNKNOWN: "Inspect the operation name and Root's grpc-message; enable verbose error output if needed.",
    GrpcStatus.INVALID_ARGUMENT: "Check IDs, enum values, required fields, and request payload types.",
    GrpcStatus.DEADLINE_EXCEEDED: "Retry later or use a longer request timeout if the operation is expected to be slow.",
    GrpcStatus.NOT_FOUND: "Check that the ID exists and that the account can see the requested resource.",
    GrpcStatus.ALREADY_EXISTS: "Fetch the existing object instead of creating it again.",
    GrpcStatus.PERMISSION_DENIED: "Check community/channel permissions and whether the account has the required role.",
    GrpcStatus.RESOURCE_EXHAUSTED: "Slow the request rate and respect Retry-After/retry metadata when Root provides it.",
    GrpcStatus.FAILED_PRECONDITION: "Check account/community state and any prerequisite operation required by the endpoint.",
    GrpcStatus.ABORTED: "Refresh state and retry the operation if it is safe to do so.",
    GrpcStatus.OUT_OF_RANGE: "Validate numeric/index/range values before sending the request.",
    GrpcStatus.UNIMPLEMENTED: "Do not retry repeatedly; the endpoint may not be supported by this Root version.",
    GrpcStatus.INTERNAL: "Usually transient. Retry conservatively; report persistent failures with the operation name.",
    GrpcStatus.UNAVAILABLE: "Retry with backoff. Check connectivity and Root service availability.",
    GrpcStatus.DATA_LOSS: "Do not blindly retry writes; capture diagnostics and treat this as a protocol/server fault.",
    GrpcStatus.UNAUTHENTICATED: "Check whether the token/session is expired, revoked, malformed, or invalid for this endpoint.",
}

_HTTP_STATUS_DESCRIPTIONS = {
    400: "Root rejected the HTTP request as malformed or invalid.",
    401: "Root did not accept the authentication credentials for this request.",
    403: "The account is authenticated but Root refused the requested operation.",
    404: "The requested HTTP resource or endpoint could not be found.",
    409: "The request conflicts with the current state of the resource.",
    413: "The request body or uploaded payload is too large.",
    429: "Root is rate limiting this client or operation.",
    500: "Root encountered an internal server error.",
    502: "A Root gateway received an invalid response from an upstream service.",
    503: "The Root service is temporarily unavailable.",
    504: "A Root gateway timed out waiting for an upstream service.",
}

_HTTP_STATUS_HINTS = {
    400: "Check request arguments, IDs, payload shape, and required fields.",
    401: "Validate or refresh the account session/token. If only one hosted account fails, validate that account's token.",
    403: "Check the account's permissions, roles, membership, and endpoint-specific requirements.",
    404: "Verify the endpoint/resource ID and whether it is visible to the current account.",
    409: "Refresh current state before retrying; the operation may already have been completed.",
    413: "Reduce the payload/file size before retrying.",
    429: "Respect Retry-After when present and reduce request frequency.",
    500: "Retry conservatively; persistent errors are likely server-side.",
    502: "Retry with backoff; this is usually transient.",
    503: "Retry with backoff after Root becomes available.",
    504: "Retry with backoff or increase the request deadline where appropriate.",
}


def _friendly_operation(operation: str) -> str:
    """Return a compact readable endpoint name without losing the full name."""
    if not operation:
        return "Root operation"
    return operation


class GrpcWebError(RootError):
    """Typed gRPC-Web error returned by a Root service.

    Attributes:
        operation: Full Root RPC operation, e.g. ``root.UserGrpcService/GetSelf``.
        status: :class:`GrpcStatus` enum value.
        status_code: Numeric gRPC status code.
        grpc_message: Raw ``grpc-message`` returned by Root.
        response_headers: Response headers captured by the transport.
        description: Human-readable meaning of this gRPC status.
        hint: Suggested diagnostic/recovery action.
    """

    status_type = GrpcStatus.UNKNOWN

    def __init__(
        self,
        operation: str,
        status,
        message: str = "",
        *,
        response_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.operation = operation
        self.status = GrpcStatus.from_value(status)
        self.status_code = int(self.status)
        self.grpc_message = message
        self.response_headers = dict(response_headers or {})

        # Decode Root's structured error payload (root-exception-bin), if any.
        # This is best-effort and must never break error construction.
        self.root_exception = None
        self.error_code = 0
        self.who_id = None
        self.what_id = None
        self.where_id = None
        self.parent_id = None
        self.payload_kind = None
        self.payload = {}
        self.validation_errors = []
        try:
            from .root_exception import decode_root_exception_header

            info = decode_root_exception_header(self.response_headers)
            if info is not None:
                self.root_exception = info
                self.error_code = info.error_code
                self.who_id = info.who_id
                self.what_id = info.what_id
                self.where_id = info.where_id
                self.parent_id = info.parent_id
                self.payload_kind = info.payload_kind
                self.payload = info.payload
                self.validation_errors = info.validation_errors
        except Exception:
            pass

        self.description = _GRPC_STATUS_DESCRIPTIONS.get(
            self.status,
            "Root returned a gRPC error.",
        )
        self.hint = _GRPC_STATUS_HINTS.get(
            self.status,
            "Inspect the operation and response metadata.",
        )

        # Keep str(exc) concise enough for logs, while exposing full detail via
        # format_root_error(exc, verbose=True).
        details = (
            f"{self.status.name} ({self.status_code}): "
            f"{self.description}"
        )
        if message and message.strip() != self.description.strip():
            details += f" [Root: {message}]"

        # Root's grpc-message for a rejected request is always the same
        # useless sentence ("One or more request values were rejected"), but
        # the root-exception-bin header carries a RequestValidatorList naming
        # the offending fields. That was already decoded into
        # self.validation_errors above and then thrown away here, which made
        # every INVALID_ARGUMENT require a round trip to diagnose. Put it in
        # the message so the field and reason are visible wherever the error
        # surfaces -- tests, scripts, logs, tracebacks.
        if self.validation_errors:
            shown = "; ".join(str(err) for err in self.validation_errors[:5])
            extra = (
                f" (+{len(self.validation_errors) - 5} more)"
                if len(self.validation_errors) > 5
                else ""
            )
            details += f" -> {shown}{extra}"
        elif self.payload_kind:
            try:
                summary = self.root_exception.summary()
            except Exception:
                summary = ""
            if summary:
                details += f" -> {summary}"

        super().__init__(
            f"{_friendly_operation(operation)} failed — {details}"
        )


class GrpcCancelled(GrpcWebError):
    status_type = GrpcStatus.CANCELLED


class GrpcUnknown(GrpcWebError):
    status_type = GrpcStatus.UNKNOWN


class GrpcInvalidArgument(GrpcWebError):
    status_type = GrpcStatus.INVALID_ARGUMENT


class GrpcDeadlineExceeded(GrpcWebError):
    status_type = GrpcStatus.DEADLINE_EXCEEDED


class GrpcNotFound(GrpcWebError):
    status_type = GrpcStatus.NOT_FOUND


class GrpcAlreadyExists(GrpcWebError):
    status_type = GrpcStatus.ALREADY_EXISTS


class GrpcPermissionDenied(GrpcWebError):
    status_type = GrpcStatus.PERMISSION_DENIED


class GrpcResourceExhausted(GrpcWebError):
    status_type = GrpcStatus.RESOURCE_EXHAUSTED


class GrpcFailedPrecondition(GrpcWebError):
    status_type = GrpcStatus.FAILED_PRECONDITION


class GrpcAborted(GrpcWebError):
    status_type = GrpcStatus.ABORTED


class GrpcOutOfRange(GrpcWebError):
    status_type = GrpcStatus.OUT_OF_RANGE


class GrpcUnimplemented(GrpcWebError):
    status_type = GrpcStatus.UNIMPLEMENTED


class GrpcInternal(GrpcWebError):
    status_type = GrpcStatus.INTERNAL


class GrpcUnavailable(GrpcWebError):
    status_type = GrpcStatus.UNAVAILABLE


class GrpcDataLoss(GrpcWebError):
    status_type = GrpcStatus.DATA_LOSS


class GrpcUnauthenticated(GrpcWebError):
    status_type = GrpcStatus.UNAUTHENTICATED


_GRPC_ERROR_TYPES = {
    GrpcStatus.CANCELLED: GrpcCancelled,
    GrpcStatus.UNKNOWN: GrpcUnknown,
    GrpcStatus.INVALID_ARGUMENT: GrpcInvalidArgument,
    GrpcStatus.DEADLINE_EXCEEDED: GrpcDeadlineExceeded,
    GrpcStatus.NOT_FOUND: GrpcNotFound,
    GrpcStatus.ALREADY_EXISTS: GrpcAlreadyExists,
    GrpcStatus.PERMISSION_DENIED: GrpcPermissionDenied,
    GrpcStatus.RESOURCE_EXHAUSTED: GrpcResourceExhausted,
    GrpcStatus.FAILED_PRECONDITION: GrpcFailedPrecondition,
    GrpcStatus.ABORTED: GrpcAborted,
    GrpcStatus.OUT_OF_RANGE: GrpcOutOfRange,
    GrpcStatus.UNIMPLEMENTED: GrpcUnimplemented,
    GrpcStatus.INTERNAL: GrpcInternal,
    GrpcStatus.UNAVAILABLE: GrpcUnavailable,
    GrpcStatus.DATA_LOSS: GrpcDataLoss,
    GrpcStatus.UNAUTHENTICATED: GrpcUnauthenticated,
}


def make_grpc_error(
    operation: str,
    status,
    message: str = "",
    *,
    response_headers: Optional[Mapping[str, str]] = None,
) -> GrpcWebError:
    status_type = GrpcStatus.from_value(status)
    error_cls = _GRPC_ERROR_TYPES.get(status_type, GrpcWebError)
    return error_cls(
        operation,
        status_type,
        message,
        response_headers=response_headers,
    )


class HttpError(RootError):
    def __init__(
        self,
        status_code: int,
        message: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self.status_name = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            413: "PAYLOAD_TOO_LARGE",
            429: "TOO_MANY_REQUESTS",
            500: "INTERNAL_SERVER_ERROR",
            502: "BAD_GATEWAY",
            503: "SERVICE_UNAVAILABLE",
            504: "GATEWAY_TIMEOUT",
        }.get(self.status_code, "HTTP_ERROR")
        super().__init__(
            f"{self.status_name} ({self.status_code})"
            + (f": {message}" if message else "")
        )


class UsernameLookupAuthenticationRequired(AuthenticationError):
    """Root requires authentication for username lookup."""

    def __init__(self) -> None:
        super().__init__(
            "Root's FindByUsername endpoint requires authentication. "
            "Pass token=..., or auth_username=... and auth_password=...."
        )


class RootProtocolError(RootError):
    """Malformed or unexpected Root protocol data."""


class DirectMessageError(RootError):
    """A direct-message operation failed for a reason we can explain.

    Raised instead of a bare ``GrpcInvalidArgument`` when a DM Find/Create
    is rejected, so the caller gets an actionable message (bad member set,
    target privacy gate, invalid id) rather than an opaque "root-error".
    The original gRPC error, when present, is available via ``__cause__``.
    """


class RootPermissionError(RootError):
    """Base error for permission failures."""


class RootNotFoundError(RootError):
    """Requested Root object was not found."""


class RootAlreadyExistsError(RootError):
    """Requested Root object already exists."""


class HttpStatusError(RootError):
    """Typed non-success HTTP response from a Root endpoint.

    The exception exposes both machine-readable fields and friendly diagnostic
    text through ``description`` and ``hint``.
    """

    status_code = 0

    def __init__(
        self,
        operation: str,
        status_code: int,
        message: str = "",
        *,
        response_headers=None,
    ):
        self.operation = operation
        self.status_code = int(status_code)
        self.response_headers = dict(response_headers or {})
        self.http_message = message
        self.description = _HTTP_STATUS_DESCRIPTIONS.get(
            self.status_code,
            "Root returned a non-success HTTP response.",
        )
        self.hint = _HTTP_STATUS_HINTS.get(
            self.status_code,
            "Inspect the operation, response body, and headers.",
        )
        details = f"HTTP {self.status_code}: {self.description}"
        if message:
            details += f" [Root: {message}]"
        super().__init__(
            f"{_friendly_operation(operation)} failed — {details}"
        )


class BadRequest(HttpStatusError):
    """HTTP 400: Root rejected the request as invalid."""
    status_code = 400


class Unauthorized(HttpStatusError):
    """HTTP 401: Root did not accept this request's authentication."""
    status_code = 401


class Forbidden(HttpStatusError):
    """HTTP 403: authenticated, but the operation is not permitted."""
    status_code = 403


class HttpNotFound(HttpStatusError):
    """HTTP 404: requested endpoint/resource was not found."""
    status_code = 404


class Conflict(HttpStatusError):
    """HTTP 409: request conflicts with current Root state."""
    status_code = 409


class PayloadTooLarge(HttpStatusError):
    """HTTP 413: request/upload is too large."""
    status_code = 413
class RateLimited(HttpStatusError):
    status_code = 429
    def __init__(self, operation, status_code=429, message="", *, response_headers=None):
        super().__init__(operation, status_code, message, response_headers=response_headers)
        value = self.response_headers.get("retry-after")
        try: self.retry_after = float(value) if value is not None else None
        except (TypeError, ValueError): self.retry_after = None
class ServerError(HttpStatusError): pass
class BadGateway(ServerError): status_code = 502
class ServiceUnavailable(ServerError): status_code = 503
class GatewayTimeout(ServerError): status_code = 504

_HTTP_ERRORS = {400:BadRequest,401:Unauthorized,403:Forbidden,404:HttpNotFound,409:Conflict,413:PayloadTooLarge,429:RateLimited,502:BadGateway,503:ServiceUnavailable,504:GatewayTimeout}

def make_http_error(operation, status_code, message="", *, response_headers=None):
    code=int(status_code)
    cls=_HTTP_ERRORS.get(code, ServerError if code >= 500 else HttpStatusError)
    return cls(operation, code, message, response_headers=response_headers)

class RetryExhausted(RootError):
    def __init__(self, operation: str, attempts: int, last_error: Exception):
        self.operation=operation; self.attempts=attempts; self.last_error=last_error
        super().__init__(f"{operation} failed after {attempts} attempts: {last_error}")


class ErrorInfo:
    """Normalized diagnostic view of an exception.

    This intentionally avoids dataclasses so it remains lightweight and easy
    to construct on error paths.
    """

    def __init__(
        self,
        *,
        type_name: str,
        summary: str,
        operation: Optional[str] = None,
        code: Optional[int] = None,
        status: Optional[str] = None,
        server_message: str = "",
        hint: str = "",
        retry_after: Optional[float] = None,
        error_code: int = 0,
        payload_kind: Optional[str] = None,
        who_id: Optional[str] = None,
        what_id: Optional[str] = None,
        where_id: Optional[str] = None,
        validation_errors=None,
    ) -> None:
        self.type_name = type_name
        self.summary = summary
        self.operation = operation
        self.code = code
        self.status = status
        self.server_message = server_message
        self.hint = hint
        self.retry_after = retry_after
        # Root's structured error detail (when present).
        self.error_code = error_code
        self.payload_kind = payload_kind
        self.who_id = who_id
        self.what_id = what_id
        self.where_id = where_id
        self.validation_errors = validation_errors or []

    def __repr__(self) -> str:
        return (
            "ErrorInfo("
            f"type_name={self.type_name!r}, "
            f"summary={self.summary!r}, "
            f"operation={self.operation!r}, "
            f"code={self.code!r}, "
            f"status={self.status!r})"
        )


def get_error_info(error: BaseException) -> ErrorInfo:
    """Return normalized, user-friendly metadata for any RootPy exception."""
    if isinstance(error, GrpcWebError):
        info = getattr(error, "root_exception", None)
        detail_summary = info.summary() if info is not None else ""
        summary = error.description
        if detail_summary:
            summary = f"{error.description} — {detail_summary}"
        return ErrorInfo(
            type_name=type(error).__name__,
            summary=summary,
            operation=error.operation,
            code=error.status_code,
            status=error.status.name,
            server_message=error.grpc_message,
            hint=error.hint,
            error_code=getattr(error, "error_code", 0),
            payload_kind=getattr(error, "payload_kind", None),
            who_id=getattr(error, "who_id", None),
            what_id=getattr(error, "what_id", None),
            where_id=getattr(error, "where_id", None),
            validation_errors=getattr(error, "validation_errors", []),
        )

    if isinstance(error, HttpStatusError):
        return ErrorInfo(
            type_name=type(error).__name__,
            summary=error.description,
            operation=error.operation,
            code=error.status_code,
            status=f"HTTP_{error.status_code}",
            server_message=error.http_message,
            hint=error.hint,
            retry_after=getattr(error, "retry_after", None),
        )

    if isinstance(error, RetryExhausted):
        inner = get_error_info(error.last_error)
        return ErrorInfo(
            type_name=type(error).__name__,
            summary=(
                f"{error.operation} failed after "
                f"{error.attempts} attempts. {inner.summary}"
            ),
            operation=error.operation,
            code=inner.code,
            status=inner.status,
            server_message=inner.server_message,
            hint=inner.hint,
            retry_after=inner.retry_after,
        )

    return ErrorInfo(
        type_name=type(error).__name__,
        summary=str(error) or type(error).__name__,
    )


def format_root_error(
    error: BaseException,
    *,
    verbose: bool = False,
    prefix: str = "",
) -> str:
    """Format an exception for terminal/log output.

    Args:
        error: Exception to format.
        verbose: Include operation, raw server message, hint, and Retry-After.
        prefix: Optional text prepended to the first line.

    Returns:
        A readable one- or multi-line string.

    Example:
        >>> try:
        ...     await client.invites.join("invite")
        ... except RootError as exc:
        ...     print(format_root_error(exc, verbose=True))

    Notes:
        ``verbose=False`` is intended for high-volume multi-account logs.
        ``verbose=True`` is intended for debugging a specific failure.
    """
    info = get_error_info(error)
    label = f"{info.type_name}"
    if info.status:
        label += f" [{info.status}"
        if info.code is not None:
            label += f"/{info.code}"
        label += "]"

    first = f"{prefix}{label}: {info.summary}"

    if not verbose:
        return first

    lines = [first]
    if info.operation:
        lines.append(f"  operation: {info.operation}")
    if info.server_message:
        lines.append(f"  server: {info.server_message}")
    if getattr(info, "payload_kind", None):
        lines.append(f"  detail: {info.payload_kind}")
    for verr in getattr(info, "validation_errors", []) or []:
        lines.append(f"    - {verr}")
    if getattr(info, "error_code", 0):
        code = info.error_code
        name = getattr(code, "name", None)
        lines.append(
            f"  root-code: {int(code)}" + (f" ({name})" if name else "")
        )
    for label_id in ("who_id", "what_id", "where_id"):
        value = getattr(info, label_id, None)
        if value:
            lines.append(f"  {label_id.replace('_', '-')}: {value}")
    if info.retry_after is not None:
        lines.append(f"  retry-after: {info.retry_after:g}s")
    if info.hint:
        lines.append(f"  hint: {info.hint}")
    return "\n".join(lines)
