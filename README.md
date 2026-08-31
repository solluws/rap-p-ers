# rootpy

[![tests](https://github.com/solluws/rootpy/actions/workflows/tests.yml/badge.svg)](https://github.com/solluws/rootpy/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

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

> **Building with an AI assistant?** Paste
> [`LLMS.md`](LLMS.md) into it first — it covers the mental model, what to use
> when, the performance characteristics, and the protocol traps that are
> impossible to guess.

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
- [Discovering the API](#discovering-the-api) — `explain` and `preview`, offline
- [Adding a new RPC](#adding-a-new-rpc)
- [Testing](#testing)
- [Protocol notes](#protocol-notes)
- [LLMS.md](LLMS.md) — context for AI assistants
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

The fastest way to see what's available:

```bash
python example.py --list                        # list the sections
python example.py --token --only messages roles # run them
```

Or in code:

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
real account with 2 communities, on a slower connection than the live suite
normally sees — ~330 ms a round trip against the ~190 ms in
[Performance notes](#performance-notes) — so read the figures below as ratios
rather than absolutes:

| mode | how | requests | time | can receive events? |
|---|---|---|---|---|
| **token-only** | just construct, then call | 1 (the call itself) | ~330 ms | no |
| **minimal** | `login_token()` + `preload_caches=False` | 2 | ~1065 ms | no |
| **lazy** *(default)* | `login_token()` | 3 | ~966 ms | no |
| **eager** | `expand_communities=True` | 3 + one per community | ~2596 ms | no |
| **gateway** | `login_token()` then `connect()` | 3 + websocket | ~1740 ms | **yes** |

**The websocket is optional.** Every RPC — sending, reading, admin, presence —
works over plain HTTPS. You only need `connect()` to *receive* pushed events.

**Bringing up many accounts:** `RootClient(defer_hub=True)` drops the hub
endpoint lookup from login and fetches it inside `connect()` instead, when
something actually wants a socket. Login goes from 3 hops to 2, measured
~39/s to ~61/s across 1,307 accounts. Use it when you build many clients and
some of them may never connect.

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

`connect()` also announces this device as Active, which is one of the two
halves of presence — see [Your account](#your-account). Pass
`announce_device=False` to skip it and announce it yourself.

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

# reply to up to 5 messages with one message
await client.reply_to([first, second, third], "answering all three")
await first.reply_with([second, third], "same thing")
await channel.reply_to([msg_a, msg_b], "answering both")

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

These arrive as objects with resolved names where known, plus `.raw`. Every
event built from a named packet — every row below *except* `on_message` — also
carries `.received_at`, a timezone-aware `datetime`. `MessageEvent` does not:
it comes off the gateway's own message path rather than the typed-event
builder, and carries `.action` and `.sequence` instead:

| event | object | useful fields |
|---|---|---|
| `on_message` | `MessageEvent` | `.message`, `.action` (create/edit/delete), `.sequence` (`None` when the message came from a poll, not the socket) |
| `on_friend_request`, `on_friend_remove` | `FriendEvent` | `user_id`, `username` |
| `on_member_join`, `on_member_leave`, `on_member_ban`, `on_member_unban` | `MemberEvent` | `user_id`, `username`, `community_id/name`, `role_ids`, `presence` |
| `on_member_online`, `on_member_offline` | `MemberEvent` | same fields, `presence=True` |
| `on_role_add`, `on_role_remove` | `MemberRoleEvent` | `role_id`, `role_name`, `user_ids` |
| `on_channel_create/_edit/_delete` | `ChannelEvent` | `name`, `category`, `community_name`, `is_text` |
| `on_role_create`, `on_role_edit`, `on_role_delete`, `on_role_move` | `RoleEvent` | `name`, `color_hex`, `mentionable`, `before_role_id` |
| `on_reaction_add`, `on_reaction_remove` | `ReactionEvent` | `shortcode`, `message_id`, `user_id`, `container_id`, `is_direct` |
| `on_block_add`, `on_block_remove` | `BlockEvent` | `user_id`, `username` |

**Presence is not membership.** `COMMUNITY_MEMBER_ATTACH` (5502) and
`COMMUNITY_MEMBER_DETACH` (5503) are what a client sends when it opens or
closes a community — the app feeds an attach straight into
`SetAttached(userId, true, OnlineStatus)` — so they fire `on_member_online`
and `on_member_offline`. Only the genuine membership packets
`COMMUNITY_JOINED` and `COMMUNITY_LEAVE` fire `on_member_join` and
`on_member_leave`. `MemberEvent.presence` tells the two families apart, so one
handler can take both and branch. If you previously used `on_member_join` to
notice people, you were watching *anyone opening the community*, not anyone
joining it — that is the behaviour change.

**`is_text` is three-valued on channel events**, not two. Only
`ChannelCreatedPacket` carries `ChannelType` (field 12); the edited and deleted
packets do not, so `ChannelEvent.is_text` returns `None` when nothing knows the
type. The builder fills it in from the client's channel cache first — the
gateway dispatches `packet` before a delete evicts the entry, so the cache is
usually still warm — and only an uncached channel yields `None`. Test with
`is True` / `is None`, not truthiness, or the unknown case reads as "not a
text channel".

**Reactions carry the container, not just the message.** One packet serves
reaction add and remove, for channels and for direct messages alike, so
`ReactionEvent.is_direct` says which kind `container_id` names. A direct-message
reaction has no `community_id`; without that flag it would look like a channel
reaction in a community you cannot look up.

Every gateway packet is also dispatched raw as `on_packet_<name>`. There are 90
of those: `PacketType` has 91 members, but `UNKNOWN` is guarded out of the
`on_packet_*` fan-out and arrives as `on_unknown_packet` instead, so a packet
this library has never seen still reaches you. `pkt.fields` gives typed values
and `pkt.get("field")` fetches one; `pkt.fields["packet_type"]` is the app's own
`PacketType` on every packet, which is how a role edit (5402) is told apart
from a role create (5401).

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

# asset URIs -> real https links
profile = await client.get_profile(user_id)

url = await client.asset_url(profile.banner_uri)       # best available size
urls = await client.asset_urls([uri1, uri2, uri3])     # many, batched

# full detail: every size Root offers, plus expiry
asset = await client.get_asset(profile.avatar_url)
asset.best_url            # highest resolution
asset.url_for_size(128)   # smallest link at least 128px across
asset.links               # [AssetLink(url, max_dimension, width, height), ...]
asset.expires_at          # these links are short-lived
asset.is_animated

# links expire, so keep the bytes if you need them permanently
await client.save_asset(profile.avatar_url, "avatars/alice")   # -> avatars/alice.png
await client.save_profile_images(profile, "images/")

# or a single user's profile
profiles = await client.get_profiles([id1, id2, id3])   # one request

# pick someone at random -- excludes you, and comes with their profile
member = await client.get_random_member(community_id)
print(member.user_id, member.username)
print(member.avatar_url, member.banner_uri, member.about_me)
await member.add_role(role_id)                              # member actions too

# several at once -- profiles fetched in ONE batched request
for member in await client.get_random_members(community_id, 5):
    print(member.username, member.about_me)

# ids only, no profile requests
member = await client.get_random_member(community_id, with_profile=False)
three  = await client.get_random_members(community_id, 3, with_profile=False)

await member.add_role(role_id)
await member.kick()

# admin
channel = await client.community_admin.create_channel(
    community_id, group_id, "new-channel", channel_type=1,   # 1 = TEXT
)
role = await client.community_admin.create_role(community_id, "New-Role")
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
await client.set_presence("online")          # or "idle" / "invisible"
await client.go_online()
await client.go_idle()
await client.go_invisible()
await client.set_online_status("online")     # alias for set_presence

client.presence                              # what others see, min(ceiling, device)

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

**Presence is two independent values, and the lower one wins.** What other
people see is `min(ceiling, device)`:

| half | call | means |
|---|---|---|
| ceiling | `SetMaxOnlineStatus` | the most you are willing to appear as |
| device | `SetDeviceOnlineStatus` | what this connection says it is doing |

Both calls succeed on their own, so a ceiling of Active with no device
announcement leaves the account **offline to everybody** and reports success
while it does. `set_presence` sets both halves, and raises if the ceiling
lands but the device does not — so it cannot happen quietly. `connect()`
announces the device too.

For one half on its own, use `client.users.set_online_status(s)` (ceiling) or
`client.user_settings.set_device_online_status(s)` (device).

### Watching somebody else's presence

```python
watch = client.watch_presence(
    lambda user_id, status: print(user_id, status),
    users=[user_id],
    poll=5,                    # seconds; omit for push only
)
...
await watch.stop()
```

Two routes, with different requirements. **Push** is instant and free, but the
hub only sends `USER_SET_STATUS` for communities this connection has attached
— see [Protocol notes](#protocol-notes). **Poll** needs no attach and no
socket, and reads every watched user in one request per tick. Push is always
on; `poll=` adds the second route.

---

## Watching for new messages

Root pushes mentions and DMs to any connected account. **Channel messages push
too — but only for communities you have attached.** Membership is not a
subscription: until you attach, the hub sends nothing for that community, which
is what makes a listener look broken when it is merely unsubscribed.

`UnreadReader` handles all of it — attach, listen, read, clear:

```python
from rootpy import UnreadReader

await client.connect()

async def handle(message):
    print(message.content)

async with UnreadReader(client, on_message=handle):
    await asyncio.Event().wait()
```

It attaches to every community you are in, does one pass to clear whatever went
unread while it was down, and then **makes no requests at all** until something
arrives. Measured on 3 communities × 3 channels with a second account posting:
**9/9 read, median 0.22 s**, one request per message — the click, not a poll.
`devscripts/unreadtest.py` runs that scenario end to end.

Two things worth knowing:

- **Attaching is visible.** Other members see you as present in the community
  (`COMMUNITY_MEMBER_ATTACH`). `UnreadReader` detaches again on exit; pass
  `detach_on_stop=False` to stay attached, or `attach=False` to never do it.
- **The subscription lives with the hub connection**, so it has to be redone
  after a reconnect. The reader watches for that and re-attaches itself.

If you need to stay out of the presence list, the older polling path is still
there: `client.watch_unread(interval=3.0)` sweeps `last_activity_at` vs
`user_last_viewed_at` instead, one `GetExtended` per community per tick. To
watch a single channel that way: `client.watch_channel(channel_id)`.

### Built on this: the ophanim project

Three tools that use this library rather than being part of it live in a
separate folder, `visjs/`:

| | |
|---|---|
| `ophanim/ophanim.py` | archives every message and major event to `ophanim.json`, and indexes activity — messages/day, active users/day, density |
| `ophanim/accounts.py` | every member of every community the account can reach, with about-mes and roles |
| `ophanim/discover.py` | finds communities the account is *not* in, from invite links in archived messages and about-mes |
| the site | a zero-dependency Node app over all of it, plus this library's own surface |

They are kept out of this repository's root on purpose: they are an
application, they need credentials, and their output contains real message
content. See `visjs/README.md`.

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

`error_code` is a named `ErrorCodeType` — 50 of them, including the two WebRTC
refusals `WEB_RTC_BACKEND_MISMATCH` (6000) and `WEB_RTC_CALL_BANNED` (6001). A
code Root has added since this library was generated comes back as the plain
integer rather than raising, so `exc.error_code` is always safe to print.

`RootError` is the base worth catching for anything this library *defines*: all
54 of its exception classes descend from it — including the four that used to
sit outside the tree:
`CallActionError` (the base of the in-call moderation refusals from
`user.mute(ctx)` / `user.kick(ctx)`, and now exported from `rootpy` directly),
`CommandError`, `MediaDependencyMissing` and `AlreadyCreatedError`. Being
refused a mute is a routine outcome, not a bug, so it has to be catchable
alongside the wire errors. Nothing that caught them before stopped working:
`CallActionError` and `MediaDependencyMissing` kept `RuntimeError` as a second
base, so an existing `except RuntimeError` around a mute or a missing `[voice]`
extra still catches what it caught before, and the other two were plain
`Exception` subclasses that `RootError` still satisfies:

```python
from rootpy import RootError

try:
    await user.kick(ctx)
except RootError as exc:      # GrpcWebError, MissingPermissions, rate limits, ...
    print(exc)
```

It is not a universal catch-all, though, and that snippet is a good place to see
why. Where no typed class exists the library still raises a plain
`RuntimeError`: `connect()` without a session, `wait_until_ready()` before
connect, `user.kick(ctx)` with no active call — the very call above, outside a
call — `set_presence()` when the ceiling lands but the device announcement
fails, and the "server answered with something we can't parse" paths in
`auth.py` and the service modules. A transport failure that outlives
`max_retries` is re-raised as the underlying `httpx` exception
(`TimeoutException`, `NetworkError`, `RemoteProtocolError`) rather than wrapped.
That escape applies inside an active call too — `user.kick(ctx)` reaches the
wire through `calls.kick` like any other request — so the snippet above is
narrower than it looks. Argument validation escapes as well, before a request
is ever made: `messages.send("not-a-guid", ...)` raises a plain `ValueError`.
There is no short tuple that covers all of it. `except Exception` is the only
true backstop; [LLMS.md §11](LLMS.md) has the counts and says which layer
raises what.

Rate limits are handled for you: a `429` records a per-endpoint cooldown that
*all* callers respect, rather than each retrying into the same wall.

Root also throttles with gRPC status 8 (`RESOURCE_EXHAUSTED`), which never
sets a `429`. That one gets no cooldown, but it now counts in
`stats.rate_limited` — the figure used to read 0 while calls were plainly
being throttled.

Root returns gRPC status 14 (`UNAVAILABLE`) for some **permanent** failures,
such as posting to a channel you are not a member of. The default retry set
includes 14, because it is usually transient, so a call that can never
succeed still pays two retries. Pass `retry_statuses=` or `should_retry=` to
`GrpcWebTransport` for calls you know will not improve.

---

## Performance notes

Measured against a live account. The most recent timing pass averaged **~190 ms**
round trip over ~850 calls — 188–199 across three runs, 332 tests at the time of
the measurement and 352 now. That supersedes the ~213 ms over 496 calls this
section used to quote, which is the earlier run; [LLMS.md §19](LLMS.md) carries
both and says which is which. Read every total as of its own run — the live
suite keeps growing — and note that the per-call figure moved too, so it is not
the stable part either. The login numbers below were taken on a slower
connection at ~330 ms, so read those as ratios rather than absolutes:

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

To do the *same* thing as every account, `broadcast` an action. It returns one
outcome per account and **never raises**, because partial success ("3 of 5
joined") is the normal case for a fan-out, not an exception — an account that
cannot join answers with a refusal, and that must not cost you the other
results:

```python
host = MultiClientHost.from_tokens([TOKEN_A, TOKEN_B])
async with host:
    results = await host.broadcast(lambda c: c.invites.join(code))
    for outcome in results.values():
        print(outcome)  # "account1: ok" / "account2: GrpcUnauthenticated: ..."
```

`broadcast` runs the accounts concurrently (HTTP/2 multiplexes, so N accounts
cost barely more wall time than one — measured at **1.10x** for a two-account
fan-out against a single call, 190 ms vs 172 ms) and sequentially *within* each
account; `only=`, `concurrency=` and `timeout=` bound it. `host.join(code)` is
the named shorthand for the example above.

`concurrency` defaults to `None`, which scales with the number of accounts and
caps at 64. It used to be a flat 8, which costs 28 s a command at 1,089
accounts; a host of 8 accounts or fewer is unchanged. Measured across 1,089
accounts for one command: 8 → 28.3 s, 32 → 7.3 s, 64 → 3.8 s, 128 → 2.0 s.

`rootpy.host.MEASURED_CEILINGS` records what one IP gets from Root, and the
numbers are not interchangeable: ~120/s for plain calls, ~60/s for login (two
gathered calls), ~87/s for attach, and only ~17/s for websocket handshakes.
Size socket work from the last one, not the first.

> **Which refusal you get is not what you would guess.** Root answers
> `UNAUTHENTICATED (16)` both for an account that is *already a member* and for
> the community's own *owner* — not `ALREADY_EXISTS`. A genuinely bad code
> answers `NOT_FOUND (5)`, so the two cases are still distinguishable; just
> match on the exception type rather than assuming. Verified live in
> `tests/test_live_broadcast.py`.

Two options worth knowing:

- `gateway=False` per account skips the websocket for accounts that only make
  API calls, saving a connection and the keepalive traffic.
- `shared_transport` is **on by default**: every account shares one connection
  pool, so the host pays the ~831 ms TLS + HTTP/2 handshake once rather than once
  per account. The rate-limit cooldown table is keyed *per account*, so one
  account's `429` no longer stalls the others — that shared-cooldown coupling is
  the only reason it used to default off. Pass `shared_transport=False` for hard
  socket isolation.

`multihost.py` is a ready-to-run version reading accounts from `accounts.txt`.

---

## Routing through a proxy

Optional, off by default:

```python
client = RootClient(token=TOKEN, proxy="socks5://127.0.0.1:1080")
```

Applies to the API and the gateway. `socks5://` needs
`pip install "httpx[socks]"`; proxying the websocket needs `websockets >= 13`.

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

## Project layout

```
rootpy/          the library — Python, plus data/ (below); nothing else ships
                 in the wheel
rootpy/data/     five generated JSON registries, ~1.1 MB
example.py       guided tour of the whole SDK
examples/        two short focused examples
devscripts/      testing, benchmarking, protocol diagnostics
tests/           1913 offline tests, plus 352 live (see Testing)
docs/            reference and protocol notes
```

`rootpy/data/` is not optional. `enums.json`, `messages.json`,
`message_schemas.json`, `rpc_services.json` and `services.json` are the
decompiled protocol registries every schema lookup reads, which is why
`pyproject.toml` declares them as package data — *"Without this the wheel
installs but every schema lookup raises at runtime."* They are produced by
external tooling; there is no generator script in this repository, so treat
them as inputs rather than build products.

Scripts in `examples/` and `devscripts/` run whether or not you've installed
the package — they add the project root to `sys.path` themselves.

## Included scripts

**`example.py`** is the one to start with — a guided tour of the whole SDK. It
logs in (or creates an account), builds its **own sandbox community**, then
demonstrates channels, roles, members, messages, DMs, friends, presence,
assets and live events inside it, and deletes it again. Your existing servers
are never touched.

```bash
python example.py                       # menu: use a token, or create an account
python example.py --token               # log in from tokens.txt, run everything
python example.py --token --only messages roles
python example.py --list                # what the sections are
python example.py --token --keep        # leave the sandbox community behind
```

Everything else lives in [`devscripts/`](devscripts/) — testing, benchmarking
and the protocol diagnostics:

| | |
|---|---|
| `fullrun.py` | 39-step lifecycle test with timings |
| `twotest.py` | two accounts: one sends, one watches |
| `monitor.py` | live monitor for one account |
| `benchmark.py` · `logintimes.py` | login cost, measured |
| `multihost.py` · `sendone.py` | many accounts in one process; single-request send |
| `diagnose.py` · `signupdebug.py` · `assetdiag.py` | RPC troubleshooting |
| `useraccounts.py` | scrape member profiles to CSV |
| `fanout.py` | many accounts, one prompt: broadcast a command to all of them |

And two short ones in [`examples/`](examples/):

| | |
|---|---|
| `quickstart.py` | the smallest useful bot |
| `auto_react.py` | react to every message matching a rule |

Credentials come from `tokens.txt` (in the project root or beside the script):

```
token=<your token>
token2=<second account, for benchmark>
watcher=<for twotest.py>
sender=<for twotest.py>
```

**Never commit `tokens.txt` or `accounts.json`** — both are in `.gitignore`.

## Discovering the API

The structured layer knows the exact wire shape of every RPC. Two methods put
that a keystroke away, and neither touches the network:

```python
client.explain()                          # index of managers and wire services
client.explain("file")                    # every method of a wire service
client.explain("file.search")             # one method's request fields
client.explain("community_files.search")  # a friendly manager method + its wire call
```

`explain` takes a name the way you'd spell it — a manager (`community_files`), a
wire service (`file`), or a `service.method` in either — and shows the Python
signature and the wire fields together. The manager-to-wire link is read from
the code itself, so it never drifts from what the method actually sends.

`preview` encodes a request and shows what would go on the wire **without
sending it**:

```python
client.preview("message.create", container_id=cid, content="hi")
# preview  message.create   (encoded, NOT sent)
#   request   MessageCreateRequest  ->  response MessageCreateResponse
#   wire body 48 bytes  (53 framed)
#   ...
```

Because it runs the real encoder, an unknown field or a malformed value (a bad
GUID) raises *here*, offline — so a preview that encodes is a call you know is
well-formed, for the price of zero round trips. Both return an object that
prints itself and carries `.as_dict()` for programmatic use.

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
pip install -e ".[dev]"
pytest -q                      # 1913 offline: no token, no network
```

Live tests need a real account. They create a community they own, work inside
it, and delete it afterwards — no ids are hard-coded, and everything created is
named `rootpy test*` so debris from a crashed run is easy to find.

```bash
export ROOT_TOKEN="..."
pytest -m live -v              # 225 tests

export ROOT_TOKEN2="..."       # a second account
pytest -m live2 -v             # 127 more: DMs, calls, friend requests,
                               # moderation, and the broadcast fan-out
pytest -m "live or live2" -v   # 352
```

**216 of 240 service methods (90%) are exercised against the live API** —
`devscripts/covermap.py` walks the call graph out of every live test and says
so. `--strict` drops the edges it resolved by a colliding method name
(`list`, `create`, `get`) rather than by service, and reports 206 (86%). Run
both: the truth is between them, and the gap is how much of the number is
inference.
[LLMS.md §18](LLMS.md) has the per-service breakdown and, more importantly,
an honest list of what is *not* covered.

The second account matters more than it sounds. One account can send a DM and
see it in its own history, but only a second one proves it was *delivered* —
and every bug that hid behind that was silent rather than loud.

### Two extra tools

```bash
pytest -m "live or live2" --timing --timing-json=t.json
```

Per-test milliseconds, split into round trip, rate-limit waiting and client
work using the SDK's own counters. A live run is ~82% network at ~190 ms a call,
which is the answer to "why is this slow" most of the time.

```bash
python devscripts/soak.py --selftest                        # offline, ~1s
python devscripts/soak.py --stress 300 --reconnect-every 25 # ~4 min
```

Two accounts messaging each other under forced reconnects, checking that every
message actually arrives, exactly once. Tracks delivery, latency, cache size,
live object counts and memory. Run `--selftest` first — it proves the
measurement itself is flat before you trust anything it reports.

---

## Protocol notes

Findings that aren't obvious from the API:

- **The gateway is a resync cycle**, not a persistent stream. It sends a batch,
  closes with code `1000`, and expects you to reconnect. That's normal, not an
  error.
- **Membership does not subscribe you; `Attach` does.** Until you call
  `client.community.attach(id)` the hub sends *nothing* for that community — no
  message, no channel edit — while DMs, mentions and status changes keep
  arriving on the same socket. After the attach a channel post shows up as a
  `message` event in ~0.2 s. The desktop client does this when it fully loads a
  community and detaches on unload, which is why it only sees live traffic for
  communities it has opened. `UnreadReader` attaches for you.
- **An attach does not outlive the connection.** The subscription lives with
  the hub connection, not the account, and the hub closes each batch and
  expects a new socket — so the attach goes with it. Measured against a second
  account reading `AttachedUserIds`: gone by +31 s with no gateway at all, and
  by +48 s when the socket was killed under it. Nothing is raised either way;
  the account simply leaves the member list while it still looks logged in.
  Use `async with client.community.held(id):`, or `client.community.hold(id)`
  and `.release(id)` for a bot with no block to leave. Both re-send the attach
  every time the socket comes back. `CommunityExtended.attached_user_ids` and
  `.is_attached(user_id)` are the only way to confirm one took hold.
- **Attaching is visible.** The server broadcasts `COMMUNITY_MEMBER_ATTACH`
  (5502) and clients show attached members as present in the community.
- **Presence is two calls, and the lower one wins.** What others see is
  `min(ceiling, device)` — the ceiling from `SetMaxOnlineStatus`, the device
  from `SetDeviceOnlineStatus`. Both succeed on their own, so setting only the
  ceiling leaves the account invisible to everybody and reports success.
  `client.set_presence` sets both; see [Your account](#your-account).
- **Mentions and DMs are pushed with no attach at all.** A notification embeds
  the whole originating message — content, author, and the author's display
  name.
- **`MessageList` must always include `DateAt`. `Limit` is optional, and when
  present must be 10–50 inclusive** — Root answers
  `Limit: Must be between 10 and 50 [InclusiveBetweenValidator]` otherwise.
  Omitting it returns 50. `messages.list` and `messages.history` range-check it
  locally, so a bad `page_size` fails once rather than on every page.
- **Usernames aren't on messages or members** — only `User` objects carry them.
  The cache learns names from notification payloads.
- **`UserOnlineStatus.ACTIVE` is `0x10`**, not a small integer.
- **Channel `ContainerId` equals its `Id`.**
- **Signup: the Turnstile token is field 3**, not field 7 (that's `AccessToken`).
  Get this wrong and the server keeps issuing challenges, which looks exactly
  like your tokens being rejected.
- **Signup retries must reuse the device id.** The challenge is bound to the
  request; a new device id makes the retry a different signup.
- **Asset links expire** — `AssetGet` returns signed Cloudflare Images URLs
  with an `exp`, so store bytes rather than URLs for anything lasting.

---

## Creating test accounts

Signup is gated by a Cloudflare Turnstile challenge. This library doesn't try
to get around that — you solve the challenge and pass the token in:

```python
from rootpy import AccountFactory

factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
# one-shot
account = await factory.create(turnstile_token=TOKEN)

# or as three explicit steps, with the solving left to you
challenge_url = await factory.create_return_turnstile(username="bob")
token         = my_own_solver(challenge_url)          # your code
account       = await factory.create_with_turnstile(
                    turnstile_token=token, challenge=challenge_url)

factory.save("accounts.json")      # private! it's in .gitignore

# new accounts start unverified, but signup already emailed the code — read it
# from your own mail server and submit it. Don't call send_verification(): the
# resend is behind a *second* Turnstile challenge (action=resend_verification)
# that the token you solved for the signup doesn't satisfy.
code = await read_from_your_mailbox(account.email)   # signup sent it
await factory.verify(account, code)

# or in one call — it polls your mailbox for the code signup already sent
account = await factory.create_and_verify(
              turnstile_token=TOKEN, code_provider=read_from_your_mailbox)
```

Keeping every account on one email pattern means the whole set can be found
and removed in one sweep — which is usually the condition attached to
permission for this. See [docs/accounts.md](docs/accounts.md), and
[docs/two-step-signup.md](docs/two-step-signup.md) for the step-by-step flow
and the rules that break it.

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

MIT — see [LICENSE](LICENSE).
