# LLMS.md — building with rootpy

Context for language models (and humans in a hurry) working with **rootpy**, an
async Python client for the Root chat platform.

Paste this into a model before asking it to build something. It covers the
mental model, what to reach for in which situation, the performance
characteristics, the protocol traps that are impossible to guess — and a
complete listing of every service, class and function with real signatures
(§14).

**Version 1.30.0** — 1913 offline tests, 352 live tests, 216/240 service
methods (90%) exercised against the real API. Sections 17–21 are the technical
handoff: the protocol traps with their evidence, measured coverage and
performance, the open threads, and the mistakes worth not repeating.

---

## 1. What this is

Root is a Discord-like platform: communities → channel groups → channels, plus
DMs, friends, roles and voice. rootpy is a reverse-engineered client for it,
built from decompiled protobuf schemas and verified against live traffic.

It speaks **gRPC-web over HTTPS** to `api.rootapp.com`, plus an optional
**websocket** to a hub for pushed events. Everything is `async`.

```python
from rootpy import RootClient

client = RootClient(token=TOKEN)
await client.login_token()
await client.messages.send(channel_id, "hello", community_id=community_id)
await client.close()
```

---

## 2. The mental model

**One client per account.** `RootClient` is the entry point and holds
everything: services (`client.messages`), managers (`client.roles`), caches,
and the gateway.

**Two layers, both fine to use.**

- *Verbs on the client*: `client.message(...)`, `client.get_members(...)` —
  convenient, id-based.
- *Objects*: `channel.send(...)`, `message.reply(...)`, `member.add_role(...)`
  — objects returned by fetches carry a client reference and can act.

Prefer objects when you already have one; they're less error-prone (no chance
of passing the wrong id) and read better.

**HTTP and websocket are separate.** Every RPC works over plain HTTPS. The
websocket exists only to *receive* pushed events. If you're only doing things,
never call `connect()`.

---

## 2b. From a token to a channel you can post in

Every example below starts with a `TOKEN` and a channel id. This is how you get
from one to the other — it is the first thing any script needs and the one
chain worth memorising.

**The token.** Read it from the environment; never hardcode it in a file you
might share. `ROOT_TOKEN` is the convention this repo uses everywhere:

```python
import os
TOKEN = os.environ["ROOT_TOKEN"]
```

**The chain.** `list_communities()` → a community's `id` →
`community_detail(id)` → `text_channels` → a channel's `id`:

```python
import asyncio, os
from rootpy import RootClient

async def main():
    client = RootClient(token=os.environ["ROOT_TOKEN"])
    await client.login_token()
    try:
        communities = await client.list_communities()   # tuple[Community]
        for c in communities:
            print(c.id, c.name)

        detail = await client.community_detail(communities[0].id)
        for channel in detail.text_channels:            # tuple[Channel]
            print(channel.id, channel.name)

        channel = detail.text_channels[0]
        await channel.send("hello")                     # the object can act
    finally:
        await client.close()

asyncio.run(main())
```

Shapes you can rely on, all verified against a live account:

| call | returns |
|---|---|
| `await client.list_communities()` | `tuple[Community]` — `.id`, `.name` |
| `await client.community_detail(id)` | `CommunityExtended` — `.community`, `.channels`, `.text_channels`, `.channel_groups`, `.roles`, `.members` |
| `detail.text_channels` | `tuple[Channel]` — `.id`, `.name`, `.community_id`, `.is_text`, and `.send()` / `.history_iter()` |

**Use `text_channels`, not `channels`.** `channels` includes voice and app
channels, and the message RPCs fail on those. On a real community they differ:
23 channels, 20 of them text.

**`client.close()` matters.** It releases the HTTP connection pool (and the
websocket if you connected). `async with RootClient(token=...) as client:` does
it for you.

---

## 3. Choosing a login mode

Measured against a live account. A full live run (332 tests at the time of the measurement; 347 now) puts the average round trip at **~190 ms** over ~850 calls (188-199 across three runs); the per-mode figures below were taken on a slower connection at ~330 ms, so read them as ratios rather than absolutes:

| mode | code | requests | time | receives events? |
|---|---|---|---|---|
| token-only | just construct, then call | 1 | ~330 ms | no |
| minimal | `login_token()` + `preload_caches=False` | 2 | ~1000 ms | no |
| **lazy** (default) | `login_token()` | 3 | ~950 ms | no |
| eager | `expand_communities=True` | 3 + one per community | ~2600 ms | no |
| gateway | `login_token()` + `connect()` | 3 + socket | ~1700 ms | **yes** |

**Rules of thumb**

- Firing one message and exiting → `RootClient.send_once(token, channel, text,
  community_id=...)`. One request, no login.
- A script that does things → `login_token()`. Don't connect.
- A bot that reacts to things → `login_token()` then `connect()`.
- `expand_communities=True` only if you'll immediately touch every community.
  It's the single biggest login cost.

**`start_token()` never returns** — it's login + connect + wait-forever. Use
the two calls separately if you want control flow back.

---

## 4. Receiving messages (the important subtlety)

Root **pushes DMs** over the websocket as `message` events unconditionally.
Channel messages push too — **but only for communities you have attached**.
This is what surprises everyone: membership is not a subscription, so a
community you belong to but have not attached delivers nothing and looks
broken. `client.community.attach(community_id)` is
`root.CommunityGrpcService/Attach`, and without it the hub sends no packet for
that community at all.

`UnreadReader` is the batteries-included version: it attaches to every
community, reads and clears whatever arrives, and re-attaches after a
reconnect.

```python
from rootpy import UnreadReader

await client.connect()

async def handle(message):
    print(message.content)

async with UnreadReader(client, on_message=handle):
    await asyncio.Event().wait()
```

- Mentions and DMs: **instant**, no attach needed.
- Channel messages: **+0.2 s** once attached; never, without.
- Idle cost: **zero requests**. `interval=0` (the default) means no sweep at
  all; pass a number for a safety-net poll.
- Attaching is visible — other members see you as present in the community.
  `detach`/`detach_many` undo it, and the reader detaches on exit by default.
- Duplicates are filtered by message id in `client.dispatch`, so a mention
  arriving both ways fires handlers once.

The older polling path (`watch_unread()`, `watch_channel()`) still works and is
the right choice when staying out of the presence list matters more than
latency: it sweeps `last_activity_at` against `user_last_viewed_at`, one
`GetExtended` per community per tick, concurrently.

Mentions are a separate signal from channel push and always were: a mention
produces a `notification` whether or not you are attached, but **no mention
spelling produces an entry in `notifications.list()`**.

Watching a single channel instead: `client.watch_channel(channel_id)`.

---

## 5. Events

```python
@client.event                       # one handler per event, named on_<event>
async def on_message(event): ...

client.add_listener("member_join", handler)      # any number of handlers
event = await client.wait_for("message", check=..., timeout=30)
```

The events built by `rootpy/typed_events.py` subclass `RootEvent` and so all
carry `.received_at` and `.raw`. **`on_message` is the exception** —
`MessageEvent` lives in `rootpy/events.py`, predates that base and does not
subclass it; its fields are `sequence`, `message`, `action`, `raw` and there is
no `.received_at`. The other `rootpy/events.py` events (`ReadyEvent`,
`ChannelDeletedEvent`, `CommunityLeaveEvent`, …) are standalone in the same way.
When you need a time for a message, read it off `event.message`.

| event | object | fields |
|---|---|---|
| `on_message` | `MessageEvent` | `.message`, `.action` — **no `.received_at`** |
| `on_friend_request`, `on_friend_remove` | `FriendEvent` | `user_id`, `username` |
| `on_member_join/_leave/_ban/_unban` | `MemberEvent` | `user_id`, `community_id`, `role_ids`, `presence=False` |
| `on_member_online/_offline` | `MemberEvent` | the same fields, `presence=True` |
| `on_role_add/_remove` | `MemberRoleEvent` | `role_id`, `role_name`, `user_ids` |
| `on_channel_create/_edit/_delete` | `ChannelEvent` | `name`, `category`, `is_text` |
| `on_role_create/_edit/_delete/_move` | `RoleEvent` | `name`, `color_hex`, `mentionable`, `before_role_id` |
| `on_reaction_add/_remove` | `ReactionEvent` | `shortcode`, `message_id`, `user_id`, `container_id`, `is_direct` |
| `on_block_add/_remove` | `BlockEvent` | `user_id` |

**`member_join`/`member_leave` are membership; `member_online`/`member_offline`
are presence.** `COMMUNITY_MEMBER_ATTACH`/`_DETACH` used to fire
`on_member_join`/`on_member_leave`, which turned every join counter into a count
of people *opening* the community in their client. Attach says nothing about
membership: the desktop app feeds it straight into
`SetAttached(userId, true, packet.OnlineStatus)` (`MemberService.cs:303-315`),
and `CommunityMemberAttachPacket` carries a `UserOnlineStatus` for precisely
that reason. So attach and detach now dispatch `member_online` and
`member_offline`, and `member_join`/`member_leave` come only from the genuine
membership packets `COMMUNITY_JOINED` and `COMMUNITY_LEAVE`.
`MemberEvent.presence` separates the two families on the object, so one handler
can take both and still tell them apart. **This is a breaking change** if you
were counting joins from attaches. See §9, "Membership is not a subscription",
for what attach is actually for.

**`ChannelEvent.is_text` is `Optional[bool]`, not `bool`.** `ChannelType` rides
on `ChannelCreatedPacket` only (field 12); the edited packet writes fields 1 and
3-11 (7 and 8 are the optional `Description` and `IconAssetUri` wrappers, which
`packet_schemas.py` does not yet decode) and the deleted packet only 1/3/4/5 —
never 12, so a flat `channel_type = 0` made `if event.is_text:` drop 100% of
edits and deletes. The builder now fills the type in from the client's channel
cache when the packet omits it — and `name` the same way, which is the only
place a delete's name can come from. The cache is still warm on a delete
because the gateway dispatches `packet` before `channel_deleted` evicts it.
What is left is honest uncertainty: `is_text` is `None`, and `name` is `None`,
when the channel was never cached. `None` is not `False`, so branch on
`event.is_text is True` / `is None` rather than on truthiness.

**Role create and edit share one packet slot.** `CommunityRolePacket` (oneof
slot 90, `COMMUNITY_ROLE`) carries both, and the app discriminates on the
packet's own `PacketType` before deciding what to do with it
(`RoleService.cs:74-92`). Routing on the slot alone meant a rename, a recolour
or an `is_mentionable` toggle all arrived as `on_role_create` with a `RoleEvent`
indistinguishable from a real creation. The builder now subroutes on
`fields["packet_type"]`: 5401 → `role_create`, 5402 → `role_edit`, 5403 →
`role_delete`, 5404 → `role_move`. Deletes and moves also have their own slots
(`COMMUNITY_ROLE_DELETED` 91, `COMMUNITY_ROLE_MOVED` 92) and reach
`on_role_delete` and `on_role_move` through those. `RoleEvent.before_role_id`
carries the role the moved one now sits after; an empty string means it moved
to the top, which is what the app sends.

`MESSAGE_REACTION` (slot 173) subroutes the same way, and it has to: one
`MessageReactionPacket` serves add and remove, for channels *and* for direct
messages. 5801 → `reaction_add`, 5802 → `reaction_remove`, and the DM pair
204/205 map to the same two names (`ChannelService.cs:657-666`,
`DirectMessageService.cs:286-294`). `ReactionEvent.is_direct` tells the two
containers apart, because a DM reaction carries no `community_id` and would
otherwise look like a channel reaction in a community you cannot find.

Every raw packet is also dispatched as `on_packet_<name>`. `PacketType` has 91
members, but the gateway guards with `is not PacketType.UNKNOWN`, so **90** of
them get an `on_packet_*` handler; anything it cannot name goes to
`on_unknown_packet` (there is no `on_packet_unknown`). `on_packet` and
`on_socket_response` see every frame either way.

`pkt.fields` gives decoded values, and two things live there that did not used
to:

- **Field 1 is `packet_type`, on all 90 schemas.** It is the app's own
  `PacketType` int, which is finer-grained than the oneof slot the gateway names
  the event after — a reaction add and a reaction remove are 5801 and 5802
  inside one slot. It is what makes the role subroute above possible, and it is
  there for your handlers too.
- **Enum payload fields decode instead of being skipped.** Thirteen of them:
  `USER_SET_STATUS.online_status` (4), `USER_SET_MAX_STATUS.max_status` (3),
  `NOTIFICATION.notification_type` (7), `MESSAGE.message_type` (11),
  `COMMUNITY_MEMBER_ATTACH.online_status` (5), `COMMUNITY_LEAVE.leave_reason`
  (5), `BILLING_SUBSCRIPTION_STATUS_CHANGED.status` (11), the three
  `USER_SET_*_INVITE_REQUIREMENT.connection` (4), `COMMUNITY_APP_ADDED`'s
  `app_type` (7) and `app_deployment_status` (11), and
  `COMMUNITY_APP_SET_STATUS.app_deployment_status` (6). They arrive as plain
  ints — run them through `SomeEnum.coerce(value)`, for the reason in §17.

**`on_sync_lost` fires when the server rejects our resume cursor.**
`ClientNotification` field 1 is a `PacketErrorCode` (`UNSPECIFIED = 0`,
`SYNC_LOST = 1`; `from rootpy import PacketErrorCode`), and the server writes it only
when it is non-zero — `InternalWriteTo` skips the unspecified value — so the
field is absent on the overwhelming majority of frames. On `SYNC_LOST` the
gateway logs a warning and dispatches `sync_lost` with the sequence number:

```python
@client.event
async def on_sync_lost(sequence): ...   # refetch; the cursor is stale
```

Everything downstream still runs, because a `SYNC_LOST` frame can carry a
packet. This is an extra signal beside the 4016 close-code path, not a
replacement for it.

**`on_notification` is not the NOTIFICATION packet.** It fires for *every*
frame the gateway reads, **keepalive pings included**, so
`wait_for("notification")` returns within a few seconds no matter what
happened and cannot be used to prove anything arrived. For the real thing use
`on_packet_notification` (or `on_socket_response` and check
`packet.type`). This trap produced a wrong finding in this project's own docs
for several releases — see §17.

**Usernames are often `None`.** Root's member and message records carry ids
only. The state cache learns names from notification payloads and profile
fetches; a miss returns `None` rather than blocking on a request. Fall back to
the id, or fetch profiles explicitly.

---

**Handlers do not necessarily run before `dispatch()` returns.** `message`
events and every `add_listener()` handler are spawned as background tasks on
purpose — a slow handler must not stall the gateway read loop, and one that
raises must not take the socket down. Only an `@client.event` handler for a
*non-message* event is awaited inline.

So awaiting `dispatch()` means the event was delivered, not that anything
finished reacting to it. When you need that — a test, a graceful shutdown,
"process this batch then exit" — drain:

```python
await client.dispatch("message", event)
await client.drain_events(timeout=10)   # now the handlers have finished
```

`drain_events()` returns how many tasks it awaited, and drains handlers spawned
by handlers so a chain settles rather than only its first link. `wait_for()` is
unaffected: waiters resolve inline, before any background work is scheduled.

## 6. Efficiency — what's expensive and what isn't

This matters more than usual here, because several calls look cheap and aren't.

| operation | cost |
|---|---|
| `client.get_members(id)` | free after the first — cached |
| `client.community_detail(id)` | one `GetExtended`, then cached |
| `client.get_profiles([ids])` | **one request, however many ids** — batch! |
| `client.get_members_detailed(id)` | members + profiles, 500 per request, 8 at a time |
| `client.asset_urls([uris])` | chunked **5 per request, 16 concurrent** |
| `messages.list(...)` | one page |
| `channel.history_iter(limit=N)` | one request per page, stops on a short page |
| `messages.send(...)` | ~170 ms warm |

**Patterns that matter**

Filter *before* resolving. Whether a member has a banner is on the profile, so
check that before asking the asset service for URLs — it typically removes 90%
of asset requests.

```python
keepers = [p for p in profiles.values() if p.banner_uri]      # free
urls = await client.asset_urls([p.banner_uri for p in keepers])  # only these
```

Batch profile lookups. `get_profiles([id1, id2, ...])` is one request;
looping `get_profile(id)` is N. **Do not chunk it yourself** — the table above
used to say "one request per 100 ids", which is wrong and led to hand-rolled
`for chunk in chunks(ids, 100)` loops that turn one request into N. Measured
against the live API: 1, 10, 50, 100, 200, 400, 800, 1000, 1500, 2000 and 4000
ids each came back complete in a **single** request, and the per-id cost falls
the whole way — 1.96 ms/id at 100, 0.50 at 1000, 0.31 at 4000 — because a
round trip costs ~190 ms almost regardless of what it carries.

Don't re-draw random members in a loop — shuffle the member list once and walk
it, or you'll re-fetch people you've already seen.

**Measure rather than guess:**

```python
print(client.timing_report())
```

Splits into round trip (network + server), waiting (rate-limit/retry holds we
imposed), and our own overhead.

---

## 6b. Routing through a proxy

