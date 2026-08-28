# Package internals

How `rootpy` is put together, and why. This is the "why is it shaped like
this" document — for *using* the SDK start with [`../../README.md`](../../README.md)
or [`../api.md`](../api.md).

> This file previously documented a `RootClientPool` API — `pool.command()`,
> `pool.run(console=True)`, `pool.validate_clients()`, `client.send_message()`,
> `pip install rootpy` — none of which exist in this package or ever shipped
> in it. About 320 of its 442 lines described that API. It has been rewritten
> against the code that is actually here. The real multi-account surface is
> `MultiClientHost` (see [Multiple accounts](#multiple-accounts)).

## The layering

The library is deliberately stacked, and each layer is usable on its own. Going
down is always possible; going up is never required.

```
client.message(target, "hi")              HighLevelMixin  -- flat one-call verbs
client.messages.send(cid, "hi")           services/       -- typed per-service
client.high.message.create(...)           StructuredAPI   -- schema-driven, by name
client.raw.message.Create(...)            RawAPI          -- schema + raw bytes
client.transport.unary(endpoint=..., body=...)
                                          GrpcWebTransport-- HTTP/2, retries, stats
```

`client.raw.<service>` resolves a short name against the 31 wire services
(`client.raw.message` -> `root.v2.MessageGrpcService`) and each method on it is
a callable `RawMethod` carrying `request_schema` / `response_schema`. Both look
the method's request or response type name up in `MESSAGE_SCHEMAS` — the flat
table in `data/message_schemas.json` — falling back to a *unique* suffix match
and returning `None` when the name is unknown or the suffix matches more than
one message.

That fallback used to carry real weight: the schema table held 571 entries and
omitted repeated fields, so 25 methods answered `None` — among them
`UserGrpcService/GetSelf`, `CommunityGrpcService/ListMine`,
`DirectMessageGrpcService/List` and `/Find`, `WebRtcGrpcService/GetIceInfo`,
and `AssetGrpcService/Get`. It now holds 831, one per message and repeated
fields included, and exactly one lookup across all 259 methods still misses:
`root.AppReviewGrpcService/ListMine` takes an `Empty` request, and `Empty` is
not a message the client declares.

`client.raw.services()` lists the services; `client.raw.describe(service[,
method])` returns one as a dict, with both schemas already resolved into it.

| module | what it owns |
|---|---|
| `client.py` | `RootClient` — composition root; owns the transport, cache, gateway and every service |
| `highlevel.py` | `HighLevelMixin` — the flat verbs (`message`, `reply`, `react`, `history`, `whoami`) |
| `services/` | one module per wire service: messages, calls, assets, users, communities, community_admin, direct_messages |
| `domain_managers.py`, `features.py`, `object_api.py` | the `client.<noun>.<verb>` managers (`roles`, `members`, `invites`, `notifications`, `community`, …) |
| `structured_api.py` | encode/decode driven by the generated descriptors; `client.high.*` |
| `raw_api.py`, `protocol.py`, `packets.py` | gRPC-Web framing and protobuf primitives |
| `transport.py` | `GrpcWebTransport` — httpx, retries, rate-limit cooldowns, `stats` |
| `gateway.py` | the websocket: connect, resync, keepalive, frame decode, dispatch |
| `models.py` | the returned records (`Message`, `Community`, `Channel`, `CommunityMember`, …) |
| `exceptions.py`, `root_exception.py` | the typed error tree and Root's structured error payloads |
| `cache.py`, `discovery.py` | `StateCache`, and offline API discovery (`explain`/`preview`) |
| `host.py` | `MultiClientHost` — many accounts, one loop, one process |
| `responses.py` | the one reader for "get field X off whatever shape this response is" |
| `data/*.json` | the generated protocol registries, loaded lazily |

## The generated registries load lazily

`data/` is five files, about 1.1 MB of JSON: the same 831 message definitions
twice over — the structured descriptors in `messages.json` and the flat raw
schemas in `message_schemas.json`, keyed identically — plus the two service
tables (`services.json`, `rpc_services.json`, 31 services each) and the 86
enums. All five are produced by external tooling from the decompiled client
sources — there is no generator script in this repo to re-run.
`_registry_loader.LazyRegistry` is a read-only `Mapping` that
defers reading the file until the first key lookup, so `import rootpy` never
parses them. Measured: pulling `rootpy.raw_api` and `rootpy.structured_api` in
on top of an already-imported `rootpy` costs ~6 ms, and after it every registry
still reports `loaded == False`. First touch, timed one registry per cold
process (n=21 each): ~8 ms for `MESSAGES`, ~3 ms for `MESSAGE_SCHEMAS` even at
831 entries — that one lands above 3 ms about as often as below, so read it as
an approximation rather than a ceiling — and a median around 0.5–0.65 ms for
`ENUMS`, `SERVICES` and `RPC_SERVICES`. Those three are small enough that
process noise dominates: their medians are stable but individual runs stray to
roughly twice the median, so treat them as sub-millisecond rather than as a
bound.

`SIMPLE_MESSAGES` is the exception, and the reason is structural rather than a
matter of size: it is a `DerivedRegistry` that builds its index by iterating
`MESSAGES`, so touching it *first* pays the whole 727 KB `messages.json` parse
and costs ~8.5 ms cold. Touch it once `MESSAGES` is already resident and only
the index build remains, ~0.3 ms.

They are read-only on purpose — `resolve_enum` memoises against `ENUMS`, which
is only sound because nothing can mutate it.

## Responses have more than one shape

Root's structured payloads come back as an `AttrDict`, a plain `dict`, a
decoded object, or `None`. Every layer used to grow its own accessor and they
drifted. `responses.py` is now the single reader:

```python
from rootpy.responses import field, first, as_sequence, items, shape
```

Two traps it exists to close:

- **A list RPC wraps its rows in an envelope.** Iterating the envelope yields
  field *names*, not records. `items(payload, "notifications")` reads the named
  field out; `domain_managers.unwrap_list` does the same when the caller does
  not know the field name.
- **An empty repeated field is omitted entirely.** So an empty list response is
  a bare envelope with no such attribute. `items()` returns `[]` for that — it
  used to fall back to wrapping the envelope itself, which made zero rows read
  back as one phantom row and `len(...) == 1`.

## Errors

`exceptions.py` maps every gRPC status and HTTP code to its own class
(`GrpcAlreadyExists`, `GrpcPermissionDenied`, `Unauthorized`, …), so callers
match on a type rather than on an integer. `GrpcStatus` is an `IntEnum` —
compare against the enum or an int, never a string.

Root also sends a `root-exception-bin` header carrying a structured payload
that names the offending field. `root_exception.py` decodes it, and the
decoded entries hang off the exception as `validation_errors`:

```python
except GrpcInvalidArgument as exc:
    for error in exc.validation_errors:
        print(error.property_name, error.error_message, error.error_code)
    # -> Limit  Must be between 10 and 50  InclusiveBetweenValidator
```

That payload is how several protocol rules in this repo were settled — it is
usually faster to read Root's own validator message than to guess.

## The gateway

`Gateway` owns one websocket and reconnects itself. Three behaviours are not
obvious:

- **Messages arrive in resync batches, not on an idle socket.** The hub closes
  each batch with code 1000, so `resync_interval` (default 4 s) forces a
  reconnect to fetch the next one. That is what makes channel messages flow at
  all.
- **Channel messages are pushed, but only after `Attach`.** Membership is not a
  subscription. Until `await client.community.attach(community_id)` the hub
  sends no packet for that community at all — not a message, not a channel edit
  — while DMs, mentions and status changes keep arriving on the same socket,
  which is exactly what a broken listener looks like. After the attach a plain
  channel post arrives as a `message` event in ~0.2 s, so `wait_for("message")`
  returns promptly; without the attach it waits forever. This file previously
  said channel messages were never pushed and that anything verifying delivery
  had to go through a DM — that was an artefact of measuring without the
  attach, and it is retracted. See [`../../LLMS.md`](../../LLMS.md) §17,
  "Membership is not a subscription — `Attach` is".
- **A stale resume cursor is also reported in band.** `ClientNotification`
  field 1 is a `PacketErrorCode` (`UNSPECIFIED = 0`, `SYNC_LOST = 1`), and the
  server only writes it when it is non-zero, so it is absent from the
  overwhelming majority of frames. `_notification_info` decodes it and a
  `SYNC_LOST` dispatches a `sync_lost` event carrying that frame's sequence
  number. It is an *extra* signal beside the 4016 close code, not a
  replacement: a `SYNC_LOST` frame can still carry a packet, and everything
  downstream of the check runs as usual.

Each frame becomes a `SocketPacket` whose `.data` carries the decoded `fields`
plus raw hex. Two exploratory protobuf-to-JSON trees — `data["notification"]`
and `data["packet_container"]` — are **off by default**: building them cost
107 of the 169 µs each frame took, on every frame including keepalive pings,
for output nothing reads. Set `client.gateway.decode_debug_trees = True` when
reverse-engineering an unfamiliar packet.

## Multiple accounts

`MultiClientHost` runs many accounts on one event loop in one process
(~30 MB total, not ~30 MB each).

```python
host = MultiClientHost.from_tokens([token_a, token_b])
async with host:
    results = await host.broadcast(lambda c: c.invites.join(code))
```

- **`shared_transport` is on by default.** One TLS + HTTP/2 handshake for the
  whole host instead of one per account (~831 ms each). It used to default off
  because the cooldown table was shared with the pool, so one account's 429
  paused everyone; cooldowns are keyed per account now
  (`GrpcWebTransport._account_scope`).
- **A client only closes a transport it owns.** With a shared transport,
  `client.close()` once closed it for every account — the first account to shut
  down killed the rest. The host owns and closes the shared pool in `stop()`.
- **`broadcast` never raises.** It returns one `Outcome` per account carrying
  either the value or the exception, because partial success is the normal
  fan-out result. It is concurrent across accounts and sequential within one —
  measured at 1.10x a single call for a two-account fan-out.

`broadcast` is the *only* sanctioned fan-out, and an offline guard keeps
mass-messaging helpers off the single-account surface.

## Where the time goes

`client.timings()` / `client.timing_report()` break a run into `waiting`
(cooldowns and backoff — our own doing), `roundtrip` (network + server, not
separable from inside the process) and `overhead` (our framing and
bookkeeping).

The dominant cost is almost always round trips: a call is ~190 ms almost
regardless of payload size, so the wins usually come from making *fewer,
larger, concurrent* requests rather than smaller ones.

- `get_members_detailed` is the clean case: 500 ids per request, 8 in flight.
  It used to send 100 and await each batch in a `for` loop, so a 31,520-member
  community was 316 serial requests and ~60 s; it is 64 requests and roughly
  2 s now. The concurrency was the expensive half — the same 316 requests went
  from 60.2 s serial to 9.4 s at `concurrency=8`. Those are the figures
  `get_members_detailed`'s own docstring records, and they are one of two runs
  on record for this change: the 1.19.0 release note in
  [`../changelog.md`](../changelog.md) and §19 of
  [`../../LLMS.md`](../../LLMS.md) report 63.7 s → 3.6 s on the same
  31,520-member community, measured on that build. Both are real; wall clock on
  a live API moves with the network, and the round trip behind these runs is
  documented at 188–199 ms across three passes. Only the request counts, 316
  and 64, are fixed — they are `ceil(31520/100)` and `ceil(31520/500)`.
- `assets.get` / `assets.resolve` invert the rule, and knowing why is the point
  of the example: `chunk_size=5`, `concurrency=16`. One URI Root dislikes
  rejects the *entire* request, and about 6% of the asset URIs on real profiles
  are stale, so a big chunk is likely to contain a poison one and then degrades
  to a request per URI. Measured over 200 real URIs: chunk 100 / conc 16 took
  18.56 s, the old chunk 10 / conc 8 took 2.62 s, chunk 5 / conc 16 takes
  1.63 s. Past 16 the concurrency flattens out, so the extra rate-limit
  exposure buys nothing.

Note that the round-trip totals are exact, but the *median* comes from a
bounded 1000-sample window per endpoint.

## Conventions

- Synchronous cache readers (`get_community`, `get_channel`, `get_message`,
  `get_user`, `messages_for_container`) are **not** coroutines. Don't await
  them.
- `client.community` is `CommunityManager`; `client.admin` is
  `CommunityAdminService`. They cover the same operations under different
  method names — introspect before assuming.
- Enum values come from `rootpy.enums`, never from literals. `ChannelType` is
  `{UNSPECIFIED: 0, TEXT: 1, THREADED_TEXT: 2, VOICE: 4, APP: 8}` — the members
  are SCREAMING_CASE, and hardcoding `1` for voice silently creates a text
  channel.
- `client.explain("service.method")` prints a method's exact request fields
  offline, with no token and no network. Check it before spending a live run.
