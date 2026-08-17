# rootpy

An async Python client for the [Root](https://rootapp.com) chat platform.

Reverse-engineered from the official desktop client, with the wire format
verified against decompiled protobuf schemas and a live traffic capture.
Covers messaging, communities, channels, roles, direct messages, friends,
presence, and the real-time gateway.

```python
import asyncio
from rootpy import RootClient

async def main():
    client = RootClient(token=TOKEN)
    await client.login_token()

    await client.messages.send(CHANNEL_ID, "hello", community_id=COMMUNITY_ID)

    await client.close()

asyncio.run(main())
```

---

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Logging in](#logging-in) — five modes, and what each costs
- [Sending and reading messages](#sending-and-reading-messages)
- [Events](#events)
- [Communities, channels, roles](#communities-channels-roles)
- [Direct messages and friends](#direct-messages-and-friends)
- [Your account](#your-account)
- [Watching for new messages](#watching-for-new-messages)
- [Errors](#errors)
- [Performance notes](#performance-notes)
- [Hosting several accounts](#hosting-several-accounts)
- [Included scripts](#included-scripts)
- [Adding a new RPC](#adding-a-new-rpc)
- [Testing](#testing)
- [Protocol notes](#protocol-notes)
- [Responsible use](#responsible-use)

---

## Install

```bash
git clone <your-repo-url> rootpy
cd rootpy
pip install -e .
```

Requires Python 3.10+. Dependencies: `httpx[http2]`, `websockets`.

Installing with `-e` matters: a bare `rootpy/` folder next to your script is
fragile — it can be shadowed by an unrelated installed package of the same
name, and it won't be found if your editor runs the script from a temp
directory.

## Quick start

```python
import asyncio
from rootpy import RootClient

TOKEN = "..."

async def main():
    client = RootClient(token=TOKEN)
    await client.login_token()

    me = await client.whoami()
    print(f"logged in as {me.username}")

    for community in await client.list_communities():
        print(community.name)

    await client.close()

asyncio.run(main())
```

---

## Logging in

There are five ways in, and they cost very different amounts. Measured on a
real account (2 communities, ~330 ms per round trip):

| mode | how | requests | time | can receive events? |
|---|---|---|---|---|
| **token-only** | just construct, then call | 1 (the call itself) | ~330 ms | no |
| **minimal** | `login_token()` + `preload_caches=False` | 2 | ~1065 ms | no |
| **lazy** *(default)* | `login_token()` | 3 | ~966 ms | no |
| **eager** | `expand_communities=True` | 3 + one per community | ~2596 ms | no |
| **gateway** | `login_token()` then `connect()` | 3 + websocket | ~1740 ms | **yes** |

**The websocket is optional.** Every RPC — sending, reading, admin, presence —
works over plain HTTPS. You only need `connect()` to *receive* pushed events.

### Token-only — one request, no login

`send()` needs nothing but the bearer token, so a fire-and-forget script can
skip login entirely:

```python
await RootClient.send_once(TOKEN, CHANNEL_ID, "hello", community_id=COMMUNITY_ID)
```

### Lazy (the default)

`GetSelf` and `ListMine` run in parallel, and per-community detail is fetched
on first access and cached. This is why lazy is *faster* than minimal while
doing more.

```python
client = RootClient(token=TOKEN)
await client.login_token()

detail = await client.community_detail(community_id)   # fetches once, then cached
```

### Eager — preload everything

Only worth it if you know you'll touch every community immediately:

```python
client = RootClient(token=TOKEN, expand_communities=True,
                    preload_direct_messages=True)
```

### Gateway — to receive events

```python
client = RootClient(token=TOKEN)
await client.login_token()
await client.connect()          # returns once the socket is up
```

> **Note:** `start_token()` is `login_token()` + `connect()` + *wait forever*.
> It never returns by design. If you need control flow back, use the two calls
> above.

---

## Sending and reading messages

```python
# by id
await client.messages.send(channel_id, "hello", community_id=community_id)

# or from an object
channel = ...                       # from a community fetch
await channel.send("hello")

# replies are threaded
await message.reply("got it")

# edit, react, pin, delete
await message.edit("updated text")
await message.react("👍")
await message.unreact("👍")
await message.pin()
await message.delete()
```

### History

One page:

```python
recent = await channel.history()                    # newest batch
recent = await client.messages.list(channel_id, community_id=community_id)
```

Or iterate as far back as you like — pagination is handled for you:

```python
async for message in channel.history_iter(limit=500):
    print(message.user_id, message.content)
```

---

## Events

Connect the gateway first (`await client.connect()`), then register handlers.

```python
@client.event
async def on_message(event):
    print(event.message.content)

@client.event
async def on_friend_request(event):
    print(f"{event.username or event.user_id} added you")
```

`@client.event` allows one handler per event (keyed on the function name). For
several handlers, or to register dynamically:

```python
client.add_listener("member_join", handler)
client.remove_listener("member_join", handler)
```

### Typed events

These arrive as objects with resolved names where known, plus `.received_at`
and `.raw`:

| event | object | useful fields |
|---|---|---|
| `on_message` | `MessageEvent` | `.message` |
| `on_friend_request`, `on_friend_remove` | `FriendEvent` | `user_id`, `username` |
| `on_member_join`, `on_member_leave`, `on_member_ban` | `MemberEvent` | `user_id`, `username`, `community_id/name` |
| `on_role_add`, `on_role_remove` | `MemberRoleEvent` | `role_id`, `role_name`, `user_ids` |
| `on_channel_create/_edit/_delete` | `ChannelEvent` | `name`, `category`, `community_name`, `is_text` |
| `on_role_create`, `on_role_delete` | `RoleEvent` | `name`, `color_hex`, `mentionable` |
| `on_block_add`, `on_block_remove` | `BlockEvent` | `user_id`, `username` |

Every gateway packet is also dispatched raw as `on_packet_<name>` (91 packet
types), with `pkt.fields` giving typed values and `pkt.get("field")` for one.

### Waiting for something

```python
event = await client.wait_for(
    "message",
    check=lambda e: e.message.user_id == someone,
    timeout=30,
)
```

Raises `asyncio.TimeoutError` if it doesn't arrive.

---

## Communities, channels, roles

```python
communities = await client.list_communities()
detail = await client.community_detail(community_id)

for group in detail.channel_groups:
    for channel in group.channels:
        print(group.name, channel.name, channel.is_text)

# or flattened
detail.channels          # every channel
detail.text_channels     # only ones that hold messages

# members of a server
members = await client.get_members(community_id)
count = await client.member_count(community_id)
member = await client.get_member(community_id, user_id)
admins = await client.members_with_role(community_id, role_id)
names = await client.member_names(community_id)   # {user_id: username or None}

# members WITH their profiles (username, pictures, about-me), batched
for member in await client.get_members_detailed(community_id):
    print(member.username, member.about_me)
    print(member.avatar_url, member.banner_uri, member.custom_status)

# or a single user's profile
profile = await client.get_profile(user_id)
profiles = await client.get_profiles([id1, id2, id3])   # one request

# pick someone at random -- excludes you, and comes with their profile
member = await client.get_random_member(community_id)
print(member.user_id, member.username)
print(member.avatar_url, member.banner_uri, member.about_me)
await member.add_role(role_id)                              # member actions too

member = await client.get_random_member(community_id, with_profile=False)  # id only
three  = await client.get_random_members(community_id, 3)

await member.add_role(role_id)
await member.kick()

# admin
channel = await client.community_admin.create_channel(
    community_id, group_id, "new-channel", channel_type=1,   # 1 = TEXT
)
role = await client.community_admin.create_role(community_id, "New Role")
await client.roles.add_to_members(community_id, role.id, [user_id])

await channel.delete()
await client.community_admin.delete_role(community_id, role.id)

# joining and leaving
await client.invites.join("INVITE-CODE")
await client.leave_community(community_id)
```

Channel types: `1` TEXT, `2` THREADED_TEXT, `4` VOICE, `8` APP. Only text
channels hold messages — message RPCs against voice/app channels fail
server-side.

---

## Direct messages and friends

```python
dm = await client.open_dm(user_id)
await dm.send("hey")
history = await dm.history()

# or in one step
await client.direct_message(user_id, "hey")

# friends
await client.friend_requests.send("someusername")
pending = await client.friend_requests.pending()
await client.friend_requests.accept(pending[0])
await client.friend_requests.decline(pending[0])

await client.list_friends()
await client.remove_friend(user_id)
await client.block(user_id)
await client.unblock(user_id)
```

---

## Your account

```python
await client.go_online()
await client.go_idle()
await client.go_invisible()
await client.set_online_status("online")     # or a UserOnlineStatus

await client.update_status("working on something")
await client.update_status(None)             # clear it

await client.update_username("newname")
await client.update_description("about me")

# profile picture -- accepts whatever you have to hand
await client.change_profile_picture("avatar.png")            # file path
await client.change_profile_picture(Path("~/pic.jpg"))       # Path
await client.change_profile_picture(image_bytes)             # raw bytes
await client.change_profile_picture(open("a.png", "rb"))     # file object
await client.change_profile_picture("https://.../a.png")     # URL
await client.remove_profile_picture()                        # clear it

await client.change_banner("banner.png")                     # same source types
```

> Root has no *do not disturb* state. Presence is active / inactive (idle) /
> disconnected (appears offline).

---

## Watching for new messages

Root pushes mentions and DMs, but **not** ordinary channel messages. To see
everything, `watch_unread()` combines both: it uses each channel's
`last_activity_at` vs `user_last_viewed_at` (the same signal behind the unread
dot in the official client) to spot changes, then fetches only what changed.

```python
await client.connect()          # mentions/DMs arrive instantly
client.watch_unread(interval=3.0, include_dms=True)

@client.event
async def on_message(event):
    print(event.message.content)   # both sources land here, deduplicated
```

One `GetExtended` per community per sweep, run concurrently; only channels
whose activity advanced get a message fetch. Mentions are instant; ordinary
channel messages appear within roughly `interval` seconds.

To watch a single channel instead: `client.watch_channel(channel_id)`.

---

## Errors

Every failure raises a typed exception carrying Root's structured error detail
when the server provides one.

```python
from rootpy.exceptions import GrpcWebError, format_root_error

try:
    await client.messages.send(channel_id, "hi", community_id=community_id)
except GrpcWebError as exc:
    print(exc.status.name)          # PERMISSION_DENIED
    print(exc.error_code)           # named ErrorCodeType, e.g. NO_PERMISSION_TO_BAN
    print(exc.payload_kind)         # username / email / friend_user / payment_error / ...
    for error in exc.validation_errors:
        print(error.property_name, error.error_code, error.error_message)
    print(format_root_error(exc, verbose=True))
```

Rate limits are handled for you: a `429` records a per-endpoint cooldown that
*all* callers respect, rather than each retrying into the same wall.

---

## Performance notes

Measured against a live account, ~330 ms per round trip:

- **Login:** lazy ~966 ms, eager ~2596 ms. Community expansion is the cost, and
  since those calls run concurrently the saving stays roughly constant as
  community count grows.
- **Sending:** ~170 ms on a warm client; ~700 ms for `send_once` (a fresh
  client pays the TLS handshake).
- **Send → readable:** a message takes roughly **500–700 ms** to become visible
  in history. If you write then immediately read, expect to poll.

---

## Hosting several accounts

One Python process costs ~30 MB before it does anything — interpreter, imports,
the SDK. Running one process per account pays that every single time:

| accounts | one process each | one process total |
|---|---|---|
| 5 | ~149 MB | ~30 MB |
| 20 | ~595 MB | ~30 MB |
| 50 | ~1,490 MB | ~30 MB |

Everything expensive is shared, and each extra account adds well under a
megabyte. So run them together:

```python
from rootpy import MultiClientHost

async def setup_alice(client):
    @client.event
    async def on_message(event):
        ...                        # alice's own handlers

host = MultiClientHost(stagger=0.5)
host.add("alice", ALICE_TOKEN, setup=setup_alice)
host.add("bob", BOB_TOKEN, setup=setup_bob)
await host.run()
```

Each account stays **independent**: its own client, handlers, credentials,
rate-limit budget, and failures. A bad token or a disconnect on one account
doesn't disturb the others — `host.status()` reports each one separately.

Two options worth knowing:

- `gateway=False` per account skips the websocket for accounts that only make
  API calls, saving a connection and the keepalive traffic.
- `shared_transport=True` puts every account on one connection pool. It's off
  by default on purpose: a shared pool also shares the rate-limit cooldown
  table, so one account's `429` would pause everyone else's calls to that
  endpoint.

`multihost.py` is a ready-to-run version reading accounts from `accounts.txt`.

---

## Where the time goes

Every request is timed, split into the three parts that are actually
distinguishable from inside the process:

```python
print(client.timing_report())
```

```
12 request(s): 2043ms round trip, 0ms waiting (rate limits/retries), 3ms client overhead
  endpoint                                     calls   median     total
  root.v2.MessageGrpcService/List                  4     181m      724m
  root.CommunityGrpcService/GetExtended            3     166m      498m
```

- **round trip** — network *and* server together. These can't be separated
  from the client; on a connection's first request it also includes DNS and
  the TLS handshake, which is why one call often looks far worse than the rest.
- **waiting** — time this client deliberately held a request: rate-limit
  cooldowns and retry backoff. Entirely our own doing.
- **overhead** — our framing and bookkeeping. Usually a rounding error.

`client.timings()` returns the same data as a dict (with per-endpoint median,
min, max, error and retry counts), and `client.reset_timings()` clears the
counters so you can measure one specific operation.

---

## Included scripts

| script | what it does |
|---|---|
| `example.py` | live monitor — prints every message/event, INFO concise, DEBUG verbose |
| `fullrun.py` | 30-step end-to-end test of the whole SDK, with timings |
| `twotest.py` | two accounts: one sends, one watches — verifies delivery + latency |
| `benchmark.py` | eager vs lazy login, per account, per endpoint |
| `logintimes.py` | times all five login modes |
| `sendone.py` | send one message in a single request |
| `diagnose.py` | RPC troubleshooter — isolates a failing call |
| `multihost.py` | runs several accounts in one process |

Each reads credentials from `tokens.txt` next to it:

```
token=<your token>
token2=<second account, for benchmark>
watcher=<for twotest.py>
sender=<for twotest.py>
```

**Never commit `tokens.txt` or `accounts.txt`** — it's in `.gitignore`, keep it that way.

---

## Adding a new RPC

The pattern, using the decompiled schemas:

```python
MY_ENDPOINT = "https://api.rootapp.com/root.SomeGrpcService/Method"

async def my_call(self, some_id: str) -> Something:
    body = bytearray()
    body += length_field(10, encode_root_guid(normalize_root_guid(some_id)))
    body += field_key(11, 0) + encode_varint(42)

    response = await self.transport.unary(
        endpoint=MY_ENDPOINT,
        body=bytes(body),           # raw protobuf -- do NOT frame it yourself
        headers=self._headers(),
        operation="SomeMethod",
    )
    payload = unwrap_grpc_web(response.content)   # unwrap BEFORE parsing
    ...
```

Two rules, both learned the hard way:

1. **Pass the raw protobuf body.** The transport adds the gRPC-web frame. An
   unframed body reaches the server as garbage and the handler throws a bare
   `UNKNOWN (2) Exception was thrown by handler` — which looks exactly like a
   permissions failure and is miserable to diagnose.
2. **Unwrap responses with `unwrap_grpc_web()`.** Parsing `response.content`
   directly hits the 5-byte frame header and reports nonsense wire types.

Field numbers come from the generated C# messages: the tag in
`WriteRawTag(N)` / `case Nu:` is `(field_number << 3) | wire_type`, so tag 82 →
field 10, wire type 2.

---

## Testing

```bash
python -m pytest tests/ -q
```

51 tests, each one locking in a bug that was expensive to find:

- `test_wire.py` — request framing (the big one)
- `test_protocol_schemas.py` — packet types, enums, error payloads
- `test_client_behaviour.py` — dedup, cursors, request shape, channel filtering
- `test_features.py` — typed events, `wait_for`, presence, object methods, rate limits
- `test_cache_and_history.py` — caching, lazy init, history pagination

---

## Protocol notes

Findings that aren't obvious from the API:

- **The gateway is a resync cycle**, not a persistent stream. It sends a batch,
  closes with code `1000`, and expects you to reconnect. That's normal, not an
  error.
- **Mentions and DMs are pushed; ordinary channel messages are not.** A
  notification embeds the whole originating message — content, author, and the
  author's display name.
- **`MessageList` must not include `Limit`**, and must always include `DateAt`.
  Sending `Limit` is rejected.
- **Usernames aren't on messages or members** — only `User` objects carry them.
  The cache learns names from notification payloads.
- **`UserOnlineStatus.ACTIVE` is `0x10`**, not a small integer.
- **Channel `ContainerId` equals its `Id`.**

---

## Responsible use

This is an unofficial client. A few things worth being deliberate about:

- **It automates one account — your own.** That's what it's built for, and what
  it stays good at.
- **Check Root's terms of service.** Automating an account may not be permitted;
  that's your call to make knowingly.
- **Don't commit tokens.** A Root token is full account access.
- **Be a decent neighbour to the API.** The defaults (rate-limit cooldowns,
  lazy loading, sensible sweep intervals) exist so this doesn't hammer anyone's
  servers — don't tune them to eleven.

Not affiliated with or endorsed by Root.

## License

Add one before publishing — MIT is the usual choice for something like this.