Every client can send its traffic through a SOCKS or HTTP proxy. It's optional
and off by default -- nothing changes if you don't set it.

```python
client = RootClient(token=TOKEN, proxy="socks5://127.0.0.1:1080")
```

The proxy applies to both the API transport and the gateway websocket.
`socks5://` needs `pip install "httpx[socks]"`, and proxying the websocket
needs `websockets >= 13` (older versions log a warning and connect directly).

Supplying your own transport means you configured it, so the client leaves it
alone:

```python
transport = GrpcWebTransport(proxy="socks5://127.0.0.1:1081")
client = RootClient(token=TOKEN, transport=transport)
```

**The verification classmethods honour it too.** `send_email_verification` and
`verify_email` are classmethods with no client behind them, so they used to
build a bare transport and go direct — sending the one request that proves you
own an address *around* the proxy the rest of the signup used. Both now take
`proxy=` and `transport=`, and `AccountFactory` forwards whatever it was
configured with into both (`AccountFactory._routing()`), passing the keys only
when they have values so a factory with no proxy still calls them the same way.
A caller-supplied transport is left open; one they build themselves is closed in
a `finally`.

## 7. Running several accounts

One Python process costs ~30 MB before doing anything. Per-account processes
pay that every time:

| accounts | separate processes | one process |
|---|---|---|
| 5 | ~149 MB | ~30 MB |
| 50 | ~1,490 MB | ~30 MB |

So run them together:

```python
from rootpy import MultiClientHost

host = MultiClientHost(stagger=0.5)
host.add("alice", ALICE_TOKEN, setup=setup_alice)
host.add("bob", BOB_TOKEN, setup=setup_bob)
await host.run()
```

Each account is independent — its own client, handlers, rate-limit budget and
failures. A bad token fails that account alone.

To do the same thing as every account, `broadcast` an action. It returns one
`Outcome` per account and never raises — partial success is the normal fan-out
result, so a refusal is a value you read, not an exception:

```python
host = MultiClientHost.from_tokens([TOKEN_A, TOKEN_B])
async with host:
    results = await host.broadcast(lambda c: c.invites.join(code))
    for outcome in results.values():
        print(outcome)      # "account1: ok" / "account2: GrpcUnauthenticated: ..."
```

**Do not assume `ALREADY_EXISTS` for a repeat join.** Measured live: Root
answers `UNAUTHENTICATED (16)` for an account that is already a member *and*
for the community's owner; a bad code answers `NOT_FOUND (5)`. Match on the
exception type. The fan-out really is concurrent — a two-account broadcast
measured 190 ms against a single call's 172 ms (1.10x), so HTTP/2 multiplexing
is doing what it claims.

`shared_transport` is **on by default**: one TLS + HTTP/2 handshake for the
whole host instead of one per account (~831 ms each). It used to default off
because the pool also shared the rate-limit cooldown table, so one account's 429
paused everyone — cooldowns are keyed per account now
(`GrpcWebTransport._account_scope`), which removed that coupling. Pass
`shared_transport=False` for hard socket isolation.

---

## 7b. Seeing what a call sends (`explain` / `preview`)

Both are offline — no token, no network — and both are for the moment you are
about to write a call and want to know its exact shape.

```python
client.explain()                          # index: managers and wire services
client.explain("file")                    # a wire service's methods
client.explain("file.search")             # one method's request fields
client.explain("community_files.search")  # a manager method + the wire call it makes

client.preview("message.create", container_id=cid, content="hi")   # encode, don't send
```

`explain` shows the Python signature and the wire fields together, resolving the
manager-to-wire link from the code itself. `preview` runs the real encoder, so
an unknown field or a bad GUID raises offline — a preview that encodes is a
well-formed call. Prefer these over guessing at a request shape or spending a
live run to find out a keyword was wrong.

---

## 8. Assets (images)

Profile pictures and banners are `root://asset/...` tokens, not URLs. Resolve
them:

```python
asset = await client.get_asset(profile.avatar_url)
asset.best_url             # largest variant
asset.url_for_size(128)    # smallest at least 128px across
asset.links                # every variant Root offers
asset.expires_at           # ← these are SIGNED, SHORT-LIVED URLs
```

Variants are typically 32 (placeholder), 128 (thumb), 512 (small), 2048
(public). Resolution is batched and concurrent.

**Store bytes, not URLs**, for anything lasting:

```python
await client.save_asset(profile.avatar_url, "avatars/alice")   # -> .png
```

---

## 9. Protocol traps

