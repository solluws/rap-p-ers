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
  hint: Validate or refresh the account session/token. If only one hosted account fails, validate that account's token.
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
| `ServerError` | 500 | Root encountered an internal server error |
| `BadGateway` | 502 | Upstream/gateway failure |
| `ServiceUnavailable` | 503 | Service temporarily unavailable |
| `GatewayTimeout` | 504 | Upstream timed out |

`RateLimited.retry_after` is populated when Root supplies a valid
`Retry-After` response header.

`ServerError` is not just the 500 row: it is the shared **base** of the three
5xx rows under it, and the lookup table the factory consults has no 500 key at
all — anything `>= 500` without a class of its own falls back to `ServerError`
(below 500 the fallback is the generic `HttpStatusError`). So
`except ServerError` catches all four of the 5xx rows above, plus any 5xx Root
starts returning that rootpy does not yet name. 500 carries its own
`description` and `hint` like every other listed code, so the fallback is not
a bare class:

```text
ServerError [HTTP_500/500]: Root encountered an internal server error.
  operation: CommunityCreate
  hint: Retry conservatively; persistent errors are likely server-side.
```

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

## Call permission errors

Not every refusal comes off the wire. `user.mute(ctx)` / `unmute` / `deafen` /
`undeafen` / `kick` check the caller's channel permissions **locally**, against
the cached community state, before spending a round trip. A missing-permission
refusal is therefore a routine expected outcome of moderating someone in a
call, not a bug — so these inherit `RootError` and a library-wide
`except RootError` catches them.

All of that presumes a call is already up. With **no active call session at
all**, these five raise a bare `RuntimeError("No active call session")` out of
`CallService.require_active_target` before any permission check runs, and that
is the most common way they fail: a bot that is not in a call never reaches the
permission check. (`user.kick` on yourself is the exception; it rejects the
self-target first, so it still raises `ActionNotApplicable`.) That bare
`RuntimeError` is neither a `CallActionError` nor a `RootError`, so the
library-wide `except RootError` does **not** catch it. Catch `RuntimeError` if
you want both cases in one handler — `CallActionError` keeps `RuntimeError` in
its MRO, so the three below come along with it.

| Exception | Raised when |
|---|---|
| `MissingPermissions` | the caller does not hold the required flag; `exc.permission` is the flag (a `PermissionName` when rootpy knows it, else the raw string) |
| `ActionNotApplicable` | the action does not apply here — a call is active but it is not a *community* call, or the target is yourself (`user.kick` on yourself; use `ctx.hangup()` to leave) |
| `PermissionStateUnavailable` | the SDK cannot resolve the caller's permissions for the active voice channel, so it refuses rather than guessing |

`CallActionError` is their shared base and is exported from `rootpy`; catch it
when any of the three is an acceptable answer:

```python
from rootpy import CallActionError, MissingPermissions

try:
    await user.kick(ctx)
except MissingPermissions as exc:
    await ctx.send(f"I need {exc.permission} to do that.")
except CallActionError as exc:
    await ctx.send(str(exc))
```

It still subclasses `RuntimeError` as well, so handlers written against the
older shape keep working.

## Command errors

`CommandError` and its three subclasses come out of command dispatch:

| Exception | Raised when |
|---|---|
| `CommandNotFound` | the prefix matched but no command by that name is registered |
| `CommandCheckFailure` | a check refused the invocation — e.g. `owner_only` with no `user_id` for the client to compare against |
| `CommandArgumentError` | the raw text could not be parsed or converted into the command's parameters |

The important part is *where* you catch them. `process_commands` swallows
`CommandNotFound` silently — an unrecognised `>word` in chat is not an error —
and routes everything else to the `command_error` event rather than letting it
escape into the gateway loop:

```python
@client.event
async def on_command_error(event):
    # event.message is the Message, event.error the exception
    if isinstance(event.error, CommandArgumentError):
        await event.message.reply(f"Bad arguments: {event.error}")
```

So `except CommandError` around your own code is for handlers that invoke
commands themselves; for the normal message path, handle `on_command_error`.
These also inherit `RootError`.

## Broadcast errors

A fan-out over several accounts never raises: `MultiClientHost.broadcast`
returns one `Outcome` per account, carrying either the return value or the
original typed exception. Partial success ("3 of 5 joined") is the normal
result, so failure is something you read rather than something you catch.

```python
from rootpy import MultiClientHost, format_root_error

host = MultiClientHost.from_tokens(tokens)
async with host:
    results = await host.broadcast(lambda c: c.invites.join(code))

for name, outcome in results.items():
    if not outcome.ok:
        print(f"{name}: {format_root_error(outcome.error, verbose=True)}")
```

`outcome.error` is the real exception — `isinstance` and the tables above apply
to it exactly as they would to a raised one. `str(outcome)` gives the short
form (`"account2: GrpcUnauthenticated: ..."`).

