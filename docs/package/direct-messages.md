# Direct messages

## Quick start

```python
import asyncio
from rootpy import RootClient

async def main():
    client = RootClient()
    await client.login("username", "password")
    try:
        # one call: opens (or reuses) the DM and sends
        await client.dm.send("00308891-cb93-8201-ad8d-22bd412ed25f", "hello")
    finally:
        await client.close()

asyncio.run(main())
```

You can pass a `User` object, a `CurrentUser`, anything with an `.id`, a
dashed UUID, or Root's 22-char base64url id.

## `client.dm` (DMMemberService)

- `await client.dm.send(user, "text", *, attachment_token_uris=None, parent_message_ids=None, delete_after=None)`
  Open/reuse the 1:1 DM and send. Returns a `MessageSendResult`.
- `await client.dm.open(user) -> DirectMessage`
  Get-or-create the DM (cache -> Find -> Create).
- `await client.dm.find(user) -> DirectMessage | None`
  Existing DM only; never creates.
- `client.dm.cached(user) -> DirectMessage | None`
  Local cache lookup, no network.
- `await client.dm.reply(message, "text", *, delete_after=None)`
  Reply inside the DM a received message belongs to (uses its container
  directly, so no member-set resolution is needed).

The low-level `client.direct_messages` (aka `client.dm_service`) still exposes
`list()`, `find()`, `create()`, `get_or_create()` if you need them.

## Why the old code failed with INVALID_ARGUMENT

Root addresses a DM by its **member set**. A 1:1 DM between you and another
user is the set `{self, other}` — not `{other}`. The previous code sent only
the target id, so both `Find` and `Create` described a conversation that
cannot exist and the server rejected them with a generic
`INVALID_ARGUMENT ("root-error")`.

The service now takes the caller's own id (the client supplies it as
`lambda: self.user_id`) and always builds the full `{self, other}` set. It
also:

- raises a clear `DirectMessageError` (not an opaque gRPC error) explaining
  the remaining real causes when Root still rejects a DM — bad target id, the
  target's friend/invite requirement, or a block;
- guards early with a readable error if you call before `login()` or pass a
  malformed id;
- logs requests and failures under the `rootpy.direct_messages` / `rootpy.dm`
  loggers (`logging.getLogger("rootpy").setLevel(logging.DEBUG)` to see them);
- treats a failed `Find` inside `get_or_create` as non-fatal and falls through
  to the idempotent `Create`. `NOT_FOUND` from Find (the normal
  "no DM exists yet" answer) is handled the same way: `find()` returns `None`
  and `get_or_create`/`dm.send` create the DM. Real failures (auth, rate
  limit, permission) still propagate.

Because `client.calls.call_user()` goes through `get_or_create`, voice calls to
a user are fixed by the same change.