> The short list. Each of these is expanded — with how it was found and
> what was ruled out — in [section 17](#17-protocol-traps-in-depth).

These are the things a model cannot infer and will get wrong. They cost real
debugging time to find.

**A channel group's name takes at most _two_ words.** `ChannelGroupCreate`'s
`Name` accepts ASCII letters, digits and apostrophes in at most two
whitespace-separated words, with no leading or trailing whitespace. Length is
not the constraint (65 characters is fine at one word). Anything else answers
`Name: 'Name' is not in the correct format. [RegularExpressionValidator]`.

```python
await client.community.create_channel_group(cid, "test area")   # OK
await client.community.create_channel_group(cid, "general")     # OK
await client.community.create_channel_group(cid, "my text area")  # 3 words - refused
await client.community.create_channel_group(cid, "test-area")     # hyphen  - refused
```

Word count is the part nobody guesses — three separate attempts blamed the
character set (hyphens, then digits) when `rootpy group bbcy` was refused
purely for being three words. Channel *names* are not subject to this;
`rootpy-text-3c426a7d` is a valid channel name.

**Request bodies must be gRPC-web framed.** The transport does this
automatically — pass raw protobuf to `transport.unary(...)` and don't frame it
yourself. An unframed body reaches the server as garbage and the handler throws
a bare `UNKNOWN (2) Exception was thrown by handler`, which looks exactly like a
permissions failure.

**Responses must be unwrapped** with `unwrap_grpc_web(response.content)` before
parsing. Parsing `.content` directly hits the 5-byte frame header and reports
nonsense wire types.

**`MessageList` must always include `DateAt`. `Limit` is optional, and when
present must be between 10 and 50 inclusive.** This entry used to say "must not
include `Limit`; sending it gets the request rejected", which is wrong in both
directions — and it contradicted `messages.history()`, which sends
`Limit=page_size` on every page and works. Root settles it in its own words:

```
Limit: Must be between 10 and 50 [InclusiveBetweenValidator]
```

Laddered against the live API: omitted → OK (50 returned); 1, 2, 5, 9 →
`INVALID_ARGUMENT`; 10–50 → OK, returning exactly that many; 51, 55, 60, 75,
99, 100 → `INVALID_ARGUMENT`. `messages.list` and `messages.history` now
range-check it locally, so a bad value raises `ValueError` before a round trip
instead of failing on every page.

**Signup: the Turnstile token goes in field 3.** Field 7 is `AccessToken` — a
different thing. Put the captcha token there and the server sees a signup with
no token, so it issues another challenge, indistinguishable from rejection.

**Signup already emails a verification code** — read it and submit it. Calling
`send_verification()` asks for *another* one and triggers a separate Turnstile
challenge (`action=resend_verification`), which solving the signup challenge
does not satisfy. On a fresh account, skip the resend entirely.

**Signup retries must reuse the device id** (and username, password, email).
Root binds the challenge to the request that produced it. A fresh device id on
the retry reads as a new signup; symptom is the `cdata` in the challenge URL
changing every attempt while no token is ever accepted.

**The gateway is a resync cycle**, not a persistent stream. It sends a batch,
closes with code `1000`, and expects a reconnect. That's normal.

**`UserOnlineStatus.ACTIVE` is `0x10`**, and Root has **no do-not-disturb**
state — presence is active / inactive (idle) / disconnected (invisible).

**A message can reply to at most 5 messages.** `parent_message_ids` is a
repeated field; past five the server rejects the whole send without saying why,
so `normalise_reply_targets` raises first.

**Channel `ContainerId` equals its `Id`.** Message RPCs only work on
`channel_type` 1 (TEXT) or 2 (THREADED_TEXT); voice (4) and app (8) channels
fail server-side.

**Root's real error is in a header, not the message.** Every rejected request
answers `INVALID_ARGUMENT (3)` with the same sentence — "One or more request
values were rejected by Root" — while the `root-exception-bin` trailer carries
a `RequestValidatorList` of `{property_name, error_message, error_code}`. Since
1.2.0 that is folded into `str(exc)` and also available as
`exc.validation_errors`, so an INVALID_ARGUMENT tells you the field and the
validator. Everything below was found that way.

**`picture_hex` is required on community create, and is exactly 7 characters**
— `#rrggbb`, hash included (`ExactLengthValidator`). Role `color_hex` uses the
same form. `normalize_hex_colour()` accepts either form and always emits the
7-character one, so you can pass `"3f51b5"`.

**`CommunityEdit` is a replace, not a patch.** `Name` *and* `PictureHex` are
required even when you only want to change the description. `edit_community()`
carries the current values forward with one extra read; supply them explicitly
to skip it.

**Channel types are 1 / 2 / 4 / 8** — `Unspecified: 0, Text: 1, ThreadedText:
2, Voice: 4, App: 8`. Sending 0 is rejected by a `PredicateValidator`; sending
a wrong *non-zero* value succeeds and silently creates the wrong kind of
channel, which is the failure mode worth guarding against.

**Name rules differ by object, and two of them are exact opposites.** All are
enforced server-side with a `RegularExpressionValidator`, so a bad name costs a
round trip and answers `Name: 'Name' is not in the correct format.` Laddered
against a live server:

| object | accepts | rejects |
|---|---|---|
| **community** | spaces, several words | — |
| **channel group** | letters, digits, apostrophes, **at most two words** | hyphen, underscore, period, 3+ words, leading/trailing space |
| **role**, **channel** | letters, digits, **hyphens** | **space**, underscore, period |

Read that table twice: a channel *group* may contain a space but not a hyphen,
while a channel may contain a hyphen but not a space. `test area` and
`new-channel` are both valid; `test-area` and `new channel` are both refused.
Length is not a constraint for any of them (40+ characters accepted).

Usernames and nicknames share one rule, which Root states
in its sign-up form: 3–20 characters of letters, numbers, underscores and
periods, with underscores and periods neither at the start or end nor next to
each other. `rootpy.validation` enforces this before the request goes out.

**`MessageList` returns deleted messages as tombstones** with `deleted_at`
set, rather than omitting them. `Message.is_deleted` flags them and `list()`
filters them by default — pass `include_deleted=True` to see them.

**Channel messages are pushed only after you attach.** Membership is not a
subscription. Until

```python
await client.community.attach(community_id)
```

the hub sends no packet for that community at all, while DMs, mentions and
status changes arrive normally on the same socket — which is exactly what a
broken listener looks like. After the attach, a plain channel post arrives as a
`message` event carrying the channel as `container_id` and the community as
`community_id`, measured at **+0.2 s**. `wait_for("message")` on a channel post
waits forever *without* the attach and returns promptly with it.

This is what the desktop client does: `Community.attachAsync` runs off its
full-load path and `FullyUnload` detaches, so it receives live traffic only for
communities it has actually opened. Attaching is visible to other members — the
server broadcasts `COMMUNITY_MEMBER_ATTACH` (5502) and clients show attached
members as present — so `client.community.detach(id)` and `detach_many(ids)`
exist to undo it. On the receiving side that broadcast is
`on_member_online`/`on_member_offline`, *not* `on_member_join`/`on_member_leave`
— it is presence, and §5 has the routing.

`UnreadReader` does all of this for you, including re-attaching after a
reconnect (the subscription lives with the hub connection).

Mentions are the exception that needs no attach: they arrive as `NOTIFICATION`
whether or not you are attached. They still produce no notification-list entry
— see §17 for the measurements.

**Mentions are markdown links**, not `<@id>`. Build them with `User.mention`
and `Channel.mention`, which round-trip through
`rootpy.commands.parse_user_mention` / `parse_channel_mention`:

```python
await channel.send(f"{user.mention} take a look at {other.mention}")
# [@someone](root://user/0030…)  and  [#general](root://channel/0030…)
```

---

## 10. Adding a new RPC

The pattern, using the decompiled schemas:

```python
MY_ENDPOINT = "https://api.rootapp.com/root.SomeGrpcService/Method"

async def my_call(self, some_id: str):
    body = bytearray()
    body += length_field(10, encode_root_guid(normalize_root_guid(some_id)))
    body += field_key(11, 0) + encode_varint(42)

    response = await self.transport.unary(
        endpoint=MY_ENDPOINT,
        body=bytes(body),                    # raw — the transport frames it
        headers=self._headers(),
        operation="SomeMethod",
    )
    payload = unwrap_grpc_web(response.content)   # unwrap BEFORE parsing
    ...
```

Field numbers come from the generated C#: the tag in `WriteRawTag(N)` or
`case Nu:` is `(field_number << 3) | wire_type`, so tag 82 → field 10, wire
type 2.

**Check which service you're targeting.** `connect.ConnectService` and
`root.UserGrpcService` have different numbering for similar-looking messages;
applying one schema to the other produces `INVALID_ARGUMENT` on every field.

When something fails, `devscripts/diagnose.py` and `devscripts/signupdebug.py`
dump the exact request and decode Root's structured error, which usually names
the offending field.

---

## 11. Errors

Every exception class rootpy *defines* descends from `RootError` — all 54 of
them — so `except RootError` covers every typed Root failure. That became true
in 1.29.0: `CallActionError`, `CommandError`, `MediaDependencyMissing` and
`AlreadyCreatedError` were outside the tree and escaped it. They now inherit
`RootError` *in addition to* their old bases (`CallActionError` and
`MediaDependencyMissing` are still `RuntimeError`), so nothing that used to
catch them stopped working, and `CallActionError` is exported from `rootpy` —
it was not before.

**It is not a total catch-all, though.** rootpy still raises plain builtins on
ordinary paths, and `except RootError` catches none of them: an AST walk of
`rootpy/**/*.py` counts 75 `raise ValueError` and 27 `raise TypeError` for
local argument and format errors (see `rootpy.validation`, below), and 51
`raise RuntimeError` for state errors — `connect()` without a session,
`wait_until_ready()` before connect, `client.mute()` with no active call —
plus one `ConnectionError` (`gateway.py`), one `KeyError` (`cache.py`) and two
`FileNotFoundError`. `transport.py` also re-raises `httpx` timeout and network
exceptions bare once retries are exhausted. Catch `Exception` if you need a
true backstop.

Below `RootError` the hierarchy splits by layer:

* `GrpcWebError` and its per-status subclasses (`GrpcInvalidArgument`,
  `GrpcNotFound`, `GrpcPermissionDenied`, …) — the request reached gRPC and was
  refused.
* `HttpStatusError` and its subclasses (`Unauthorized`, `RateLimited`,
  `ServerError`, …) — rejected before gRPC. **An HTTP 401 and a gRPC
  `UNAUTHENTICATED` are different problems**: the first usually means no
  authorization header was sent at all, the second that one was sent and
  refused. Collapsing them hides which layer is at fault.

For a rejected request, read the detail rather than the sentence:

```python
try:
    await client.community.create("my community")
except GrpcInvalidArgument as exc:
    for err in exc.validation_errors:      # [] when Root sent no payload
        print(err.property_name, err.error_message, err.error_code)
    print(exc.payload_kind)                # e.g. "request_validator_list"
```

`format_root_error(exc, verbose=True)` prints the whole picture when you want
it in a log. `exc.description` and `exc.hint` give a human-readable meaning and
a suggested next step for any gRPC status.

Format rules are checked client-side where they are known, so a bad username,
nickname or colour raises `ValueError` immediately instead of costing a round
trip — see `rootpy.validation`.

## 12. Building something — a worked shape

A logger that archives everything it sees:

```python
import asyncio
from rootpy import RootClient

async def main():
    client = RootClient(token=TOKEN)
    await client.login_token()
    await client.connect()                     # needed to receive

    @client.event
    async def on_message(event):
        m = event.message
        await store(m.id, m.user_id, m.container_id, m.content)

    client.watch_unread(interval=3.0)          # covers non-mention messages

    # backfill history for channels you care about
    detail = await client.community_detail(COMMUNITY_ID)
    for channel in detail.text_channels:
        async for message in channel.history_iter(limit=500):
            await store(message.id, message.user_id, channel.id, message.content)

    await asyncio.Event().wait()               # run forever

asyncio.run(main())
```

Points worth copying: connect **and** watch (push covers only mentions/DMs),
use `text_channels` (message RPCs fail on voice channels), and
`history_iter` rather than repeated `list` calls.

---

## 13. Where to look

| | |
|---|---|
| `example.py` | guided tour — creates its own sandbox community, then deletes it |
| `examples/` | two short focused examples (quickstart, auto-react) |
| `docs/api.md` | full API reference |
| `docs/errors.md` | error decoding in detail |
| `docs/accounts.md`, `docs/two-step-signup.md` | account creation, in detail |
| `devscripts/` | testing, benchmarking, protocol diagnostics |
| `tests/` | 1880 offline + 347 live, each locking in a bug that was expensive to find |

---

---

## 14. Complete surface

**Generated** by `python devscripts/gendocs.py` from the live package —
do not edit by hand. Signatures, defaults, field lists and async-ness
are read from the code, so this section cannot drift from it. The prose
elsewhere in this file is written by hand and is where the reasoning
lives; this is the reference.

Two ways to reach most things: a **verb on the client** (shortest) or
the **namespace** it lives in (fuller options). `client.message(...)`
and `client.messages.send(...)` do the same job.

### Client verbs

Generated from the live package. `async` entries are awaited, `gen`
entries are async generators (`async for`), and the rest are
synchronous cache reads that raise TypeError if awaited.

```python
async client.accept_friend_request(notification)
async client.add_friend(username: str)
      client.add_listener(event_name: str, coroutine) -> None
      client.add_message_listener(listener)
async client.asset_url(asset_uri, *, size: Optional[int] = None) -> Optional[str]
async client.asset_urls(asset_uris, *, size: Optional[int] = None) -> dict
async client.block(user: UserTarget)
async client.call(user: UserTarget) -> CallSession
async client.change_banner(source) -> Optional[str]
async client.change_profile_picture(source) -> Optional[str]
async client.clone_community(source_community_id: str, **kwargs)
async client.close() -> None
      client.command(name: Optional[str] = None, *, aliases: Union[tuple[str, ...], list[str]] = (), owner_only: bool = True, description: str = '')
async client.community_detail(community_id: str, *, refresh: bool = False)
async client.connect(*, announce_device: bool = True) -> None
async client.create_account(*, username: str, password: str, email: str, access_token: Optional[str] = None, turnstile_token: Optional[str] = None, device_id: Optional[str] = None, command_prefix: str = '>', require_media: bool = False, transport = None, proxy: Optional[str] = None) -> RootClient
async client.create_community(name: str, **kwargs)
async client.deafen(deafened: bool = True) -> None
async client.decline_friend_request(notification)
async client.delete_message(message: Message) -> None
      client.describe_services(name: Optional[str] = None)
async client.direct_message(user: UserTarget, content: str, *, delete_after: Optional[float] = None) -> MessageSendResult
      client.disable_auto_react() -> bool
async client.dispatch(name: str, event: object) -> None
async client.download_asset(uri) -> Optional[bytes]
async client.drain_events(*, timeout: Optional[float] = None) -> int
async client.edit_message(message: Message, content: str, *, uris: Optional[List[str]] = None) -> Message
      client.enable_auto_react(emojis, *, only_self: bool = True, containers = None, stop_on_error: bool = False)
async client.ensure_community_cached(community_id: str) -> CommunityExtended
      client.event(coroutine: EventHandler) -> EventHandler
      client.explain(target: Optional[str] = None)
async client.fetch_community(community_id: str) -> CommunityExtended
async client.get_asset(uri)
      client.get_channel(channel_id: str) -> Optional[Channel]
      client.get_channel_group(channel_group_id: str) -> Optional[ChannelGroup]
      client.get_command(name: str) -> Optional[Command]
async client.get_communities(*, refresh: bool = True)
      client.get_community(community_id: str) -> Optional[Community]
async client.get_friends()
async client.get_member(community_id: str, user_id: str, *, refresh: bool = False)
async client.get_members(community_id: str, *, refresh: bool = False)
async client.get_members_detailed(community_id: str, *, refresh: bool = False, batch_size: int = 500, concurrency: int = 8)
      client.get_message(message_id: str) -> Optional[Message]
async client.get_profile(user_id: str)
async client.get_profiles(user_ids) -> dict
async client.get_random_member(community_id: str, *, refresh: bool = False, exclude_self: bool = True, with_profile: bool = True)
async client.get_random_members(community_id: str, count: int = 1, *, refresh: bool = False, exclude_self: bool = True, with_profile: bool = True)
async client.get_servers(*, refresh: bool = True)
      client.get_user(user_id: str, *, username: Optional[str] = None) -> User
async client.go_idle()
async client.go_invisible()
async client.go_online()
      client.history(container_id: str, *, community_id: Optional[str] = None, limit: Optional[int] = 200, before: Optional[float] = None, page_size: int = 50)
async client.join_voice(channel: Union[Channel, str]) -> CallSession
async client.leave_community(community_id: str) -> None
async client.leave_voice() -> None
async client.list_blocked()
async client.list_communities(*, refresh: bool = True)
async client.list_friends()
async client.list_messages(container_id: str, *, community_id: Optional[str] = None, direction: str = 'both', after: Optional[float] = None, limit: Optional[int] = None, include_deleted: bool = False)
async client.list_notifications(**kwargs)
async client.login(username: str, password: str) -> None
async client.login_token(token: Optional[str] = None, *, device_id: Optional[str] = None, web_api_url: str = 'https://api.rootapp.com/') -> None
async client.mark_all_read()
async client.mark_channel_read(container_id: str, *, community_id: Optional[str] = None) -> None
async client.member_count(community_id: str, *, refresh: bool = False) -> int
async client.member_names(community_id: str, *, refresh: bool = False)
async client.members_with_role(community_id: str, role_id: str, *, refresh: bool = False)
async client.message(target: SendTarget, content: str, *, reply_to: Optional[Message] = None, attachments: Optional[List[str]] = None, delete_after: Optional[float] = None) -> MessageSendResult
      client.messages_for_container(container_id: str) -> tuple[Message, ...]
async client.mute(muted: bool = True) -> None
async client.open_dm(user: UserTarget) -> DirectMessage
async client.pending_friend_requests()
      client.permissions_for(user_id: str, channel_id: str) -> Optional[ChannelPermissions]
async client.pin(message: Message) -> None
async client.play(source: str, *, loop: bool = False) -> AudioPlayback
      client.preview(target: str, **kwargs)
async client.process_commands(message: Message) -> None
async client.react(message: Message, emoji: str) -> None
async client.refresh_communities(*, expand: bool = True) -> None
async client.remove_friend(user: UserTarget)
      client.remove_listener(event_name: str, coroutine) -> None
      client.remove_message_listener(listener) -> bool
async client.remove_profile_picture() -> None
async client.reply(message: Message, content: str, *, delete_after: Optional[float] = None) -> MessageSendResult
async client.reply_to(messages, content: str, *, container_id: Optional[str] = None, community_id: Optional[str] = None, notify: bool = False, **kwargs)
      client.reset_timings() -> None
async client.resolve_channel(channel_id: str) -> Optional[Channel]
      client.run(username: Optional[str] = None, password: Optional[str] = None) -> None
async client.save_asset(uri, path) -> Optional[str]
async client.save_profile_images(profile_or_user, directory = '.', prefix: Optional[str] = None) -> dict
async client.send_email_verification(username: Optional[str] = None, password: Optional[str] = None, *, token: Optional[str] = None, turnstile_token: Optional[str] = None, proxy: Optional[str] = None, transport: Optional[GrpcWebTransport] = None) -> None
async client.send_friend_request(username: str)
async client.send_once(token: str, container_id: str, content: str, *, community_id: Optional[str] = None, **kwargs)
      client.service(name: str)
async client.set_online_status(status)
async client.set_presence(status)
      client.set_user_id(user_id: str) -> None
async client.start(username: Optional[str] = None, password: Optional[str] = None) -> None
async client.start_token(token: Optional[str] = None, *, device_id: Optional[str] = None, web_api_url: str = 'https://api.rootapp.com/') -> None
async client.stop_playing() -> None
      client.timing_report() -> str
      client.timings() -> dict
async client.unblock(user: UserTarget)
async client.undeafen() -> None
async client.unmute() -> None
async client.unpin(message: Message) -> None
async client.unreact(message: Message, emoji: str) -> None
async client.unread_count()
      client.unwatch_all() -> None
async client.update_avatar(source) -> Optional[str]
async client.update_banner(source) -> Optional[str]
async client.update_description(description: Optional[str]) -> None
async client.update_profile(*, username: Optional[str] = None, description: Optional[str] = None, status: Optional[str] = None, avatar: Optional[str] = None, banner: Optional[str] = None) -> CurrentUser
async client.update_status(status: Optional[str]) -> None
async client.update_username(username: str) -> None
async client.verify_email(verification_code: str, username: Optional[str] = None, password: Optional[str] = None, *, token: Optional[str] = None, proxy: Optional[str] = None, transport: Optional[GrpcWebTransport] = None) -> None
async client.wait_for(event: str, *, check = None, timeout: Optional[float] = None)
async client.wait_until_ready() -> None
      client.watch_channel(container_id: str, *, community_id: Optional[str] = None, interval: float = 5.0, limit: int = 50)
      client.watch_presence(on_change, *, users = None, poll = None)
      client.watch_unread(*, interval: float = 3.0, include_dms: bool = True, mark_read: bool = False, callback = None, concurrency: int = 8, community_refresh: float = 120.0, dm_every: int = 20)
async client.whoami(*, refresh: bool = False) -> CurrentUser
```

Properties (no parentheses, never awaited): `calls`, `device_id`, `hub_url`, `is_connected`, `presence`

`gen` entries are async generators — iterate with `async for`, do not await them.

### Service namespaces

Grouped by what they touch. Aliases are noted where two names reach
the same object.

#### `client.admin` / `client.community_admin` — CommunityAdminService

*admin and community_admin are the same object.*

```python
async client.admin.clone_community(source_community_id: str, *, name: Optional[str] = None, clone_roles: bool = True, clone_access_rules: bool = True, clone_member_overrides: bool = False) -> Community
async client.admin.create_channel(community_id: str, channel_group_id: str, name: str, *, description: Optional[str] = None, channel_type: int = 1, use_channel_group_permission: bool = False, icon_token_uri: Optional[str] = None, access_rules: Optional[Iterable[AccessRule]] = None) -> Channel
async client.admin.create_channel_group(community_id: str, name: str, *, access_rules: Optional[Iterable[AccessRule]] = None) -> ChannelGroup
async client.admin.create_community(name: str, *, picture_hex: str = '#3f51b5', icon_upload_token_uri: Optional[str] = None, description: Optional[str] = None, reject_unverified_email: bool = False, is_age_restricted: bool = False, template_type: str = '') -> Community
async client.admin.create_role(community_id: str, name: str, *, color_hex: Optional[str] = None, community_permissions: Optional[CommunityPermission] = None, channel_permissions: Optional[ChannelPermissions] = None, mentionable: bool = False, self_assignable: bool = False) -> CommunityRole
async client.admin.delete_access_rule(community_id: str, channel_or_group_id: str, target_id: str) -> None
async client.admin.delete_channel(community_id: str, channel_id: str) -> None
async client.admin.delete_channel_group(community_id: str, group_id: str) -> None
async client.admin.delete_community(community_id: str) -> None
async client.admin.delete_role(community_id: str, role_id: str) -> None
async client.admin.edit_channel(community_id: str, channel_id: str, *, name: str, description: Optional[str] = None, update_icon: bool = False, icon_token_uri: Optional[str] = None, use_channel_group_permission: bool = False) -> Channel
async client.admin.edit_channel_group(community_id: str, group_id: str, *, name: str) -> ChannelGroup
async client.admin.edit_community(community_id: str, *, name: Optional[str] = None, picture_hex: Optional[str] = None, picture_token_uri: Optional[str] = None, update_picture: bool = False, default_channel_id: Optional[str] = None, reject_unverified_email: Optional[bool] = None, description: Optional[str] = None, is_age_restricted: Optional[bool] = None) -> Community
async client.admin.edit_role(community_id: str, role_id: str, *, name: str, color_hex: Optional[str] = None, community_permissions: Optional[CommunityPermission] = None, channel_permissions: Optional[ChannelPermissions] = None, mentionable: bool = False, self_assignable: bool = False) -> CommunityRole
async client.admin.list_access_rules(community_id: str, channel_or_group_id: str) -> Tuple[AccessRule, ...]
async client.admin.move_channel(community_id: str, channel_id: str, *, old_group_id: Optional[str] = None, new_group_id: Optional[str] = None, before_channel_id: Optional[str] = None) -> None
async client.admin.move_channel_group(community_id: str, group_id: str, *, before_group_id: Optional[str] = None) -> None
async client.admin.move_role(community_id: str, role_id: str, *, before_role_id: Optional[str] = None) -> None
async client.admin.set_access_rule(community_id: str, channel_or_group_id: str, target_id: str, overlay: ChannelOverlay, *, exists: bool = False) -> None
```

#### `client.assets` — AssetService

```python
async client.assets.download(uri) -> Optional[bytes]
async client.assets.get(uris, *, chunk_size: int = 5, concurrency: int = 16) -> dict
async client.assets.resolve(uris, *, chunk_size: int = 5, strict: bool = False, size: Optional[int] = None, concurrency: int = 16) -> dict
async client.assets.save(uri, path) -> Optional[str]
async client.assets.upload_bytes(data: bytes, *, filename: str = 'upload.png', modified: Optional[str] = None) -> str
async client.assets.upload_file(source) -> str
      client.assets.uri_for_id(asset_id, kind: str = 'image') -> str
async client.assets.url_for(uri) -> Optional[str]
```

#### `client.auth` — AuthClient

```python
async client.auth.hub_endpoint(token: str, device_id: str) -> str
async client.auth.login(username: str, password: str) -> AuthenticationSession
async client.auth.session_from_token(token: str, *, device_id: Optional[str] = None, web_api_url: str = 'https://api.rootapp.com/', fetch_hub: bool = True) -> AuthenticationSession
async client.auth.signup(username: str, password: str, email: str, *, access_token: Optional[str] = None, turnstile_token: Optional[str] = None, device_id: Optional[str] = None) -> AuthenticationSession
```

#### `client.blocks` — BlockManager

```python
async client.blocks.block(user_id: str)
async client.blocks.list()
async client.blocks.unblock(user_id: str)
```

#### `client.cache` — StateCache

```python
      client.cache.clear() -> None
      client.cache.ingest_community(extended) -> None
      client.cache.ingest_message(message) -> None
      client.cache.ingest_users(users: Iterable) -> None
      client.cache.member(community_id: Optional[str], user_id: Optional[str])
      client.cache.remember_member(community_id: Optional[str], member) -> None
      client.cache.remember_user(user_id: Optional[str], username: Optional[str] = None, **extra) -> None
      client.cache.stats() -> dict
      client.cache.user(user_id: Optional[str]) -> Optional[dict]
      client.cache.username(user_id: Optional[str]) -> Optional[str]
```

#### `client.calls` — CallService

```python
async client.calls.call_user(user) -> CallSession
async client.calls.create_session(container_id: str, *, community_id: Optional[str] = None, session_description: Optional[bytes] = None, supports_v2: bool = False) -> CallSession
async client.calls.detach(*, container_id: str, community_id: Optional[str] = None) -> None
async client.calls.disconnect() -> None
async client.calls.get_ice_info() -> IceInfo
async client.calls.join_channel(channel) -> CallSession
async client.calls.kick(*, container_id: str, community_id: Optional[str], user_id: str) -> None
async client.calls.play_audio(source: Union[str, MessageAttachment], *, loop: bool = False) -> AudioPlayback
      client.calls.require_active_target() -> tuple[str, Optional[str]]
async client.calls.set_mute_and_deafen(*, container_id: str, community_id: Optional[str] = None, muted: Optional[bool] = None, deafened: Optional[bool] = None) -> None
async client.calls.set_mute_and_deafen_other(*, container_id: str, community_id: Optional[str], user_id: str, muted: Optional[bool] = None, deafened: Optional[bool] = None) -> None
async client.calls.set_pause(*, container_id: str, community_id: Optional[str] = None, video_paused: Optional[bool] = None, screen_paused: Optional[bool] = None) -> None
async client.calls.stop_audio(*, close_peer: bool = True) -> None
async client.calls.tracks_create(*, track_id: str, mid: Optional[str], local_description_type: str, local_sdp: str) -> dict[str, Any]
```

#### `client.community` — CommunityManager

```python
async client.community.add_role(community_id: str, user_id: str, role_id: str)
async client.community.attach(community_id: str) -> None
async client.community.ban(community_id: str, user_id: str, **kwargs)
      client.community.cached() -> Tuple[Community, ...]
async client.community.clone(community_id: str, **kwargs) -> Community
async client.community.create(name: str, **kwargs) -> Community
async client.community.create_channel(community_id: str, channel_group_id: str, name: str, *, description: Optional[str] = None, channel_type: int = 1, use_channel_group_permission: bool = False, icon_token_uri: Optional[str] = None, access_rules: Optional[Iterable[AccessRule]] = None) -> Channel
async client.community.create_channel_group(community_id: str, name: str, *, access_rules: Optional[Iterable[AccessRule]] = None) -> ChannelGroup
async client.community.create_group(community_id: str, name: str, **kwargs)
async client.community.create_role(community_id: str, name: str, *, color_hex: Optional[str] = None, community_permissions: Optional[CommunityPermission] = None, channel_permissions: Optional[ChannelPermissions] = None, mentionable: bool = False, self_assignable: bool = False) -> CommunityRole
async client.community.create_text_channel(community_id: str, channel_group_id: str, name: str, **kwargs) -> Channel
async client.community.create_voice_channel(community_id: str, channel_group_id: str, name: str, **kwargs) -> Channel
async client.community.delete(community_id: str) -> None
async client.community.delete_channel(community_id: str, channel_id: str) -> None
async client.community.delete_channel_group(community_id: str, group_id: str) -> None
async client.community.delete_role(community_id: str, role_id: str) -> None
async client.community.detach(community_id: str) -> None
async client.community.detach_many(community_ids) -> None
async client.community.edit(community_id: str, **kwargs) -> Community
async client.community.edit_channel(community_id: str, channel_id: str, *, name: str, description: Optional[str] = None, update_icon: bool = False, icon_token_uri: Optional[str] = None, use_channel_group_permission: bool = False) -> Channel
async client.community.edit_channel_group(community_id: str, group_id: str, *, name: str) -> ChannelGroup
async client.community.edit_role(community_id: str, role_id: str, *, name: str, color_hex: Optional[str] = None, community_permissions: Optional[CommunityPermission] = None, channel_permissions: Optional[ChannelPermissions] = None, mentionable: bool = False, self_assignable: bool = False) -> CommunityRole
async client.community.fetch(community_id: str) -> CommunityExtended
async client.community.fetch_member(community_id: str, user_id: str)
      client.community.get(community_id: str) -> Optional[Community]
      client.community.get_channel(channel_id: str) -> Optional[Channel]
async client.community.get_channel_groups(community_id: str, *, refresh: bool = False) -> Tuple[ChannelGroup, ...]
async client.community.get_channels(community_id: str, *, refresh: bool = False) -> Tuple[Channel, ...]
      client.community.get_group(group_id: str) -> Optional[ChannelGroup]
async client.community.get_groups(community_id: str, *, refresh: bool = False)
async client.community.get_member(community_id: str, user_id: str, *, refresh: bool = False) -> Optional[CommunityMember]
async client.community.get_members(community_id: str, *, refresh: bool = False) -> Tuple[CommunityMember, ...]
async client.community.get_role(community_id: str, role_id: str, *, refresh: bool = False) -> Optional[CommunityRole]
async client.community.get_roles(community_id: str, *, refresh: bool = False) -> Tuple[CommunityRole, ...]
      client.community.held(community_id: str, *more) -> _HeldCommunities
async client.community.hold(community_id: str) -> AttachHold
async client.community.kick(community_id: str, user_id: str)
async client.community.list(*, refresh: bool = True) -> Tuple[Community, ...]
async client.community.move_channel(community_id: str, channel_id: str, *, old_group_id: Optional[str] = None, new_group_id: Optional[str] = None, before_channel_id: Optional[str] = None) -> None
async client.community.move_channel_group(community_id: str, group_id: str, *, before_group_id: Optional[str] = None) -> None
async client.community.move_role(community_id: str, role_id: str, *, before_role_id: Optional[str] = None) -> None
async client.community.release(community_id: str) -> None
async client.community.remove_role(community_id: str, user_id: str, role_id: str)
async client.community.servers(*, refresh: bool = True) -> Tuple[Community, ...]
async client.community.unban(community_id: str, user_id: str)
```

#### `client.community_apps` — CommunityAppManager

```python
async client.community_apps.add(**kwargs)
async client.community_apps.get(**kwargs)
async client.community_apps.get_settings(**kwargs)
async client.community_apps.initialize(community_id: str)
async client.community_apps.list(**kwargs)
async client.community_apps.remove(**kwargs)
async client.community_apps.set_settings(**kwargs)
async client.community_apps.update_version(**kwargs)
```

#### `client.community_files` — CommunityFileManager

```python
async client.community_files.create(community_id: str, container_id: str, source: str, *, directory_id: Optional[str] = None)
async client.community_files.delete(community_id: str, container_id: str, file_id: str, directory_id: str)
async client.community_files.download(**kwargs)
async client.community_files.edit(community_id: str, container_id: str, file_id: str, directory_id: str, name: str)
async client.community_files.get(community_id: str, container_id: str, file_id: str, directory_id: str)
async client.community_files.list(community_id: str, container_id: str, directory_id: str)
async client.community_files.move(community_id: str, container_id: str, file_id: str, *, old_directory_id = None, new_directory_id = None)
async client.community_files.search(**kwargs)
async client.community_files.search_community(**kwargs)
```

#### `client.community_service` — CommunityService

```python
async client.community_service.attach(community_id: str) -> None
async client.community_service.detach(community_id: str) -> None
async client.community_service.detach_many(community_ids) -> None
async client.community_service.get_extended(community_id: str) -> CommunityExtended
async client.community_service.leave(community_id: str) -> None
async client.community_service.list_mine() -> tuple[Community, ...]
```

#### `client.direct_messages` / `client.dm_service` — DirectMessageService

*direct_messages and dm_service are the same object.*

```python
async client.direct_messages.create(user_id: str) -> DirectMessage
async client.direct_messages.find(user_id: str) -> Optional[DirectMessage]
async client.direct_messages.get_or_create(user) -> DirectMessage
async client.direct_messages.list() -> tuple[DirectMessage, ...]
```

#### `client.directories` — DirectoryManager

```python
async client.directories.create(community_id: str, container_id: str, name: str, *, parent_directory_id: Optional[str] = None)
async client.directories.delete(community_id: str, container_id: str, directory_id: str)
async client.directories.edit(community_id: str, container_id: str, directory_id: str, name: str)
async client.directories.get(community_id: str, container_id: str, directory_id: str)
async client.directories.list(community_id: str, container_id: str)
async client.directories.move(community_id: str, container_id: str, directory_id: str, *, old_parent_directory_id: Optional[str] = None, new_parent_directory_id: Optional[str] = None)
```

#### `client.dm` — DMMemberService

```python
      client.dm.cached(user: UserLike) -> Optional[DirectMessage]
async client.dm.find(user: UserLike) -> Optional[DirectMessage]
async client.dm.open(user: UserLike) -> DirectMessage
async client.dm.reply(message: Message, content: str, *, delete_after: Optional[float] = None) -> MessageSendResult
async client.dm.send(user: UserLike, content: str, *, attachment_token_uris: Optional[List[str]] = None, parent_message_ids: Optional[List[str]] = None, delete_after: Optional[float] = None) -> MessageSendResult
```

#### `client.emojis` — EmojiManager

```python
async client.emojis.create(community_id: str, shortcode: str, source: str)
async client.emojis.delete(community_id: str, emoji_id: str)
async client.emojis.list(community_id: str)
async client.emojis.list_mine()
async client.emojis.resolve(emoji_ids: Iterable[str])
```

#### `client.friend_groups` — FriendshipGroupManager

```python
async client.friend_groups.create(name: str)
async client.friend_groups.delete(group_id: str)
async client.friend_groups.edit(group_id: str, name: str)
async client.friend_groups.list()
async client.friend_groups.move(group_id: str, *, before_group_id: str)
```

#### `client.friend_requests` — FriendRequestManager

```python
async client.friend_requests.accept(notification)
async client.friend_requests.accept_all()
async client.friend_requests.accept_from(user)
async client.friend_requests.decline(notification)
async client.friend_requests.decline_from(user)
async client.friend_requests.pending()
async client.friend_requests.pending_from(user)
async client.friend_requests.respond(notification, *, accept: bool)
async client.friend_requests.responded()
async client.friend_requests.send(username: str)
```

#### `client.friends` — FriendManager

```python
async client.friends.accept(notification_id: str, user_id: str)
async client.friends.friend_ids() -> set
async client.friends.get(user_id: str)
async client.friends.get_all()
async client.friends.groups()
async client.friends.is_friend(user_id: str) -> bool
async client.friends.list()
async client.friends.reject(notification_id: str, user_id: str)
async client.friends.remove(user_id: str)
async client.friends.request(username: str)
async client.friends.respond(notification_id: str, user_id: str, *, accept: bool)
```

#### `client.invites` — InviteManager

```python
async client.invites.code_exists(code: str) -> bool
async client.invites.create(community_id: str, *, code: Optional[str] = None, max_uses: Optional[int] = None, expires_at = None)
async client.invites.delete(community_id: str, invite_id: str)
async client.invites.info(code: str)
async client.invites.join(code: str, *, age_verified: bool = False)
async client.invites.list(community_id: str)
async client.invites.list_mine(community_id: str)
```

#### `client.logs` — LogManager

```python
async client.logs.app(**kwargs)
async client.logs.community(community_id: str, *, last_log_id: Optional[str] = None)
```

#### `client.members` — MemberManager

```python
async client.members.ban(community_id: str, user_id: str, *, reason: Optional[str] = None, expires_at = None)
async client.members.ban_bulk(community_id: str, user_ids: Iterable[str], *, reason: Optional[str] = None, expires_at = None)
async client.members.edit_nickname(community_id: str, user_id: str, nickname: str)
async client.members.get(community_id: str, user_id: str)
async client.members.kick(community_id: str, user_id: str)
async client.members.kick_bulk(community_id: str, user_ids: Iterable[str])
async client.members.list(community_id: str, user_ids: Iterable[str])
async client.members.list_all(community_id: str)
async client.members.roles(community_id: str, user_id: str)
async client.members.unban(community_id: str, user_id: str)
```

#### `client.message_cache` — LRUCache

```python
      client.message_cache.clear() -> None
      client.message_cache.get(key: str, default = None)
      client.message_cache.items()
      client.message_cache.keys()
      client.message_cache.pop(key: str, default = None)
      client.message_cache.set(key: str, value) -> None
      client.message_cache.values()
```

#### `client.messages` — MessageService

```python
async client.messages.add_reaction(message: Message, reaction: str) -> None
async client.messages.delete_message(message: Message) -> None
async client.messages.edit_message(message: Message, content: str, *, uris: Optional[List[str]] = None) -> Message
async client.messages.flag_message(message: Message, reason) -> None
  gen client.messages.history(container_id: str, *, community_id: Optional[str] = None, limit: Optional[int] = 200, before: Optional[float] = None, page_size: int = 50)
async client.messages.list(container_id: str, *, community_id: Optional[str] = None, direction: str = 'both', after: Optional[float] = None, limit: Optional[int] = None, include_deleted: bool = False) -> List[Message]
async client.messages.pin_list(container_id: str, *, community_id: Optional[str] = None) -> List[Message]
async client.messages.pin_message(message: Message) -> None
async client.messages.remove_reaction(message: Message, reaction: str) -> None
async client.messages.send(container_id: str, content: str, *, community_id: Optional[str] = None, attachment_token_uris: Optional[List[str]] = None, parent_message_ids: Optional[List[str]] = None, needs_parent_notification: bool = False, delete_after: Optional[float] = None) -> MessageSendResult
async client.messages.set_view_time(container_id: str, *, community_id: Optional[str] = None) -> None
async client.messages.unpin_message(message: Message) -> None
```

#### `client.moderation` — ModerationManager

```python
async client.moderation.ban(community_id: str, user_id: str, *, reason: Optional[str] = None, expires_at = None)
async client.moderation.invite_user(community_id: str, user_id: str, *, role_ids: Optional[Iterable[str]] = None)
async client.moderation.kick(community_id: str, user_id: str)
async client.moderation.list_bans(community_id: str)
async client.moderation.unban(community_id: str, user_id: str)
```

#### `client.notifications` — NotificationManager

```python
async client.notifications.count_unviewed() -> int
async client.notifications.counts_by_container() -> dict
async client.notifications.delete(notification_id: str)
async client.notifications.delete_all()
async client.notifications.list(**kwargs)
async client.notifications.mark_all_viewed()
async client.notifications.mark_viewed(notification_id: str)
```

#### `client.permissions` — PermissionManager

```python
      client.permissions.channel(**kwargs) -> ChannelPermission
      client.permissions.community(**kwargs) -> CommunityPermission
async client.permissions.create_rule(community_id: str, channel_or_group_id: str, target_id: str, **permissions) -> None
async client.permissions.delete_rule(community_id: str, channel_or_group_id: str, target_id: str) -> None
async client.permissions.edit_rule(community_id: str, channel_or_group_id: str, target_id: str, **permissions) -> None
async client.permissions.list_rules(community_id: str, channel_or_group_id: str)
      client.permissions.overlay(**kwargs) -> ChannelOverlay
      client.permissions.rule(target_id: str, **permissions) -> AccessRule
```

#### `client.roles` — RoleManager

```python
async client.roles.add_to_members(community_id: str, role_id: str, user_ids: Iterable[str])
async client.roles.create(community_id: str, name: str, *, color_hex: str = '', community_permission = None, channel_permission = None, is_mentionable: bool = False, is_self_assignable: bool = False)
async client.roles.delete(community_id: str, role_id: str)
async client.roles.edit(community_id: str, role_id: str, **kwargs)
async client.roles.get(community_id: str, role_id: str)
async client.roles.list(community_id: str)
async client.roles.move(community_id: str, role_id: str, *, before_role_id: Optional[str] = None)
async client.roles.remove_from_members(community_id: str, role_id: str, user_ids: Iterable[str])
async client.roles.set_primary(community_id: str, user_id: str, role_id: str)
```

#### `client.search` — SearchManager

```python
async client.search.files(**kwargs)
async client.search.messages(**kwargs)
```

#### `client.transport` — GrpcWebTransport

```python
async client.transport.close() -> None
      client.transport.open_plain_client(**options)
async client.transport.unary(*, endpoint: str, body: bytes, headers: dict[str, str], operation: str) -> httpx.Response
```

#### `client.user_settings` — UserSettingsManager

```python
async client.user_settings.get_note(user_id: str)
async client.user_settings.resend_verification_email()
async client.user_settings.set_community_invite_requirement(connection, email_verified: bool = False)
async client.user_settings.set_device_online_status(status)
async client.user_settings.set_dm_invite_requirement(connection, email_verified: bool = False)
async client.user_settings.set_friend_invite_requirement(connection, email_verified: bool = False)
async client.user_settings.set_max_online_status(status)
async client.user_settings.set_note(user_id: str, note: str)
```

#### `client.users` — UserService

```python
async client.users.get_profile(user_id: str)
async client.users.get_profiles(user_ids) -> dict
async client.users.get_self() -> CurrentUser
async client.users.set_banner(source: Optional[str]) -> Optional[str]
async client.users.set_description(description: Optional[str]) -> None
async client.users.set_online_status(status) -> UserOnlineStatus
async client.users.set_profile_picture(source: Optional[str]) -> Optional[str]
async client.users.set_status(status: Optional[str]) -> None
async client.users.set_username(username: str) -> None
```

#### `client.voice_admin` — VoiceAdminManager

```python
async client.voice_admin.kick(community_id: str, container_id: str, user_id: str)
async client.voice_admin.list(community_id: str, container_id: str)
async client.voice_admin.set_member_mute_deafen(community_id: str, container_id: str, user_id: str, *, muted: Optional[bool] = None, deafened: Optional[bool] = None)
```

### Objects you get back

Fields come from the dataclass definition, so this cannot drift from
the code. Objects returned by a fetch carry a client reference and can
act on themselves.

#### `AccessRule`

```python
# fields: target_id, overlay
```

#### `Asset`

```python
# fields: uri, url, links, mime_type, is_animated, expires_at, width, height
      asset.url_for_size(pixels: int) -> Optional[str]
      asset.best_url    # property
```

#### `AssetLink`

```python
# fields: url, max_dimension, width, height
```

#### `AudioPlayback`

```python
# fields: source, track_id, mid
```

#### `AuthenticationSession`

```python
# fields: token, device_id, hub_url, web_api_url
```

#### `BlockEvent`

```python
# fields: received_at, raw, user_id, username
```

#### `CallDetachedEvent`

```python
# fields: sequence, container_id, raw
```

#### `CallSession`

```python
# fields: session_id, container_id, command_id, audio_bandwidth, video_bandwidth, screen_bandwidth, screen_audio_bandwidth, backend, server_url, access_token
```

#### `Channel`

```python
# fields: id, community_id, channel_group_id, name, description, icon_asset_uri, before_channel_id, use_channel_group_permission, channel_type, position, community_app_id, last_activity_at, user_last_viewed_at, permissions, role_or_member_ids, packet_type, raw
async channel.delete() -> None
async channel.edit(**kwargs)
async channel.history(*, limit: Optional[int] = None, before = None, direction: str = 'both')
      channel.history_iter(*, limit: Optional[int] = 200, before = None, page_size: int = 50)
async channel.mark_read() -> None
async channel.move(**kwargs) -> None
async channel.reply_to(messages, content: str, **kwargs)
async channel.send(content: str, **kwargs)
      channel.is_text    # property
      channel.mention    # property
```

#### `ChannelActivity`

```python
# fields: channel_id, channel_name, community_id, last_activity_at
```

#### `ChannelDeletedEvent`

```python
# fields: sequence, channel_id, community_id, channel_group_id, cached_channel, raw
```

#### `ChannelEvent`

```python
# fields: received_at, raw, channel_id, name, community_id, community_name, channel_group_id, category, channel_type
      channelevent.is_text    # property
```

#### `ChannelGroup`

```python
# fields: id, community_id, name, position, permissions, role_or_member_ids, channels, raw
async channelgroup.create_channel(name: str, **kwargs)
async channelgroup.delete() -> None
async channelgroup.edit(**kwargs)
```

#### `ChannelOverlay`

```python
# fields: channel_full_control, channel_view, channel_use_external_emoji, channel_create_message, channel_delete_message_other, channel_manage_pinned_messages, channel_view_message_history, channel_create_message_attachment, channel_create_message_mention, channel_create_message_reaction, channel_make_message_public, channel_move_user_other, channel_voice_talk, channel_voice_mute_other, channel_voice_deafen_other, channel_voice_kick, channel_video_stream_media, channel_create_file, channel_manage_files, channel_view_file, channel_app_kick
      channeloverlay.allow_all() -> ChannelOverlay
      channeloverlay.deny_all() -> ChannelOverlay
      channeloverlay.inherit_all() -> ChannelOverlay
      channeloverlay.update(**kwargs) -> ChannelOverlay
```

#### `ChannelPermission`

```python
# fields: channel_full_control, channel_view, channel_use_external_emoji, channel_create_message, channel_delete_message_other, channel_manage_pinned_messages, channel_view_message_history, channel_create_message_attachment, channel_create_message_mention, channel_create_message_reaction, channel_make_message_public, channel_move_user_other, channel_voice_talk, channel_voice_mute_other, channel_voice_deafen_other, channel_voice_kick, channel_video_stream_media, channel_create_file, channel_manage_files, channel_view_file, channel_app_kick
      channelpermission.all() -> ChannelPermissions
      channelpermission.has(name: str) -> bool
      channelpermission.merge(other: ChannelPermissions) -> ChannelPermissions
      channelpermission.none() -> ChannelPermissions
      channelpermission.update(**kwargs) -> ChannelPermissions
```

#### `ChannelPermissions`

```python
# fields: channel_full_control, channel_view, channel_use_external_emoji, channel_create_message, channel_delete_message_other, channel_manage_pinned_messages, channel_view_message_history, channel_create_message_attachment, channel_create_message_mention, channel_create_message_reaction, channel_make_message_public, channel_move_user_other, channel_voice_talk, channel_voice_mute_other, channel_voice_deafen_other, channel_voice_kick, channel_video_stream_media, channel_create_file, channel_manage_files, channel_view_file, channel_app_kick
      channelpermissions.all() -> ChannelPermissions
      channelpermissions.has(name: str) -> bool
      channelpermissions.merge(other: ChannelPermissions) -> ChannelPermissions
      channelpermissions.none() -> ChannelPermissions
      channelpermissions.update(**kwargs) -> ChannelPermissions
```

#### `Command`

```python
# fields: name, callback, aliases, owner_only, description
```

#### `CommandErrorEvent`

```python
# fields: message, error
```

#### `CommandEvent`

```python
# fields: context
```

#### `Community`

```python
# fields: id, owner_user_id, default_channel_id, name, picture_hex, picture_asset_uri, reject_unverified_email, description, is_age_restricted, packet_type, raw
async community.clone(**kwargs)
async community.create_channel(channel_group_id: str, name: str, **kwargs)
async community.create_channel_group(name: str, **kwargs)
async community.create_role(name: str, **kwargs)
async community.create_text_channel(channel_group_id: str, name: str, **kwargs)
async community.create_voice_channel(channel_group_id: str, name: str, **kwargs)
async community.delete() -> None
async community.edit(**kwargs)
async community.leave() -> None
```

#### `CommunityDeletedEvent`

```python
# fields: sequence, community_id, cached_community, removed_channels, raw
```

#### `CommunityEvent`

```python
# fields: received_at, raw, community_id, community_name, user_id
```

#### `CommunityExtended`

```python
# fields: community, channel_groups, members, roles, raw
      communityextended.is_attached(user_id: str) -> bool
      communityextended.member(user_id: str)
      communityextended.role(role_id: str)
      communityextended.attached_user_ids    # property
      communityextended.channels    # property
      communityextended.text_channels    # property
```

#### `CommunityLeaveEvent`

```python
# fields: sequence, community_id, user_id, leave_reason, is_self, cached_community, removed_channels, raw
```

#### `CommunityMember`

```python
# fields: user_id, role_ids, raw, community_id
async communitymember.add_role(role_id: str)
async communitymember.ban(**kwargs)
async communitymember.kick()
async communitymember.remove_role(role_id: str)
      communitymember.user    # property
```

#### `CommunityPermission`

```python
# fields: community_manage_community, community_manage_roles, community_manage_emojis, community_manage_audit_log, community_create_invite, community_manage_invites, community_create_ban, community_manage_bans, community_full_control, community_kick, community_change_my_nickname, community_change_other_nickname, community_create_channel_group, community_manage_apps
      communitypermission.all() -> CommunityPermission
      communitypermission.none() -> CommunityPermission
      communitypermission.update(**kwargs) -> CommunityPermission
```

#### `CommunityRole`

```python
# fields: id, permissions, name, position, raw, community_permissions, color_hex, is_mentionable, is_self_assignable, channel_permission_raw, community_permission_raw, community_id
async communityrole.add_to(user_id: str)
async communityrole.delete()
async communityrole.edit(**kwargs)
async communityrole.move(*, before_role_id = None)
async communityrole.remove_from(user_id: str)
```

#### `Context`

```python
# fields: client, message, prefix, invoked_with, command, args, raw_arguments
async context.call_user(user: User)
async context.deafen() -> None
      context.get_channel(channel_id: str) -> Channel
      context.get_user(user_id: str) -> User
async context.hangup() -> None
async context.join_call(channel: Channel)
async context.mute() -> None
async context.play_audio(source: Optional[Union[str, MessageAttachment]] = None, *, loop: bool = False)
async context.reply(content: str, *, delete_after: Optional[float] = None) -> MessageSendResult
async context.send(content: str, *, delete_after: Optional[float] = None) -> MessageSendResult
async context.stop_audio() -> None
async context.undeafen() -> None
async context.unmute() -> None
      context.author    # property
      context.author_id    # property
      context.community_id    # property
      context.container_id    # property
      context.mentioned_channel    # property
      context.mentioned_channels    # property
      context.mentioned_user    # property
      context.mentioned_users    # property
```

#### `CreatedAccount`

```python
# fields: username, password, email, user_id, token, created_at, verified, device_id, note
      createdaccount.redacted() -> dict
```

#### `CurrentUser`

```python
# fields: id, username, email, profile_picture_asset_uri, is_email_verified, max_online_status, description, banner_asset_uri, user_defined_status, is_billable, raw
async currentuser.edit(*, username: Optional[str] = None, description: Optional[str] = None, status: Optional[str] = None, avatar: Optional[str] = None, banner: Optional[str] = None) -> CurrentUser
async currentuser.set_avatar(source: Optional[str]) -> Optional[str]
async currentuser.set_banner(source: Optional[str]) -> Optional[str]
async currentuser.set_description(description: Optional[str]) -> None
async currentuser.set_profile_picture(source: Optional[str]) -> Optional[str]
async currentuser.set_status(status: Optional[str]) -> None
async currentuser.set_username(username: str) -> None
```

#### `DetailedMember`

```python
# fields: member, profile
async detailedmember.add_role(role_id)
async detailedmember.ban(**kwargs)
async detailedmember.kick()
async detailedmember.remove_role(role_id)
      detailedmember.about_me    # property
      detailedmember.avatar_url    # property
      detailedmember.banner_uri    # property
      detailedmember.community_id    # property
      detailedmember.custom_status    # property
      detailedmember.description    # property
      detailedmember.is_deleted    # property
      detailedmember.online_status    # property
      detailedmember.profile_picture_uri    # property
      detailedmember.role_ids    # property
      detailedmember.user_id    # property
      detailedmember.username    # property
```

#### `DirectMessage`

```python
# fields: id, creator_user_id, member_user_ids, command_id, raw
async directmessage.history(*, limit: Optional[int] = None, before = None)
async directmessage.send(content: str, **kwargs)
```

#### `EndpointStats`

```python
# fields: calls, errors, retries, rate_limited, roundtrip_ms, roundtrip_total, roundtrip_low, roundtrip_high, wait_ms, overhead_ms
      endpointstats.record(roundtrip: float, wait: float, overhead: float) -> None
      endpointstats.summary() -> dict
      endpointstats.total_roundtrip_ms    # property
```

#### `EnumValue`

```python
# fields: enum_type, value, name
```

#### `EventErrorEvent`

```python
# fields: event_name, event, error
```

#### `Explanation`

```python
# fields: target, kind, title, doc, python_signature, members, wire, managers, services, hint
      explanation.as_dict() -> dict
      explanation.render() -> str
```

#### `FriendEvent`

```python
# fields: received_at, raw, user_id, username, friendship_id, group_id
```

#### `HostedAccount`

```python
# fields: name, token, setup, gateway, client, task, error, ready, options
```

#### `IceInfo`

```python
# fields: urls, username, credentials, features_raw
```

#### `MediaBackendInfo`

```python
# fields: available, backend, av_version, ffmpeg_libraries, error
```

#### `MemberEvent`

```python
# fields: received_at, raw, user_id, username, community_id, community_name, role_ids, presence
```

#### `MemberRoleEvent`

```python
# fields: received_at, raw, community_id, community_name, role_id, role_name, user_ids
      memberroleevent.user_id    # property
```

#### `Message`

```python
# fields: id, container_id, user_id, content, community_id, packet_type, message_type, deleted_at, edited_at, pinned_at, payload_raw, attachments, raw
async message.delete() -> None
async message.edit(content: str, *, uris: Optional[List[str]] = None) -> Message
async message.flag(reason: int) -> None
async message.pin() -> None
async message.react(reaction: str) -> None
async message.reply(content: str, **kwargs) -> MessageSendResult
async message.reply_with(others, content: str, **kwargs) -> MessageSendResult
async message.send_to_channel(content: str, **kwargs) -> MessageSendResult
async message.unpin() -> None
async message.unreact(reaction: str) -> None
      message.author    # property
      message.first_attachment    # property
      message.first_attachment_url    # property
      message.first_audio_attachment    # property
      message.is_deleted    # property
      message.is_dm    # property
```

#### `MessageAttachment`

```python
# fields: asset_uri, filename, length, mime_type, modified_at, file_type, download_url, raw
      messageattachment.display_name    # property
      messageattachment.is_audio    # property
      messageattachment.source    # property
```

#### `MessageEvent`

```python
# fields: sequence, message, action, raw
```

#### `MessageSendResult`

```python
# fields: id, container_id, community_id, content, command_id
```

#### `NotificationEvent`

```python
# fields: sequence, packet_case, raw
```

#### `Outcome`

```python
# fields: name, value, error
      outcome.ok    # property
```

#### `Page`

```python
# fields: items, cursor, raw
      page.has_more    # property
```

#### `Preview`

```python
# fields: alias, method, endpoint, request, response, method_type, values, fields, unframed, framed
      preview.as_dict() -> dict
      preview.render() -> str
      preview.size    # property
```

#### `RawRpcResult`

```python
# fields: service, method, request_type, response_type, body, http_status
```

#### `ReactionEvent`

```python
# fields: received_at, raw, message_id, user_id, username, shortcode, container_id, channel_name, community_id, community_name, is_direct
```

#### `ReaderStats`

```python
# fields: sweeps, requests, channels_opened, messages_read, pushed_messages, attached, started_at
      readerstats.requests_per_minute    # property
      readerstats.uptime    # property
```

#### `ReadyEvent`

```python
# fields: device_id, hub_url
```

#### `RoleEvent`

```python
# fields: received_at, raw, role_id, name, community_id, community_name, color_hex, mentionable, before_role_id
```

#### `RootEvent`

```python
# fields: received_at, raw
```

#### `RootExceptionInfo`

```python
# fields: error_code, id, who_id, what_id, where_id, parent_id, payload_kind, payload, payload_raw, raw
      rootexceptioninfo.summary() -> str
      rootexceptioninfo.validation_errors    # property
```

#### `SocketPacket`

```python
# fields: sequence, type, case, data, raw
      socketpacket.get(name: str, default = None)
      socketpacket.fields    # property
      socketpacket.is_unknown    # property
```

#### `StructuredResult`

```python
# fields: service, method, data, raw, request_type, response_type, http_status
```

#### `UnreadChannel`

```python
# fields: channel_id, channel_name, community_id, community_name, last_activity_at, user_last_viewed_at
```

#### `User`

```python
# fields: client, id, username
async user.call()
async user.deafen(ctx: Context)
async user.dm(content: str, *, attachment_token_uris = None, parent_message_ids = None, delete_after: Optional[float] = None)
async user.kick(ctx: Context)
async user.mute(ctx: Context)
async user.undeafen(ctx: Context)
async user.unmute(ctx: Context)
      user.mention    # property
```

#### `UserProfile`

```python
# fields: user_id, username, profile_picture_uri, banner_uri, description, custom_status, online_status, is_deleted, raw
      userprofile.about_me    # property
      userprofile.avatar_url    # property
```

#### `ValidationError`

```python
# fields: property_name, error_message, error_code
```

#### `WireMethod`

```python
# fields: alias, method, endpoint, request, response, method_type, signature, fields
      wiremethod.field_lines(indent: str = '    ') -> List[str]
```

### Module-level classes

```python
from rootpy import (
    RootClient,           # the client
    MultiClientHost,      # several accounts in one process
    AccountFactory,       # account creation
    CreatedAccount,       # what it returns
    TurnstileChallenge,   # a str (the URL) carrying signup context
    AlreadyCreatedError,  # raised when no challenge was needed
    StateCache, LRUCache, # caching
    TransportStats,       # timing counters
    # typed events
    FriendEvent, MemberEvent, MemberRoleEvent, RoleEvent, BlockEvent,
    # models
    Asset, AssetLink, UserProfile, DetailedMember,
)

from rootpy.exceptions import (
    RootError, GrpcWebError, TurnstileRequired,
    UsernameAlreadyExists, EmailAlreadyExists,
    format_root_error, get_error_info,
)
from rootpy.enums import (
    ErrorCodeType, ChannelType, UserOnlineStatus,
    MessageType, NotificationType, ContentFlagReason,
)
from rootpy.protocol import (
    grpc_frame, unwrap_grpc_web, iter_fields,
    string_field, length_field, field_key, encode_varint,
)
from rootpy.identifiers import (
    encode_root_guid, normalize_root_guid, create_desktop_device_guid,
)
```

### Account creation

```python
from rootpy import AccountFactory

factory = AccountFactory(email_pattern="hello-{tag}@example.com",
                         username_prefix="test")

await factory.create(turnstile_token=..., username=..., keep_client=False)
await factory.create_return_turnstile(username=..., device_id=...)   # -> TurnstileChallenge
await factory.create_with_turnstile(turnstile_token=..., challenge=...)
await factory.create_and_verify(turnstile_token=..., code_provider=...)
await factory.send_verification(account)
await factory.verify(account, code)
factory.emails() / factory.unverified() / factory.save(path) / factory.load(path)
```

### Multi-account host

```python
from rootpy import MultiClientHost

host = MultiClientHost(stagger=0.5, max_connections=20,
                       shared_transport=True)   # True is the default
# max_connections is the ceiling of whichever pool the accounts use: the whole
# host's when shared (the default), each account's when shared_transport=False.
# Also accepted as max_connections_per_account, its old name.
host.add(name, token, setup=async_fn, gateway=True, **client_options)
await host.start() / await host.wait_ready(timeout=30) / await host.run()
await host.stop()
host.client(name) / host.status() / host.remove(name)
```

---

## 15. Testing

The suite is the most reliable description of what actually works, because
every live test has been run against the real API.

```bash
pytest -q                      # 1880 offline: no token, no network
pytest -m live -v              # 225: needs ROOT_TOKEN
pytest -m live2 -v             # 122: needs ROOT_TOKEN and ROOT_TOKEN2
pytest -m "live or live2" -v   # 347
```

Live tests create a community they own, work inside it, and delete it. No ids
are hard-coded. Everything created is named `rootpy test*`.

**214 of 237 service methods (90%) are exercised against the live API.** See
§18 for the per-service table and for what is deliberately not covered.
The figure is measured, not counted:

```bash
python devscripts/covermap.py            # names what is *not* reached
python devscripts/covermap.py --strict   # lower bound, 205/237 (86%)
```

Two flags worth knowing:

```bash
pytest -m "live or live2" --timing --timing-json=t.json
```

Per-test milliseconds, split into round trip, rate-limit waiting and client
work, using the SDK's own `TransportStats`. A live run is ~82% network.

```bash
python devscripts/soak.py --selftest                        # offline
python devscripts/soak.py --stress 300 --reconnect-every 25
```

Two accounts, verifying delivery rather than observing it: each round the
sender DMs a nonce and the receiver must see it. Tracks delivery, latency,
duplicates, live object counts and RSS. It calibrates itself against a null
client first and will say so if its own counters drift — four earlier memory
"findings" were the instrument rather than the SDK.

### Writing new tests

Some conventions the suite enforces on itself, because each came from a real
failure:

* **Read the signature before writing the call.** `client.community` is
  `CommunityManager`; `client.admin` is `CommunityAdminService`. They have
  different method names for the same operations.
* **Do not await the synchronous cache readers** (`get_community`,
  `get_channel`, `get_message`, `get_user`, `messages_for_container`) or the
  async generator `messages.history`. An offline guard scans the live suite's
  AST and fails on either mistake.
* **Poll, do not sleep.** `eventually(check, describe=...)` from `conftest`
  replaces `await asyncio.sleep(2)` before a read-after-write. A guard fails on
  any fixed multi-second sleep in a live file.
* **Always pass `encoding="utf-8"`** to text I/O. A guard scans every `.py`;
  the platform default is cp1252 on Windows.
* **Assert the failure, not just the fix.** Several guards are verified by
  reintroducing the bug and confirming they fail.

## 16. Scope and conduct

This is an unofficial client for a third-party platform. Things to keep in
mind when building on it:

- **Check Root's terms.** Automating an account may not be permitted.
- **Tokens are full account access.** Never commit them; `tokens.txt` and
  `accounts.json` are gitignored.
- **Defaults exist to be polite** — rate-limit cooldowns, lazy loading,
  sensible sweep intervals. Don't tune them to eleven.

---

## 17. Protocol traps, in depth

Section 9 is the index; this is the evidence. Every item was found by a
failing call against the live API, and each says how it was settled — which
matters, because several of them look like bugs in your code until you know
otherwise.

Every one of these cost at least one live run. They are listed because none
could be inferred from the descriptors alone.

### Errors carry their reason in a header

Every rejected request answers `INVALID_ARGUMENT (3)` with the same sentence,
"One or more request values were rejected by Root". The actual reason is in the
`root-exception-bin` trailer as a `RequestValidatorList` of
`{property_name, error_message, error_code}`. Since 1.2.0 this is folded into
`str(exc)` and available as `exc.validation_errors`. Everything below was found
that way.

### An HTTP 401 is not a gRPC UNAUTHENTICATED

401 means the request was refused *before* gRPC, almost always because no
authorization header was sent. If some endpoints 401 while others succeed on
the same token, the cause is a request path not attaching the header.

### Edits are replaces

`CommunityEdit` and `CommunityRoleEdit` carry every field. Omitting one blanks
it — and Root answers `INTERNAL (13)` rather than a validation error. Both now
read the current object and carry unsupplied values forward. This pattern was
fixed three times, one field at a time, before being handled structurally.

### A wrong keyword never reaches the wire at all

`StructuredProtoCodec.encode_message` raises
`TypeError: <Request> has no field 'x'` for an unknown keyword — *before* the
request is built. So a manager that names a field wrong is not a subtle
protocol problem that shows up as a puzzling rejection; it is a method that
raises the instant anyone calls it, and for a rarely-used setter that can be
never.

All three `user_settings.set_*_invite_requirement` methods were in that state
from the beginning: they sent `is_required` at requests whose field is
`IsEmailVerified`. The offline suite pinned their *signature* and the enum
values and still could not see it, because nothing checked the keyword against
the schema. It does now — `TestEveryHighCallSiteSendsRealFields` sweeps all 92
`high.*` call sites.

The two conditions are independent, incidentally: `connection` is how closely
someone must already be linked to you, `is_email_verified` additionally
requires a verified address. They are ANDed.

### A default can defeat the layer below's sentinel

`CommunityRoleEdit` and `CommunityEdit` are replaces, so
`CommunityAdminService.edit_role` uses `color_hex=None` to mean "read the role
and carry its colour forward". `CommunityManager.edit_role` — the spelling
almost every caller uses — declared `color_hex: str = ""` and forwarded it
verbatim. `""` is not `None`, so the carry-forward never fired and
`normalize_hex_colour("")` raised `ValueError` before the request was built:
`client.admin.edit_role(cid, rid, name="x")` worked and
`client.community.edit_role(cid, rid, name="x")` could not be called at all.

This is the fourth time the replace semantics have bitten, and the first time
it was the *wrapper* rather than the request. `defaultcheck.py` sweeps every
`f(x=x)` forward in the package for the same shape.

### Blocking someone deletes the friendship, permanently

Measured directly, both directions:

```
friends            -> True
block()            -> friends: False
unblock()          -> friends: False   (not restored)
```

So `block` then `unblock` is **not** a no-op. DMs and calls both depend on the
friendship — Root's default privacy setting only accepts DMs from friends — so
the damage surfaces later and elsewhere as `PERMISSION_DENIED`, which reads
like a different bug entirely.

This had been happening on every live run since the blocking tests were
written. Nothing noticed because nothing after them looked at the friendship,
and the next run's `friendship` fixture quietly re-established it. It only
surfaced when a test was added that asserts `is_friend` up front.
`TestBlocking` restores the friendship in an **autouse** fixture now — the
first fix patched only the test that asserts the destruction and left the two
ordinary block/unblock tests still tearing it down, which failed the same way
one run later.

### Root content-addresses assets

Uploading identical bytes returns the identical asset id, and therefore the
identical `root://` URI. Two consequences that both bit:

- Any test asserting "the image changed" must upload **fresh** bytes. Setting
  the same PNG the account already wears is a no-op that looks like a broken
  setter. `conftest.make_png()` generates a unique valid PNG per call.
- Derivatives are shared too. A community file uploaded from the same bytes as
  a profile picture inherited the avatar's signed `image` derivative, which
  made the "only the `file` kind resolves" finding appear to flip.

### A file's name rule differs between Create and Edit

`FileEdit` validates `Name` with a `RegularExpressionValidator` and wants the
**stem, not the filename**. Measured one variable at a time:

| accepted | rejected |
| --- | --- |
| `renamedabcd` | `renamedabcd.png` |
| `renamed-abcd` | `renamedabcd.txt` |
| `renamed_abcd` | `renamed.abcd` |
| `Renamedabcd` | `renamed abcd` |
| `renamed1234` | |

So hyphens, underscores, digits and uppercase are all fine; it is **any dot**
and the space that are refused.

The trap is the asymmetry. `FileCreate` stores the uploaded filename complete
with its extension — `probe-1a2b.png` — and feeding that same string back to
`FileEdit` fails. Reading a name off a record and editing it is the natural
thing to do and it does not work. Add this to the per-object name table above.

### A community file's bytes cannot be retrieved

Two independent routes, both closed, both verified live:

- `FileGrpcService/Download` answers **`UNIMPLEMENTED (12)`** — with the asset
  id from the file record, with the file id in its place, and with the field
  omitted. It is not an argument problem.
- The asset service does not substitute. A file's `asset_id` builds a valid
  URI, but only the `"file"` kind resolves, and that returns an **unsigned**
  `static.rootapp.com` URL which is refused with HTTP 403 (and 400 if you
  attach the bearer token — S3 rejects it). The `"image"` kind — the one that
  yields signed, working `imagedelivery.net` URLs for avatars, banners and
  emoji — does not resolve for a file's asset at all, even when the file is a
  PNG.

Listing, renaming, moving and deleting all work. The content does not come
back. Both halves are pinned by tests that fail if Root ships either, which is
the notification you want.

### Asset URIs are a GUID plus a kind tag

```
root://asset/<base64url( <16 raw GUID bytes> 0x0A <len> <kind> )>
```

`AssetService.uri_for_id(asset_id, kind="image")` builds one; verified by
reconstructing two URIs Root itself produced, byte for byte. This matters
because several records carry a bare `asset_id` and every asset method takes a
URI, so there was previously no way across.

`kind` selects the derivative and the derivatives live on **different
backends** — `image` on signed Cloudflare Images, `file` on unsigned
`static.rootapp.com`. It is not cosmetic.

### List RPCs wrap one repeated field

`CommunityRoleListResponse.CommunityRoles`,
`CommunityMemberBanListResponse.CommunityMemberBans`, and so on. Return the
items, not the envelope — iterating the envelope yields field *names*, so
`role.id` raises `AttributeError`.

### Name rules differ per object and cannot be inferred from each other

| object | rule |
| --- | --- |
| community | spaces fine, reasonably long |
| channel group | **short**; `"grp abcd"` (8 chars, 2 words) works, longer forms rejected |
| channel | hyphens and digits fine (`rootpy-text-1a2b`) |
| role | username rule |
| nickname | username rule |
| username | 3–20 chars of letters, numbers, `_`, `.`; `_`/`.` not at ends or adjacent |

Channel group names took four attempts: hyphens were blamed, then digits, then
letters-and-spaces — all wrong. It is length or word count. `rootpy.validation`
enforces the username rule client-side.

### Colours are `#rrggbb`, exactly 7 characters

`PictureHex` is required on community create (`NotEmptyValidator`) and validated
with an `ExactLengthValidator` of 7. Role `color_hex` uses the same form.
`normalize_hex_colour()` accepts either form and emits the 7-character one.

### Channel types are 1 / 2 / 4 / 8

`{Unspecified: 0, Text: 1, ThreadedText: 2, Voice: 4, App: 8}`. Sending 0 is
rejected by a `PredicateValidator`; sending a wrong *non-zero* value succeeds
and silently creates the wrong kind of channel.

### Enum fields arrive as ints, and two names are ambiguous

The structured API returns raw protobuf values, so reading `.name` off a
`notification_type` gets `str(int)` and silently misbehaves — use
`SomeEnum.coerce(value)`. The same applies to the enum fields the packet
decoder now fills in (see §5): they are ints, not members. When *encoding*,
pass a `rootpy.enums` member or a plain int, and mind that `UserOnlineStatus`
exists in two packages whose values disagree: `RootApp.WebApi.Shared.Enums`
(the wire one) has `Active = 16` while `RootApp.Browser.Models` has
`Active = 3`. The *extracted* table (`data/enums.json`, what `client.explain()`
and the structured layer read) used to carry `Active = 0` for the wire enum:
the decompiled source writes it as the hex literal `0x10` and the extractor
dropped the literal, leaving a value indistinguishable from `Unspecified`.
`enums.json` now has 16 for both `UserOnlineStatus` and
`UserDeviceOnlineStatus`; `RootApp.Browser.Models.UserOnlineStatus` is a
genuinely different enum and still reads 3. `rootpy.enums.UserOnlineStatus` was
hand-written against the protocol and always had `ACTIVE = 16` — it is the
member to pass.

### A notification's `UserId` is its owner, not the sender

For a friend request that is *you*. The counterparty is in the payload —
`NotificationPayloadFriendshipInviteCreated {UserId, FriendUserId}` — and is
what `FriendshipInviteRespond` wants as `FriendUserId`.

### DMs require friendship

Root's default privacy setting only accepts DMs from friends, so
`DirectMessageCreate` returns `PERMISSION_DENIED` on a fresh pair of accounts —
and `call_user()` opens a DM first, so calls fail the same way. Send a request
with `add_friend(username)`, accept it with `friend_requests.accept_from()`.

`create()` is **not** idempotent; it answers `ALREADY_EXISTS`. Use
`get_or_create()`.

### Membership is not a subscription — `Attach` is

**This is the finding that closes the "why does nothing arrive?" question, and
it reverses what earlier versions of this file said.**

Until a community is *attached*, the hub sends no packet for it at all — not a
message, not a channel edit, nothing — while DMs, mentions and status changes
keep arriving on the same socket. That asymmetry is why a bot can look
perfectly connected and never react to a channel post, and why it looked for a
long time like the account needed "activating" by a real client.

One call:

```python
await client.community.attach(community_id)      # root.CommunityGrpcService/Attach
await client.community.detach(community_id)      # ...and back
await client.community.detach_many(ids)          # several, one request
```

Measured in a single run, same channel, post before and after the attach:

| | plain post | post mentioning the listener |
|---|---|---|
| **not attached** | nothing | `NOTIFICATION` |
| **attached** | `MESSAGE` at +0.2 s | `NOTIFICATION` + `MESSAGE` |

`CommunityAttachRequest` carries only the community id in field 10; the reply
is empty. The desktop client makes the call from `Community.attachAsync`, which
runs off `UpdateFromCommunityExtendedResponse` (its full-load path), and
`FullyUnload` calls `detachAsync` — which is exactly why the real client
receives live traffic only for communities it has actually opened, and why
`initializeReconnectServicesAsync` re-pulls `IsFullyLoaded` communities on every
reconnect. **The subscription lives with the hub connection, so it has to be
re-established after a reconnect.** `UnreadReader` watches `is_connected` and
re-attaches on the rising edge.

Attaching is **visible to other members**: the server broadcasts
`COMMUNITY_MEMBER_ATTACH` (5502) — `MemberService` marks the member present on
receipt, and clients show them as such. `detach` (5503) undoes it, and
`UnreadReader(detach_on_stop=True)` is the default for that reason.

That is also why attach and detach are `on_member_online`/`on_member_offline`
and not `on_member_join`/`on_member_leave`. The app hands an attach to
`SetAttached(userId, true, packet.OnlineStatus)` (`MemberService.cs:303-315`),
and the packet carries a `UserOnlineStatus` because presence is all it means.
Routing it as a join made "someone joined" mean "someone opened the community",
which is a different event with a different rate. `MemberEvent.presence` marks
which family an event came from.

What this replaces: an earlier version of this file claimed ordinary channel
messages were never pushed, then that they were pushed only to an account
"activated" by a real desktop client session. Both were artefacts of testing
without the attach. Every negative result behind them — `SetDeviceOnlineStatus`,
repeating it on a timer, the full startup RPC sequence before and after connect,
the token-embedded device id, forced resync, `GetExtended`, `MessageList`,
`SetViewTime`, posting first — is still correct as a *negative*; none of those
is the trigger. The trigger is `Attach`, and it was never called.

**Mentions are the exception that needs no attach**, and the notification *is*
mention-specific. An earlier measurement here recorded that posting any message
pings every member's socket with an identical `notification`; re-run as a
controlled A/B (plain, mention, plain, in one run, same channel) that does not
reproduce — a plain post to an unattached community produces no frame at all.
The `notification` fires for the mention and only the mention.

**No mention spelling produces a notification-list entry**, though. Measured
across three runs and four probes against a peer confirmed to be a member: not
`[@name](root://user/<id>)` — the format `rootpy.commands.USER_MENTION_RE`
matches, so the one Root's own client writes — nor the label-less markdown
variant, nor a bare `root://user/<id>`, nor the Discord-style `<@id>` the suite
used to send. The reader is not broken: `COMMUNITY_MEMBER_KICKED` and
`..._BANNED` entries arrive in the same run, and `TestMentionFormat` carries
that control test explicitly.

**Mentions are markdown links, not `<@id>`.** Whatever the notification-list
behaviour, the syntax is settled: `rootpy.commands` has parsed
`[@name](root://user/<id>)` and `[#name](root://channel/<id>)` since it was
written. `Channel.mention` emitted `<#id>` — a string the library's own parser
refuses — and there was no user builder at all, which is why the suite invented
one. `User.mention`, `Channel.mention` and `models.build_*_mention()` agree with
the parser now, asserted offline by round-tripping.

### Event handlers are fire-and-forget

`dispatch()` returns before message handlers and `add_listener` handlers have
run — deliberately, so a slow or raising handler cannot stall or kill the read
loop. `await client.drain_events()` when you need them finished.

### The gateway is a resync cycle

It sends a batch, closes with code 1000, and expects a reconnect. That is
normal, not an error. Backoff is reserved for other close codes.

### Other

- `MessageList` must always send `DateAt`. `Limit` is *optional* and must be
  **10–50 inclusive** when present — Root's own validator says
  `Limit: Must be between 10 and 50 [InclusiveBetweenValidator]`. Laddered
  live: omitted → 50 back; 1/2/5/9 → INVALID_ARGUMENT; 10–50 → exactly that
  many; 51+ → INVALID_ARGUMENT. This entry used to read "must omit `Limit`",
  which contradicted `messages.history()` sending `Limit=page_size` on every
  page and working.
- **`ChannelGroupCreate`'s `Name` takes ASCII letters, digits and apostrophes
  in at most _two_ whitespace-separated words**, with no leading or trailing
  whitespace. Length is not a constraint (65 chars accepted at one word).
  Refused: three or more words, hyphen, underscore, period, accented letters,
  emoji, leading/trailing space, empty. `test area` and `grp abcd` are good;
  `rootpy group bbcy` is not, and neither is `test-area`.

  Settled by a 40-candidate ladder that varied length, word count and
  character set independently — it had cost three wrong guesses, each of which
  blamed the character set. The real constraint was **word count**:
  `rootpy-ag-1a2b` failed on hyphens, but `rootpy group 7269` and
  `rootpy group bbcy` both failed simply for being three words.
- **Channel messages push only after `root.CommunityGrpcService/Attach`.** See
  "Membership is not a subscription" above for the measurements and the API.
  The matrix that used to live here -- receiver x desktop-client-signed-in x
  community-age -- was measuring the wrong variable: none of those probes had
  attached, so every cell that "worked" was a DM or a mention and every cell
  that did not was simply unsubscribed.
- **The device id is embedded in the token.** `ClientToken.Parse` base64url-
  decodes it and reads bytes[0:16] as the user id and bytes[16:32] as the
  device id -- verified: bytes[0:16] of a real token is exactly that account's
  user id, as a plain big-endian UUID. `BearerConnectionAuth` sends it as
  `x-root-Device-Id`, and rootpy does the same. Byte 8 of the device id encodes
  a `RootGuidType`; both test accounts read `18` (Desktop). Not a push
  condition, but worth matching.
- **Channel creation is not reliably announced.** With every packet recorded
  and nothing filtered, creating a channel in a community the account was
  attached to produced **no packet at all** -- not `CHANNEL_CREATED`, not
  anything. A *rename* of the same channel pushes `CHANNEL_EDITED`, and in one
  earlier run a creation was followed by `COMMUNITY_PERMISSION_UPDATE`, so it
  is not silent by design -- it just cannot be relied on. Messages in the new
  channel still arrive, because the attach is per community. Anything that
  needs to *know a channel exists* has to re-list; `ophanim.py` does that on a
  timer for exactly this reason.
- **Joining a community pushes `COMMUNITY_PERMISSION_UPDATE`, not
  `COMMUNITY_JOINED`.** Measured: accepting an invite produced a permission
  update and nothing else. A watcher keyed on the join packet stays unattached
  and misses the entire community, silently. Key on any packet naming a
  community you have not attached.
- **`messages.history()` used to stop after one page** (fixed in 1.23.0). The
  cursor stepped back by `page_size` *seconds*, so any container whose page
  spanned longer than that repeated itself and the walk ended early. Measured:
  50 walked where 1024 existed. If you see a history walk return exactly
  `page_size` rows, suspect the cursor, not the server.
- **Root ids are timestamp GUIDs.** Milliseconds since 2020-01-01 UTC live in
  the top 48 bits of the high word, and the low byte of that word is the
  `RootGuidType` (1 person, 2 community, 4 channel, 15 direct message).
  `rootpy.root_guid_datetime()` / `root_guid_type()` read them; the client does
  the same in `MessageGuid.ToDateTime()`. Useful for dating a backfilled
  message on the same clock as a pushed one.
- **`notification` fires for every frame, pings included.** The gateway
  dispatches it unconditionally per frame — the
  `dispatch("notification", event)` that follows every packet dispatch in
  `Gateway`'s read loop — so
  `wait_for("notification")` returns within about five seconds whatever you
  send. Two live tests and one documented protocol finding were built on it and
  were all measuring the keepalive: the payload recorded as "identical for a
  message with no mention", `
`, is a ping. Use
  `packet_notification`, or `socket_response` and check `packet.type`.
- **Root's unread test** is `LastActivityAt > UserLastViewedAt`, *both
  non-null*, and not a voice channel (client `Channel.HasActivity`). The
  both-non-null half matters: a channel never opened has no
  `UserLastViewedAt` and is **not** unread.
- **A plain member cannot see channels the owner creates in a fresh group.**
  Only the community's default `Text` channel is visible until an access rule
  grants `channel_view`. Any two-account channel test needs that rule or it
  silently watches nothing.

  Creating in an *existing* group with `use_channel_group_permission=True`
  works, and is visible to the peer immediately — but the group has to be
  identified through **`community.default_channel_id`**, whose channel's
  `channel_group_id` is the public group. Two shortcuts that look equivalent
  are not:

  - `groups[0]` — `GetExtended` does not return channel groups in a stable
    order. The same call came back `["General", "Admin"]` and
    `["Admin", "General"]` on consecutive runs.
  - "the first text channel" — same problem, `Admin-Text` sorts first just as
    often.

  Building into `Admin` makes every subsequent post look like a push failure,
  which cost three runs here. Note `default_channel_id` is populated on the
  `Community` from `create`/`ListMine`, not on the `CommunityExtended` detail.
  `devscripts/unreadtest.py` has the working version.
- `MessageList` returns deleted messages as tombstones with `deleted_at` set;
  `Message.is_deleted` flags them and `list()` filters them by default.
- `FileList` requires a `DirectoryId` — listing "a channel's files" means
  listing a directory.
- Asset URLs are signed and expire; store bytes, not URLs. Variants are
  typically 32 / 128 / 512 / 2048 px.
- Channel `ContainerId` equals its `Id`. Message RPCs only work on
  `channel_type` 1 or 2.
- An owner cannot *leave* their own community; they delete it.
- Profile writes (`set_status`, `set_description`, `set_username`) share a
  rate-limit quota and a large suite will hit it.
- `invites.code_exists()` rejects the code `invites.create()` just returned
  (`Code: PredicateValidator`) while answering False for nonsense. **Open
  question** — marked `xfail`, not hidden.

---

---

## 18. Coverage, and how to run it

```bash
pip install -e ".[dev]"

pytest -q                      # 1880 offline, no token, no network
pytest -m live -v              # 225 tests, needs ROOT_TOKEN
pytest -m live2 -v             # 122 tests, needs ROOT_TOKEN and ROOT_TOKEN2
pytest -m "live or live2" -v   # 347

pytest -m "live or live2" --timing --timing-json=t.json   # per-test ms
python devscripts/soak.py --selftest                      # no token needed
python devscripts/soak.py --stress 300 --reconnect-every 25
```

Live tests create a community they own, work inside it, and delete it. Nothing
is hard-coded — no channel, role or user ids. Everything created is named
`rootpy test*` so debris from a crashed run is greppable.

The original single-account baseline is still runnable on its own:

```bash
pytest -m live tests/test_live_integration.py tests/test_live_gateway.py   # 74
```

### Test layout

| file | what it covers |
| --- | --- |
| `test_untested_surface.py` | offline: contracts, guards, regressions |
| `test_error_paths.py` | offline: every gRPC/HTTP status mapping |
| `test_live_integration.py` | messaging, members, assets, account surface |
| `test_live_gateway.py` | connection, dispatch, watch loops, lifecycle |
| `test_live_services.py` | roles, invites, emojis, directories, files, logs |
| `test_live_files.py` | the rest of files, directories, assets, search |
| `test_live_permissions.py` | permission rules, member queries, profile |
| `test_live_admin.py` | the admin service and community manager |
| `test_live_two_accounts.py` | DMs, calls, friend requests, moderation |
| `test_live_settings.py` | notes, friends, DM service, notifications, apps |
| `test_live_performance.py` | round-trip budgets: how many requests each operation makes |

`conftest.py` holds the fixtures: `client` (token-only), `gateway_client`
(connected), `peer` (second account, connected), `sandbox`, `friendship`,
`joined_peer`, `png`, plus `eventually()` for polling instead of sleeping and
`ladder()` for measuring a server-side rule instead of guessing at it.

### Offline checks that exist because guessing cost runs

All five run in `pytest -q`; each also has a standalone script under
`devscripts/` that prints a readable report.

| check | what it catches | script |
| --- | --- | --- |
| `TestEveryHighCallSiteSendsRealFields` | a manager sending a keyword the request message does not have — `encode_message` raises `TypeError` before the request is built, so the method has *never* worked | `kwargcheck.py` |
| `TestLiveSuiteCallsBind` | a live test calling a method with the wrong arguments; `**kwargs` forwarders are traced one hop and checked against the wire schema | `bindcheck.py` |
| `TestManagerDefaultsDoNotDefeatTheSentinel` | a wrapper whose falsy default makes the layer below's `None`-means-carry-forward unreachable | `defaultcheck.py` |
| `TestEverySourceFileCompiles` | a file that `ast.parse` accepts and `compile` rejects, which silently exempts it from *every* AST guard | — |
| `TestDocumentationMatchesThePackage` | LLMS.md's surface listing drifting from the package — signatures, defaults, field lists and async-ness are *generated*, so a stale or incomplete listing fails the build | `gendocs.py` |

---

214 of 237 service methods (90%) are exercised by the live suite, directly or
through a high-level verb, and the whole live suite passes. `--strict` drops
every chain resolved by an ambiguous name and gives 205/237 (86%); the truth is
between them, and the gap is how much of the number is inference. Regenerate
both with `python devscripts/covermap.py [--strict]`.

| complete | partial — with what is missing |
| --- | --- |
| `community_admin` 19/19 | `calls` 3/14 — media, see below |
| `members` 10/10 | `community_apps` 2/8 — `add`, `get`, `get_settings`, `remove`, `set_settings`, `update_version`, all needing an installed app |
| `roles` 9/9 · `users` 9/9 | `community` 41/42 — `create_channel_group`, a covermap under-count, see below |
| `community_files` 9/9 | `friends` 10/11 · `friend_requests` 9/10 — `respond` on each, see below |
| `assets` 8/8 · `permissions` 8/8 | `messages` 11/12 — `flag_message`, see below |
| `invites` 7/7 | `user_settings` 7/8 — `resend_verification_email`, needs Turnstile |
| `notifications` 7/7 | `community_service` 5/6 — `detach_many`, see below |
| `directories` 6/6 | |
| `emojis` 5/5 · `moderation` 5/5 | |
| `friend_groups` 5/5 · `dm` 5/5 | |
| `direct_messages` 4/4 | |
| `blocks` 3/3 · `voice_admin` 3/3 | |
| `search` 2/2 · `logs` 2/2 | |

**`community.create_channel_group` is exercised; covermap cannot see it.** The
live tests reach it through `conftest.create_channel_group(manager, ...)`,
which takes the service as an *argument*, so nothing in the call chain names
the service and the walk cannot credit it. covermap documents this case in its
own module docstring rather than fixing it by crediting every definition the
walk lands on — that was tried and inflated the total by five. Read 214/237 as
a floor.

**`community_service.detach_many` is genuinely not covered live.** It is
`client.community.detach_many(ids)`, documented in §9 as the way to undo an
attach, and `UnreadReader` calls it on stop — so it is user-facing API, not
dead weight. What exercises it today is offline only: `test_wire.py` proves it
repeats field 10 per id and sends nothing for an empty list. Nothing has
watched a real server act on it. Detaching several communities at the end of a
live gateway test would close this.

`search` was listed as "undocumented kwargs" and written off. It is not:
`StructuredMethod.signature()` gives the request fields offline, and both
methods are now covered —

```
file.search_community(community_id, container_ids[], search)
message.search(container_id, community_id, search, last_message_id, limit)
```

**The friend request/response surface is covered now.** It used to be out of
reach: `friends.accept`/`reject`/`respond` and
`friend_requests.accept`/`decline`/`respond` all answer a *pending* request,
and the two test accounts are permanently friends, so there was never one to
answer. `TestFriendshipCycle` unfriends the pair and walks the whole cycle —
remove, decline, reject, accept, remove again, accept — verifying restoration
in a `finally` before it finishes. It is the last test in the last live
module, so nothing downstream depends on the friendship while it runs.

That run also answered a question nothing else had: **Root does let the same
person re-request immediately after being declined.** Three request/decline
rounds in a row all went through.

The two `respond` methods still show as uncovered only because the covermap
credits the wrapper (`accept`/`decline`/`reject`) rather than the shared
implementation they all call. They are exercised.

### Verified working

Confirmed against a live server by the suite above:

- **Messaging** — send, threaded replies, edit, delete, react, pin, paginated
  history, multi-reply (max 5 targets), tombstone filtering
- **Communities** — create, clone, edit, delete, channel groups, channels,
  roles, members, permissions, ordering (`move_*`)
- **Members and profiles** — listing, batched fetches (500/request, 8
  concurrent), random picks, nicknames, role assignment
- **DMs** — open, send, receive, history, conversation listing, both directions
  across two accounts
- **Calls** — signalling only: session creation, the peer receiving
  `packet_direct_message_ring`, teardown. No media.
- **Friends** — requests sent *and accepted* across two accounts, blocks,
  friend groups, private notes
- **Moderation** — kick, ban, unban, ban listing
- **Real-time** — connection, dispatch, `wait_for`, `watch_channel`, reconnect,
  handler isolation, `drain_events`
- **Assets** — `root://` resolution to signed URLs, saving to disk, upload
  (emoji and community files)

### Not verified

- **Voice media** (`client.calls`, 11 of 14 methods). It is not a WebRTC
  client: it drives a headless Chromium through Playwright. Deps are opt-in via
  `pip install "rootpy[voice]"` and are never installed automatically.
  `voice_admin.kick` and `set_member_mute_deafen` are exercised against an
  *empty* voice channel — that verifies the request is addressed and encoded
  correctly and reaches Root's handler, not that the kick takes effect. The
  test says so.
- **`community_apps`** (2/8) — `list` and `initialize` need no app; the other
  six take an `app_id`/`community_app_id` only a real installation produces.
- **Profile images** (`users.set_profile_picture`, `set_banner`) — correct and
  runnable, but **opt-in** via `ROOTPY_TEST_PROFILE_IMAGES=1`. The set works;
  the *restore* shares Root's profile-write quota and that quota outlasts a
  95-second backoff, so on two separate runs a real account was left wearing a
  test image with the original already discarded. The tests snapshot the
  previous image to disk before touching anything and name the file when they
  give up — but two methods of coverage do not justify state the suite cannot
  reliably undo.
- **`user_settings.resend_verification_email`** — the request takes a Turnstile
  token and it sends a real email. Out of scope on both counts. The other
  three `set_*_invite_requirement` methods *are* covered now: they only ever
  move the setting to its most permissive value, which cannot lock an account
  out, and the DM gate is re-checked afterwards.
- **`messages.flag_message`** — files a real content report with Root's
  moderation team. Covering it means sending noise to human reviewers, which
  is not worth one method.
- **Long-running behaviour.** See "What the soak does and does not prove".

---

---

## 19. Performance, measured

From `--timing` over a full live run. The timings below were taken when the
live suite was 332 tests; it is 347 now, so treat the wall clock as of its date
and the per-call figures — which is what the section is actually about — as
still current:

| | |
| --- | --- |
| wall clock | ~280 s |
| round trip | 57-60% of it, ~850 calls, **~190 ms/call** |
| fixture setup | 7% |
| rate-limit waiting | ~2 s |
| sleeps and client work | 34-36% |

The per-call figure has come down (213 ms over 496 calls previously) and the
share of wall clock spent *not* in RPC has gone up, both for the same reason:
the two-account file was 61 tests and 309 calls on that run (it collects 66
now), and much of what it does is wait for delivery on a socket rather than
issue requests. It alone was 40% of the run.

**Almost all of the non-RPC time is two tests waiting out a timeout they are
designed to lose.** `TestMentionFormat::test_some_mention_spelling_notifies`
walks five mention spellings, each burning a 12 s `eventually` window that
never resolves — 64.7 s of wall clock against 22.3 s of RPC. That is the
recorded `xfail`, so the cost is deliberate, but it means "36% sleeps and
client work" is not a diagnosis of the SDK: excluding that one test the share
drops to roughly 22%.

A third test used to sit beside it — `test_delete_all_empties_the_peer_inbox`
burned its full 20 s window on every run because `responses.items()` read an
empty inbox as one phantom row, so the condition could never become true. That
was a real bug, not a slow server; fixing it took ~20 s off every live run.

**The floor is round trips, not payload size.** A call costs ~190 ms whether it
carries 1 id or 4,000, so the lever is fewer, larger, concurrent requests. See
`get_members_detailed` (316 serial requests → 64 concurrent). **Two runs of
that benchmark are on record, and neither supersedes the other.** The 1.19.0
release measurement, in `docs/changelog.md`, is 63.7 s → 3.6 s. The shipped
docstring on `get_members_detailed` (`rootpy/highlevel.py`) records 60 s →
roughly 2 s, and breaks the concurrency half out separately: the same 316
requests went 60.2 s serial → 9.4 s at `concurrency=8`. Same 31,520-member
community, different runs — `docs/api.md` and
`docs/package/rootpy-internals.md` quote the docstring's pair. Cite one and say
which; do not average them into a third number.

`assets.resolve` is the **exception**, and the reason is worth knowing: one URI
Root dislikes rejects the *whole* request, and about 6% of the asset URIs on
real profiles are stale and rejected (measured over 120 real URIs: 113 returned
data, 7 refused, and 9 good + 1 refused was refused whole). The bigger the
chunk, the likelier it holds a poison URI, and the chunk then degrades to
one-request-per-URI. So it goes small and wide instead — **`chunk_size=5`,
`concurrency=16`**. Measured over 200 real URIs, 3 rounds each, no rate
limiting: chunk 100/conc 16 was 18.56 s, chunk 10/conc 8 (the old default)
2.62 s, chunk 5/conc 16 **1.63 s**. Past 16 concurrent it flattens (24, 32 and
48 came in at 1.55 / 1.44 / 1.53 s), so the extra rate-limit exposure buys
nothing. Raise `chunk_size` only if you know every URI is live.

Import is ~117 ms; `httpx` and the voice stack load on first use, not at
import. `client.timing_report()` splits request time into round trip, waiting
(rate-limit holds) and client overhead.

Cost traps: `get_members()` is cached and free after the first call;
`get_profiles([ids])` is **one request for any number of ids** — it makes a
single unary call with a repeated `length_field(10, ...)` per id and does no
chunking (`services/users.py`, `get_profiles`) — while looping `get_profile()`
is N; filtering on fields already present on a profile before hitting the asset
service removes ~90% of asset requests. The "one request per 100" wording this
line used to carry was wrong and is retracted in §6.

---

### What the soak proves, and what it does not

`devscripts/soak.py` runs two accounts and verifies delivery rather than only
observing: each round the sender DMs a nonce and the receiver must see it within
a deadline. It tracks delivery rate, latency, duplicates, cache size, background
tasks, live object counts and RSS.

```bash
python devscripts/soak.py --selftest                        # offline, ~1 s
python devscripts/soak.py --stress 300 --reconnect-every 25 # ~4 min
python devscripts/soak.py --hours 24                        # time-dependent only
```

**Read this before trusting any memory figure from it.** Four live runs were
spent attributing memory growth to the SDK, and each time the finding was the
measuring tool — a listener stacked on every reconnect, an RSS reader building a
`ctypes.Structure` per sample, a zero that meant "unmeasured". The structural
error was that there was never a control.

The tool now calibrates against a null client before measuring, subtracts its
own per-round cost, and refuses to present a retention finding as trustworthy
when it drifts. `--selftest` proves it flat in about a second.

**Measured offline, and settled:**

```
200 client construct+close cycles     +0 objects
100 transport build+close cycles      +0 objects   (real httpx, real SSL)
control (no work at all)              +0 objects
```

The client lifecycle and transport retain nothing.

**Still genuinely unknown**, because it is time-dependent rather than
volume-dependent and cannot be compressed: token or session expiry over hours, a
server deploy mid-run, anything keyed to time of day. **The longest run on
record is the `--stress 300` sweep above — single-digit minutes.** This file
carried "about four minutes" here and "~6 minutes" in §20 for several releases,
which is the tell that neither was measured; whoever runs `--hours 24` next
should replace this sentence with the real duration.

---

---

## 20. Open threads and known issues

Things that are true, unfinished, and worth knowing before you extend this.

**The DLLs have been read — that item is closed.** Earlier versions of this
file said all schema work came from the `.exe` and that the sibling assemblies
were the highest-value untouched target. They are touched: two decompiled trees
are checked in at `sources/RootSrc` and `sources/RootSrcV2` (27 decompiled
assemblies each), including 196 `*Reflection.cs` files, and **every one of the
831 entries
in `data/messages.json` carries a `source` naming the assembly and file it came
from** — 707 from `RootApp.WebApi.Shared.Entities`, 64 from `RootApp.Core`, 52
from `RootApp.Connect.Shared`, 7 from `RootApp.AppHub.Client`, 1 from
`RootApp.Client.Domain`. That is where the "which service does this schema
belong to" ambiguity went: it is answered per message, in the data, and you can
open the `.cs` file behind any schema you doubt.

**`source` is a logical identifier, not a path that resolves as written.** It
is spelled `<assembly>/<namespace-with-dots-as-slashes>/<Class>.cs`, but the
decompiled trees give each namespace one flat dotted directory, so rejoin the
middle segments with dots. All 831 recorded paths fail to open literally and
all 831 open after the rejoin. So
`RootApp.WebApi.Shared.Entities/RootApp/WebApi/Shared/Grpc/Requests/MessageCreateRequest.cs`
is on disk at
`sources/RootSrcV2/RootApp.WebApi.Shared.Entities/RootApp.WebApi.Shared.Grpc.Requests/MessageCreateRequest.cs`,
and the `ChannelEditedPacket` entry behind §5's field numbers is at
`sources/RootSrcV2/RootApp.WebApi.Shared.Entities/RootApp.WebApi.Shared.Packets/ChannelEditedPacket.cs`.
Both trees are laid out the same way, so the rule holds for `sources/RootSrc`
too. Nothing in the package reads the key — it is provenance for humans — so
the mismatch costs no runtime behaviour, only a failed `cat`.

What is left is **regeneration against a newer dump**, not extraction. The
schemas describe the app version those trees were taken from; when Root ships
one that adds fields, the way to catch up is to re-decompile and re-extract, not
to hand-patch `data/*.json`.

**There is no generator script in this repo.** `rootpy/data/*.json` is produced
by external tooling that does not live here, so "run the generator" is not
advice anyone can follow — a regeneration means rebuilding that step, and until
then the JSON is an input, not a build artifact. The offline guards are what
stand in for the missing build: `TestDocumentationMatchesThePackage` catches
§14 drifting from the package, and `kwargcheck`/`bindcheck` catch a schema that
no longer matches the code that calls it.

**The layering.** 27 distinct service objects on the client, five overlapping
manager layers, seven `send`-shaped entry points. Nothing is broken, but it has
cost real time twice: tests written against `CommunityAdminService` when
`client.community` is `CommunityManager`, and repeated sync/async confusion.
Merging is a breaking v2 decision.

**Untested surfaces should be verified or deleted.** `client.calls` is 11/14
unverified and drags in Playwright and Chromium. Moving it behind an
`experimental` namespace with a first-use warning would keep the
reverse-engineering without implying support.

### What to do next, in order

1. **`calls` 3/14** — signalling is verified, media is not, and this is now by
   far the largest single gap. It needs `av`, `aiortc`, `imageio-ffmpeg` and a
   Chromium download, none of which are installed. Decide whether the
   headless-Chromium architecture stays before testing around it; if it stays,
   an `experimental` namespace with a first-use warning is the honest framing.
2. **Why mentions do not notify.** The two live hypotheses are a per-account
   notification setting and "Root's client sends something we do not". A third
   account, or a capture of the real client's `MessageCreate`, would separate
   them. Do not guess between them — the `xfail` and its control test are
   there so the question stays visible instead of being papered over.
3. **`community_apps` 2/8** — the other six need an `app_id` that only a real
   installation produces. `initialize` answers `PERMISSION_DENIED (7)` for an
   ordinary account, and `logs.app` requires a `CommunityAppId`, so both are
   blocked on the same thing.
4. **A long soak.** `--stress` covers everything volume-dependent. What remains
   is genuinely time-dependent: token expiry, a server deploy mid-run, anything
   keyed to time of day. Nothing has run longer than the `--stress 300` sweep —
   single-digit minutes; see §19 for why the exact figure is not stated.
5. **Re-extract against a newer dump.** The DLL extraction itself is *done* —
   831 schemas, each naming the decompiled file it came from, against the trees
   in `sources/` (see §20, "Open threads"). What is open is keeping it current:
   a fresh decompile and a re-extraction when Root ships a version that adds
   fields. Note that the extractor is not in this repo, so this is not a
   `python devscripts/...` away — reconstructing that step is part of the task.

All six ladders from the previous round are narrowed to assertions now, which
was item 1 last time. `DirectoryMove` and `FileMove` both want the old *and*
new parent; `FileSearch`'s cursor may be omitted; `CommunityAppList`'s
`app_type` is optional. Each took its first candidate.

Deliberately **not** on this list, with reasons in §20: profile images
(opt-in — the restore loses to Root's quota), `resend_verification_email`
(Turnstile, and it sends a real email), and `messages.flag_message` (files a
real report with human moderators).

### Audit backlog — confirmed, not yet fixed

From the 1.19.0 audit, minus the entries since fixed. Each was verified by
reading the code; none is a guess. Ordered by how much it costs a user, not by
how hard it is.

*Cited by symbol, not by line.* This list used to give file:line, and every one
of them had drifted by the time anyone checked — a line number is invalidated by
any edit above it, including edits that have nothing to do with the defect, so a
stale citation sends a reader to unrelated code and quietly costs them the trust
they had in the entry. A symbol name moves with the thing it names and is
greppable.

**Correctness**

- **`Gateway.start()` hangs forever on a permanently failing connect**
  (`gateway.py`, `Gateway.start` — it waits for `_ready` *or* `_stopped`, and
  with reconnect on, `_stopped` never sets). With the shipped defaults,
  `await client.connect()`
  neither returns nor raises when the token is revoked, the hub URL is wrong,
  or the account is banned — the only symptom is repeated `log.warning` lines,
  and `start()`/`start_token()` inherit it. Needs a distinction between
  permanently fatal handshake failures (401/403) and transient ones, or a
  bounded first-connect window. *The reason this is not fixed here: it needs a
  live test against a revoked token, and deliberately revoking one of the two
  burner accounts would cost the rest of the suite.*
- **`client.get_user` drops the username on a cache hit**
  (`client.py`, `RootClient.get_user` — the `if cached is not None: return
  cached` arm never looks at the `username=` it was handed), so a `User` first
  cached from a bare-id lookup can never gain its name and `User.mention`
  renders the raw uuid.
- **`AccountFactory.make_username` truncates to 32** (`accounts.py`,
  `AccountFactory.make_username`, `return name[:32]`) while the library's own
  validator caps usernames at 20, so any `username_prefix` over 12 characters
  fails every `create()` after a solved Turnstile.

**Round trips**

- **`clone_community` issues a full `CommunityGetExtended` per created object**
  (`services/community_admin.py`, `CommunityAdminService.clone_community`) —
  ~32 of 85 requests for a modest community, and
  each returns the target's entire member/role/channel dump, so it is quadratic
  in bytes. Most exist only to look up an id the Create response already
  returned. Note the ordering constraint: roles, groups and channels are created
  in sequence because position is creation order, so only the *reads* can be
  hoisted or made concurrent.
- **`update_profile` makes up to 5 serial round trips** (`highlevel.py`,
  `update_profile`) and returns a `CurrentUser` reflecting none of them except
  the username.

**Dead weight**

- **~197 unreachable lines in `CallService`** (`services/calls.py`,
  `_to_root_offer_sdp` and `_from_root_answer_sdp` — nothing calls either; grep
  returns only their `def` lines), 14% of the largest service module. Reading
  them as live behaviour will mislead anyone debugging voice. The neighbouring
  `_parse_session_description` *is* live and was fixed in 1.29.0: the caller
  named a `_parse_description` that does not exist, so session SDP decoding
  raised `AttributeError` on every call.
- **`import statistics` costs 10–25 ms of every `import rootpy`**
  (`stats.py`, module-level `import statistics`) for one `median()` call in a
  diagnostics-only method, and it drags in `decimal`, `fractions` and
  `numbers`. It undercuts the deliberate deferral in `transport._httpx`, which
  goes to some trouble to keep httpx's ~45 ms off the import path and then
  hands 10–25 ms of it straight back. Move it into `EndpointStats.summary()`.

**The tests themselves**

These matter most, because a test that cannot fail is worse than no test:

- `test_features.py::test_heavy_registries_are_not_imported_eagerly` — its
  `subprocess.run(...)` has no `check=True`, so a probe that fails to start
  leaves `result.stdout` empty, `loaded` empty, and the assertion satisfied. It
  passes vacuously, and it is the only guard on the headline lazy-import
  property.
- `test_untested_surface.py::TestSoakCalibratesBeforeMeasuring::test_a_drifting_instrument_invalidates_its_own_findings`
  — a trailing `) or True` makes the second assertion unconditional, and it is
  the half the inline comment says is the point.
- `test_untested_surface.py::TestClientLifecycleRetainsNothing::test_close_releases_the_http_client`
  — it skips
  with `"httpx[http2] not installed"` on the path where `close()` *worked*
  (`result is None` is both the ImportError signal and the success value), so
  it reports SKIPPED with a false reason and can never pass. Confirmed by
  running it with httpx 0.28.1 and h2 4.3.0 both present.
- **A large share of live tests assert nothing, or only `is not None`** — every
  `edit_*` call goes unverified. Worth a pass converting "it returned" into "it
  changed what it said it changed". The "44% (146/329)" figure this line used to
  carry was computed against a suite size that has not existed for several
  releases; it is 347 live tests now, and the ratio has not been recounted.
- **A quarter of the offline suite's wall clock is spent inside
  `gc.get_objects()`** — 54.5 s of 216.6 s, across 902 calls in 36 tests, and
  roughly three quarters of it in `TestSoakCalibratesBeforeMeasuring`, which
  reaches the heap walk indirectly through `devscripts/soak.py::calibrate`
  (`TestSamplersDoNotAllocate` is most of the rest). The cost
  scales with pytest's own heap, not with the code under test: the same
  `calibrate(200)` issues the same 442 `gc.get_objects()` calls in 0.26 s in a
  bare interpreter and 26.5 s inside pytest, so these tests get slower every
  time a test is added anywhere. The "~78% ... is four `gc.get_objects()`
  tests" this line used to carry was wrong twice over — the four slowest tests
  come to about 63% of the run, not 78%, and not all of them are heap-walk
  tests at all (`test_the_email_classmethods_are_proxied` never calls it).
  Which tests land in the slowest four shifts between runs, so treat that set
  as unstable and the 25% aggregate above as the durable figure. Re-measured
  with `pytest -q -p no:randomly --durations=0`
  plus a plugin timing every `gc.get_objects()` call; 1,855 passed, 25 skipped.

---

## 21. How this has gone wrong before

Method notes. Every entry cost at least one wasted run, and they repeat if
nobody writes them down.

These are not hypotheticals. Each cost at least one live run.

**Read the signature before writing the call.** `client.community` is
`CommunityManager`; `client.admin` is `CommunityAdminService`. Different method
names for the same operations. Introspect first — `inspect.signature`,
`inspect.getsource` — rather than inferring from a sibling.

**Never edit code with a regex.** Four regex edits misfired: one applied to four
files out of five, one silently no-op'd because an earlier substitution had
already changed the text it matched, one produced `unwrap_list(x).data` in seven
methods, one inserted a docstring into the wrong function. Use exact-string
replacement and verify the result by reading it back or parsing the AST.

**Parsing is not resolving, and parsing is not compiling.** `ast.parse`
succeeding says nothing about whether names resolve. A missing import inside a
test body only fails at run time, which cost a live run. There is a guard for
this now.

It is worse than that, though. `lambda: len(await thing())` *parses* fine and
fails at `compile()` — the check lives in the symbol-table pass, not the
parser. And every AST guard is built on `_parsed_sources`, which **skips** a
file it cannot parse so one broken file does not mask the rest. Put those
together and a file with that mistake is silently exempted from the
await-contract check, the undefined-name check and the sleep check
simultaneously, while `pytest -q` still reports green. That happened while
writing the 1.16.0 notification tests, and the only symptom would have been a
collection error at the start of a live run.
`TestEverySourceFileCompiles` is the counterweight: `compile()` is strictly
stronger than `ast.parse`, so it subsumes it.

**Verify the guard, not just the green.** One "proof" that a guard worked passed
only because the deliberate break left the file syntactically invalid, so
collection errored and the guard never ran. Break it *validly*, watch it fail,
then restore.

**Instrument, don't guess.** Four separate values were guessed wrong repeatedly —
`picture_hex` twice, channel group names four times, `edit_role` three times.
What worked every time was a *ladder*: try candidate shapes, report which one
the server accepted, with the detail. `conftest.ladder()` is that pattern
generalised — pass `(description, coro_factory)` pairs, get back what worked
and a printed report of everything that did not. Six probes use it now.

**Check the descriptor before spending a run.** "Undocumented kwargs" is
usually not true. `search` was written off on those grounds for months;
`StructuredMethod.signature()` gives the request fields offline, and both its
methods went from 0/2 to 2/2 without a single exploratory run. The same
listing found the invite-requirement bug. `client.high.<alias>.describe()`
prints every method of a service with its fields.

**Suspect the instrument first.** Four memory "findings" from the soak were the
soak: a listener stacked per reconnect, an RSS reader building a class per
sample, a zero meaning "unmeasured". The structural error was having no
*control*. `soak.py --selftest` now proves the sampler flat before it measures,
and `TestClientLifecycleRetainsNothing` answers offline what four live runs were
spent guessing at.

It happened again, and faster than you would expect. A probe reported that
*none* of four user-mention spellings produced a notification — which would
have meant "only mentions and DMs are pushed" was wrong. It was measuring
`len(notifications.list())`, and that list is **paginated**: once the page is
full, a new arrival does not change the length. The same run's
`TestMentionsArePushed`, which waits on a pushed event instead of a count,
passed on `<@id>` — two instruments disagreeing in the same run, and the
count was the wrong one. Compare id *sets*, not lengths.

The tell was available before the run: a strong negative result ("none of
these work") that contradicts a passing test elsewhere is almost always the
instrument.

**A restore is more dangerous than the change it undoes.** The profile-picture
test set a new avatar, verified it, and then hit the shared profile rate limit
on the way back — leaving a real account wearing a 1x1 test PNG with the
original bytes already discarded. Guarding the *set* against
`RESOURCE_EXHAUSTED` was not enough; the call that matters is the one that puts
things back. Anything that mutates durable state now writes the original to
disk *before* changing it, retries the restore through the rate limit, and
verifies the restore landed — and says where the file is if it could not.

**Isolate one variable per run.** Changing the harness and reading a finding from
the same run means you learn nothing about either.

### Conventions the suite enforces on itself

Offline guards fail on all of these, so they are not suggestions:

- Don't await the synchronous cache readers (`get_community`, `get_channel`,
  `get_message`, `get_user`, `messages_for_container`) or the async generator
  `messages.history`.
- Use `eventually(check, describe=...)` instead of `asyncio.sleep(N)` before a
  read-after-write.
- Pass `encoding="utf-8"` to every text read or write.
- Every name used in a test must be imported or defined.
- Samplers must not allocate per call.