For many accounts, aggregate rather than printing a line each:

```python
from collections import Counter

failures = Counter(
    type(o.error).__name__ for o in results.values() if not o.ok
)
for name, count in failures.most_common():
    print(f"{name}: {count}")
```

`host.status()` snapshots every account's `ready`/`error` state without making
a request — that is the one to check when you want to know whether a *client*
is broken rather than a particular call.

## Common failures

### `Unauthorized` / HTTP 401

The specific HTTP request was not accepted as authenticated. On a
`MultiClientHost`, first check whether the error affects one account or every
account — `host.status()` reports each one separately, and a broadcast of a
cheap authenticated call (`lambda c: c.whoami(refresh=True)`) identifies which
tokens are actually dead.

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

Status 8 never sets HTTP 429, so it gets no per-endpoint cooldown. It does
count in `client.timings()` under `rate_limited`, which used to read 0 while a
gRPC-only throttle was plainly happening.

### `GrpcUnavailable` / status 14

The service could not process the request at that time. This is generally a
transient connectivity/service condition; use bounded backoff rather than an
immediate retry loop. rootpy retries it for you.

Root also uses status 14 for at least one **permanent** failure: posting to a
channel you are not a member of comes back as UNAVAILABLE, not
PERMISSION_DENIED. Retrying cannot help — measured as the same 7 of 24
accounts failing at concurrency 8 and at 64, each after two retries and two
backoffs. For a call you know fails this way, pass `retry_statuses=` (to drop
14 from the retried set) or `should_retry=` to `GrpcWebTransport`.

## Gateway errors are events, not exceptions

The websocket has its own error channel, and nothing on it raises: a socket
fault has no call to raise into, so it arrives as a dispatched event.

A `ClientNotification` carries a `PacketErrorCode` in field 1
(`UNSPECIFIED = 0`, `SYNC_LOST = 1`), unrelated to the `ErrorCodeType` that
rides on API responses. The server only writes that field when it is non-zero
— `PACKET_ERROR_CODE_UNSPECIFIED` is skipped on the wire — so it is absent on
the overwhelming majority of frames, and its presence is the signal.

`SYNC_LOST` is the server saying our resume cursor is no longer valid. rootpy
logs it and dispatches `sync_lost` with the frame's sequence number:

```python
@client.event
async def on_sync_lost(sequence):
    log.warning("gateway cursor rejected at seq=%s", sequence)
```

`sequence` is the sequence of the frame that carried the notification, and can
be `None` when the frame has no sequence field.

This is the same condition close code **4016, "Sequence out of range"**
reports, but stated *in band and before the socket goes away* — it is the one
form of that signal that does not have to be inferred from close-code text.
It is an **extra** signal, not a replacement: the 4016 close-code path (drop
the cursor and resume fresh, and the `contention_pause` stand-off after
`contention_threshold` consecutive 4016 closes) still runs on its own. A
`SYNC_LOST` frame can also carry a packet, so dispatch continues normally
after the event — nothing downstream is skipped, and you are not expected to
reconnect by hand.

See [api.md](api.md#client) for `contention_pause`, and the `gateway_paused` /
`gateway_resumed` events that bracket that stand-off.

## Reading why a request was rejected

Root answers every rejected request with the same `INVALID_ARGUMENT (3)`
message — "One or more request values were rejected by Root" — and puts the
actual reason in the `root-exception-bin` trailer. rootpy decodes that into the
exception, so the field and validator appear in `str(exc)`:

```
CommunityCreate failed — INVALID_ARGUMENT (3): ...
  -> PictureHex: 'Picture Hex' must be 7 characters in length.
     You entered 6 characters. [ExactLengthValidator]
```

The same detail is available as data:

| attribute | meaning |
| --- | --- |
| `exc.validation_errors` | list of `{property_name, error_message, error_code}`; empty when Root sent no payload |
| `exc.payload_kind` | e.g. `request_validator_list`, `payment_error`, `username` |
| `exc.payload` | the decoded payload for that kind |
| `exc.error_code` | Root's own error code, finer than the gRPC status |
| `exc.root_exception` | the whole decoded structure, or `None` |

## HTTP 401 is not gRPC UNAUTHENTICATED

An `Unauthorized` (HTTP 401) means the request was refused *before* gRPC —
almost always because no authorization header was sent. `GrpcUnauthenticated`
(status 16) means a header was sent and rejected. If only some endpoints 401
while others succeed on the same token, the cause is a request path that is not
attaching the header, not a permissions problem.

## Format errors are raised locally

Usernames, nicknames and hex colours are checked before the request goes out,
so those raise `ValueError` with the rule in the message rather than costing a
round trip. See `rootpy.validation`.
