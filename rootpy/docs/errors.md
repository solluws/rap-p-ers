# RootPy errors

RootPy exposes typed exceptions for both gRPC-Web and HTTP failures. Applications
should catch a specific exception when they can handle it and use `RootError`
for a final library-wide fallback.

## Readable errors

Every gRPC/HTTP transport exception now includes a concise explanation:

```text
Unauthorized [HTTP_401/401]:
Root did not accept the authentication credentials for this request.
```

For detailed diagnostics:

```python
from rootpy import RootError, format_root_error

try:
    await client.invites.join("invite-code")
except RootError as exc:
    print(format_root_error(exc, verbose=True))
```

Example:

```text
Unauthorized [HTTP_401/401]: Root did not accept the authentication credentials for this request.
  operation: root.CommunityMemberInviteGrpcService/LinkJoin
  hint: Validate or refresh the account session/token. If only one pool client fails, validate that client's token.
```

`get_error_info(exc)` returns structured fields for logging/UI use:

```python
from rootpy import get_error_info

info = get_error_info(exc)

print(info.type_name)
print(info.operation)
print(info.status)
print(info.code)
print(info.summary)
print(info.server_message)
print(info.hint)
print(info.retry_after)
```

## HTTP exceptions

| Exception | Code | Meaning |
|---|---:|---|
| `BadRequest` | 400 | Invalid/malformed request |
| `Unauthorized` | 401 | Authentication was not accepted |
| `Forbidden` | 403 | Authenticated but not permitted |
| `HttpNotFound` | 404 | Resource/endpoint not found |
| `Conflict` | 409 | Conflicts with current state |
| `PayloadTooLarge` | 413 | Payload is too large |
| `RateLimited` | 429 | Request rate/quota limit |
| `BadGateway` | 502 | Upstream/gateway failure |
| `ServiceUnavailable` | 503 | Service temporarily unavailable |
| `GatewayTimeout` | 504 | Upstream timed out |

`RateLimited.retry_after` is populated when Root supplies a valid
`Retry-After` response header.

## gRPC exceptions

| Exception | gRPC status | Code |
|---|---|---:|
| `GrpcCancelled` | `CANCELLED` | 1 |
| `GrpcUnknown` | `UNKNOWN` | 2 |
| `GrpcInvalidArgument` | `INVALID_ARGUMENT` | 3 |
| `GrpcDeadlineExceeded` | `DEADLINE_EXCEEDED` | 4 |
| `GrpcNotFound` | `NOT_FOUND` | 5 |
| `GrpcAlreadyExists` | `ALREADY_EXISTS` | 6 |
| `GrpcPermissionDenied` | `PERMISSION_DENIED` | 7 |
| `GrpcResourceExhausted` | `RESOURCE_EXHAUSTED` | 8 |
| `GrpcFailedPrecondition` | `FAILED_PRECONDITION` | 9 |
| `GrpcAborted` | `ABORTED` | 10 |
| `GrpcOutOfRange` | `OUT_OF_RANGE` | 11 |
| `GrpcUnimplemented` | `UNIMPLEMENTED` | 12 |
| `GrpcInternal` | `INTERNAL` | 13 |
| `GrpcUnavailable` | `UNAVAILABLE` | 14 |
| `GrpcDataLoss` | `DATA_LOSS` | 15 |
| `GrpcUnauthenticated` | `UNAUTHENTICATED` | 16 |

## Pool errors

Pool broadcast results expose the original typed exception plus formatted
diagnostics:

```python
results = await pool.execute_console_command("mycommand")

for result in results:
    if not result.succeeded:
        print(result.format_error(verbose=True))
```

For a large pool, aggregate failures instead of printing thousands of lines:

```python
summary = pool.summarize_errors(results)

for name, count in summary.items():
    print(f"{name}: {count}")
```

Enable detailed automatic broadcast errors with:

```python
pool = RootClientPool(
    tokens,
    verbose_errors=True,
)
```

The default is `False` so high-volume pool logs stay compact.

## Common failures

### `Unauthorized` / HTTP 401

The specific HTTP request was not accepted as authenticated. For pools, first
check whether the error affects one client or every client. `validate_clients()`
can identify credentials that fail a lightweight authenticated request.

### `GrpcUnauthenticated` / status 16

The gRPC endpoint rejected the request's authentication state. Inspect the
operation because a token may work for one endpoint but not another depending
on session/account state.

### `GrpcPermissionDenied` / status 7 or `Forbidden` / HTTP 403

Authentication succeeded, but the account does not have permission to perform
the requested operation.

### `GrpcResourceExhausted` / status 8 or `RateLimited` / HTTP 429

Root rejected work because of a quota/resource/rate limit. Do not retry in a
tight loop. Respect server retry metadata when available.

### `GrpcUnavailable` / status 14

The service could not process the request at that time. This is generally a
transient connectivity/service condition; use bounded backoff rather than an
immediate retry loop.
