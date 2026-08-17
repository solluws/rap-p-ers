# rootpy – Python Client for the Root Platform

**rootpy** is a community‑developed Python library that provides a programmatic interface to the Root platform. It is built by reverse‑engineering the public gRPC‑Web endpoints used by the official desktop client, and it is intended for legitimate automation of your own account.

> **Disclaimer:** This project is **not** an official SDK. It is a third‑party tool created through community research. The Root developers are aware of such projects and have historically tolerated them, provided they are used responsibly and do not violate the platform’s [Terms of Service](https://rootapp.com/terms).

---

## Purpose and Responsible Use

This library exists to give power users and developers the ability to:

- Automate routine tasks on their own account (e.g., sending messages, updating profile information).
- Build integrations that interact with Root in a programmatic way.
- Experiment with the platform’s capabilities in a safe, non‑disruptive manner.

**This library must not be used for:**

- Spamming, harassing, or otherwise abusing other users.
- Attempting to bypass security controls or access unauthorised data.
- Creating fake accounts, engaging in username sniping, or any form of ban evasion.
- Any activity that would violate Root’s Terms of Service.

The project maintainers and the wider community do not endorse any malicious use. You are fully responsible for how you use this library.

---

## Getting Started

Install the library:
```bash
pip install rootpy```
Basic usage:

```python
from rootpy import RootClient

client = RootClient()
await client.start("your_username", "your_password")
await client.send_message("user_id_or_mention", "Hello, world!")```

For more examples, please refer to the examples/ folder (if available).

Customisation and Extensibility:
The library is intentionally modular and exposes both low‑level protocol primitives and higher‑level convenience wrappers. You are welcome to adapt it to your own workflow, as long as your modifications remain within the bounds of responsible use.

We encourage you to:

Wrap the RootClient with methods that suit your specific needs.

Add caching or state management to improve performance.

Refactor code for readability or personal preference.

We do not encourage:

Adding features that enable mass messaging, automated voice‑call spamming, or any form of harassment.

Distributing modified versions that claim to be official.

Documentation
Inline help is available via help(rootpy). For a deeper dive into the underlying RPC services, you can explore the RawAPI and StructuredAPI surfaces.

Contributing
Contributions that improve the library’s reliability, performance, or documentation are welcome. However, we will not accept changes that facilitate abuse or violate the platform’s policies. Please open an issue first to discuss any significant changes.

Acknowledgements
This project would not exist without the efforts of the reverse‑engineering community and the transparency of the Root platform’s publicly accessible endpoints. We are grateful to the Root team for their work and for allowing this kind of community innovation to coexist with their official clients.

Use responsibly, and respect your fellow users.

## Multiple token sessions

`RootClient` can now start from an existing client token:

```python
from rootpy import RootClient

bot = RootClient(token="YOUR_TOKEN")

@bot.event
async def on_ready(event):
    print("ready")

bot.run()
```

For a small set of accounts you control, `RootClientPool` runs their independent
gateway/API sessions concurrently:

```python
from rootpy import RootClientPool

pool = RootClientPool(
    ["TOKEN_1", "TOKEN_2"],
    max_clients=10,
)

@pool.event
async def on_ready(event):
    print("client ready")

pool.run()
```

Each entry in `pool.clients` is a normal `RootClient`, with its own transport,
session, caches, gateway, services, commands, and event dispatch. The pool is a
lifecycle manager; it does not include a bulk-action/broadcast helper.


## Broadcast pool commands and terminal console

A pool command is normally registered on every client but executes only on
the account that receives it:

```python
@pool.command()
async def ping(ctx):
    await ctx.send("pong")
```

Set `broadcast=True` to execute the callback concurrently once per client in
the pool:

```python
@pool.command(broadcast=True)
async def inspect(ctx, text: str):
    print(
        ctx.client_index,
        ctx.client.user_id,
        text,
    )
```

Each invocation receives a `BroadcastContext`:

- `ctx.client` is the RootClient for the current pool account.
- `ctx.client_index` is its zero-based pool index.
- `ctx.pool` is the owning `RootClientPool`.
- `ctx.source` is the original Root command `Context`, or `None` for a
  terminal invocation.
- `ctx.is_console` tells you whether the command came from the terminal.

Message-triggered broadcast commands retain the original message destination,
so normal context helpers are available. Terminal commands have no message
container; use explicit APIs through `ctx.client` when a destination is
required.

Enable the interactive console with:

```python
pool.run(console=True)
```

Built-in console commands:

```text
help
status
commands
quit
exit
```

Any command registered with `broadcast=True` is also available directly:

```text
rootpy> inspect "hello"
Broadcast complete: 2 succeeded, 0 failed
```

Broadcast execution uses `asyncio.gather`, so all pool clients run the callback
concurrently rather than one after another.

If the same non-owner-only command message is observed by several pool clients,
RootPy deduplicates it by message ID for 30 seconds to prevent an N-by-N
duplicate broadcast.


### Broadcast staggering

Broadcast commands are concurrent by default:

```python
@pool.command(broadcast=True)
async def refresh(ctx):
    await do_something(ctx.client)
```

All pool clients are scheduled immediately.

To stagger clients, set `delay` on the command:

```python
@pool.command(broadcast=True, delay=0.25)
async def refresh(ctx):
    await do_something(ctx.client)
```

Scheduling becomes:

```text
client 0  0.00s
client 1  0.25s
client 2  0.50s
client 3  0.75s
...
```

The implementation still creates independent asyncio tasks for every client.
The delay uses `await asyncio.sleep(...)`, so it does not block the event loop.

The terminal can override the decorator setting for one invocation:

```text
rootpy> --delay 1.0 refresh
rootpy> --delay=0 refresh
```

`--delay=0` forces a staggered command to start every client concurrently.

For programmatic execution:

```python
await pool.execute_console_command(
    "refresh",
    delay=0.5,
)
```

`RootClientPool` now defaults to `max_clients=10000`; lower it explicitly if
you want a deployment-specific safety ceiling. Large pools remain asyncio-task
based rather than using one OS thread per client.


## High-level direct messages

`User.dm()` resolves or creates the correct direct-message container and sends
the message into it:

```python
user = client.get_user("00308891-cb93-8201-ad8d-22bd412ed25f")
await user.dm("hello")
```

It supports the same optional attachment/reply/delete-after arguments as
`client.dm.send(...)`.

## Large token pools

`RootClientPool` now uses lightweight startup by default and shards API HTTP
transports rather than allocating one `httpx.AsyncClient` per Root account.

```python
pool = RootClientPool(
    tokens,
    preload_caches=False,
    startup_concurrency=100,
    startup_delay=0.01,
    transport_shards=16,
)
```

`preload_caches=False` still authenticates each token and loads `user_id`, but
skips the expensive full community and DM cache warm-up. Community and DM APIs
remain available on demand.

Before an operation where token health matters, you can validate the pool:

```python
results = await pool.validate_clients(concurrency=100)

for result in results:
    if not result.succeeded:
        print(result.index, result.error)
```


## Easier error handling

RootPy transport errors are typed and now include readable descriptions and
recovery hints:

```python
from rootpy import RootError, format_root_error

try:
    await client.invites.join("invite-code")
except RootError as exc:
    print(format_root_error(exc, verbose=True))
```

Pool results support the same formatter:

```python
for result in results:
    if not result.succeeded:
        print(result.format_error(verbose=True))
```

For large broadcasts:

```python
print(pool.summarize_errors(results))
```

See `rootpy/docs/errors.md` for the complete HTTP/gRPC error reference.


## Pool startup gating

When `pool.run(console=True)` is used, RootPy now completes the initial startup
attempt for every configured client before accepting terminal commands.

Example startup output:

```text
[RootPy] Initializing 200 clients. Commands will become available after startup completes...
[RootPy] Starting clients: 0/200 complete | ready=0 | authenticating=100 | pending=100 | failed=0
[RootPy] Starting clients: 97/200 complete | ready=95 | authenticating=100 | pending=3 | failed=2
[RootPy] Starting clients: 200/200 complete | ready=196 | authenticating=0 | pending=0 | failed=4

[RootPy] Initial pool startup complete.
  configured: 200
  ready:      196
  failed:     4
  note: failed clients are excluded from broadcast commands.

rootpy>
```

Broadcast commands automatically target clients that completed the initial
startup successfully. Failed clients are excluded rather than producing
`Client is not logged in` command errors.

`status` now prints a summary; use `status --all` for per-client state.

Programmatic users can wait explicitly:

```python
pool_task = asyncio.create_task(pool.start())
await pool.wait_for_startup()

print(pool.startup_counts)
```


### Python 3.9 event-loop compatibility

`RootClientPool` now creates `asyncio.Event`, `Lock`, and `Semaphore` instances
inside the running event loop rather than in the pool constructor. This fixes:

```text
RuntimeError: got Future <Future pending> attached to a different loop
```

when a pool is created at module scope and later started with:

```python
pool.run(console=True)
```

No application-code change is required.


## Pre-lazy rollback build (v7)

This build restores the SDK's network/session implementation from before the
lazy-loading and presence/reconnect experiments.

The following components are intentionally unchanged from that pre-lazy build:

- `RootClient.login_token()`
- `RootClient._initialize_authenticated_state()`
- `GetSelf` during authenticated initialization
- `AuthClient.session_from_token()`
- `Gateway`
- Root protocol keepalive behavior
- WebSocket connection parameters
- hub endpoint handling
- user high-level helpers including `User.dm()` and `User.call()`

Only two pool-level adjustments are included:

1. `RootClientPool.fast_stable(tokens)` selects 50 concurrent startup slots,
   no fixed startup delay, and the original transport defaults.
2. Clients waiting for the startup semaphore remain reported as `pending`
   rather than incorrectly appearing as `authenticating`.

Use:

```python
pool = RootClientPool.fast_stable(
    TOKENS,
    startup_concurrency=50,
)
```

To reproduce the older default more closely:

```python
pool = RootClientPool(
    TOKENS,
    startup_concurrency=100,
    preload_caches=False,
)
```


## Leaving communities

The SDK exposes both client-level and object-level leave helpers.

```python
await client.leave_community(community_id)
```

If the community is cached/fetched:

```python
community = client.get_community(community_id)
if community is not None:
    await community.leave()
```

For a pool console broadcast:

```python
@pool.command(broadcast=True)
async def leave(ctx, community_id: str):
    await ctx.client.leave_community(community_id)
    print(
        f"[Client {ctx.client_index}] "
        f"left {community_id}"
    )
```

Then:

```text
rootpy> leave <community_id>
```
