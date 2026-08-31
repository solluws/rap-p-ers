# Changelog

## 1.30.0

Two faults that a single account can never see, and a fan-out that got about
ten times faster. Both faults had the same shape: the call returned success
and the thing the caller asked for did not happen, so nothing anywhere raised.

1913 offline tests, 352 live (225 `live`, 127 `live2`), and 216/240 service
methods reached by the live suite — 90%, or 206/240 (86%) under
`covermap.py --strict`. Six of the new live tests are two-account tests,
because neither fault below is visible from one account.

### Presence is `min(ceiling, device)`

Root computes what other people see from two independent values, and the lower
one wins. The ceiling is `UserGrpcService/SetMaxOnlineStatus`; the device is
`UserGrpcService/SetDeviceOnlineStatus`, which the desktop client announces on
every connect.

`set_online_status` sent the ceiling only. An account that never announced a
device was **invisible to everybody** while both requests returned success —
so a fan-out that called it appeared, correctly and silently, to do nothing.

`client.set_presence(status)` sets both halves, and `set_online_status` is now
an alias for it, so `go_online`, `go_idle` and `go_invisible` are fixed too. It
**raises** if the ceiling lands and the device does not: returning the ceiling
there would report the state the caller asked for while the account stayed
invisible, which is the whole fault repeated one layer up.

New `client.presence` reports the effective value. New
`client.watch_presence(on_change, users=..., poll=...)` covers both ways to
observe someone else: push, which needs an attach, and polling, which needs
neither an attach nor a socket. New module `rootpy/presence.py` holds the rule
as `effective_presence()`.

### An attach does not outlive the connection

The subscription lives with the hub connection, not the account, and this hub
closes each batch and expects a new socket. So an attach made once decays on
its own. Measured against a second account reading `AttachedUserIds`: still
there at +16 s with no gateway and gone by +31 s; survived +36 s when the
socket was killed under it and gone by +48 s. Nothing is raised — the account
keeps looking logged in and leaves the member list.

New module `rootpy/attach.py` with `AttachHold`, reached through
`async with client.community.held(id):`, or `client.community.hold(id)` and
`.release(id)` for a bot with no block to leave. It re-sends the attach every
time the socket comes back, and opens a socket if there is not one.

`CommunityExtended.attached_user_ids` and `.is_attached(user_id)` are new, and
are the only way to confirm an attach took hold. That used to mean decoding
field 20 of the raw response by hand.

### Retries, and a counter that read zero

Root answers a non-member's message send with **14 (UNAVAILABLE)**, not
PERMISSION_DENIED, so the transport retried a permanent failure twice — the
same 7 of 24 accounts failed identically at concurrency 8 and at 64.
`GrpcWebTransport` now takes `retry_statuses` and a
`should_retry(endpoint, status, attempt)` hook.

`RESOURCE_EXHAUSTED` (8) now counts as rate limiting. It never sets HTTP 429,
so `stats.rate_limited` read 0 while calls were plainly being throttled —
which is the one number you reach for when a fan-out slows down.

### Fan-out speed

Measured across 1,307 accounts on one shared transport:

| phase | before | after |
|---|---|---|
| login | ~110 s | 19.6 s |
| presence | 348.8 s | 20.8 s |
| attach | 335.3 s | 32.6 s |
| detach | 166.7 s | 13.5 s |

`RootClient(defer_hub=True)` takes the hub lookup off the login path and does
it in `connect()`, when something actually wants a socket: 38.7/s → 60.9/s.
`connect(announce_device=False)` lets a caller announce the device itself.

`MultiClientHost.broadcast(concurrency=...)` no longer defaults to a flat 8,
which costs 28 s a command at 1,089 accounts. It scales with the account count
and caps at 64; a host of 8 accounts or fewer is unchanged. New
`MEASURED_CEILINGS` records what one IP gets — ~120/s for plain calls, ~60/s
for login, ~87/s for attach, and only ~17/s for websocket handshakes. They are
not interchangeable, and treating them as one number is what made a
1,307-account attach take 335 s.

## 1.29.0

A correctness pass against client **0.9.128**. Almost nothing here was
designed: the gateway decoder, the enum tables and the generated registries
were read back against `sources/RootSrcV2` field by field, and everything that
disagreed with the app was corrected. Some of those disagreements were bugs
that had already shipped, and one correction changes which handler a packet
arrives at. The one genuine addition is the two typed events below, both of
which existed on the wire and had no route.

1880 offline tests, 347 live (225 `live`, 122 `live2`), and 214/237 service
methods reached by the live suite — 90%, or 205/237 (86%) under
`covermap.py --strict`, which drops the name collisions the ordinary walk
follows into every candidate. The truth is between them, as always; the gap is
how much of the number is inference.

### New: `on_role_move` and the reaction events

Two slots reached the gateway and stopped there. Both are now typed events.

**`on_role_move`.** `COMMUNITY_ROLE_MOVED` (slot 92, packet type 5404) had no
key in the routing table, so the builder returned nothing and the event never
fired — while the subroute table listed `5404: role_move` for a slot that can
never carry it. `RoleEvent` gained **`before_role_id`**, from field 6, which is
the only payload a move carries beyond the two ids. An empty string means the
role moved to the top of the list.

**`on_reaction_add` / `on_reaction_remove`.** `MESSAGE_REACTION` (slot 173) had
no route at all, so reactions were reachable only as raw
`on_packet_message_reaction`. One `MessageReactionPacket` serves four packet
types — 5801/5802 for channels, 204/205 for direct messages — so the slot
subroutes on field 1 exactly as the role slot does. Without that, a removed
reaction is indistinguishable from a new one.

The new `ReactionEvent` carries `shortcode`, `message_id`, `user_id`,
`container_id` and **`is_direct`**. That last flag is not a convenience: a
direct-message reaction carries no `community_id`, so without it a DM reaction
looks like a channel reaction in a community you cannot look up.

### Fixed: `on_role_delete` had an empty `role_id`

Field 4 is the role id on all three role slots, but the schemas name it
differently — `CommunityRolePacket` and `CommunityRoleMovedPacket` call it
`id`, `CommunityRoleDeletedPacket` calls it `community_role_id`. The builder
read only `id`, so every real delete arrived with `role_id=""` and no way to
tell which role went. It reads both names now.

### Breaking: attach is presence, not membership

`COMMUNITY_MEMBER_ATTACH` (5502) and `COMMUNITY_MEMBER_DETACH` (5503) no longer
fire `on_member_join` / `on_member_leave`. They fire **`on_member_online`** and
**`on_member_offline`**, and `MemberEvent` gained **`presence: bool`** — `False`
on the membership family, `True` on the presence one, so one handler can take
both and still tell them apart.

An attach is a client *opening* a community, not a person joining it. The
desktop app hands the packet straight to
`SetAttached(packet.UserId, true, packet.OnlineStatus, "AttachPacket")`
(`MemberService.cs:308`), and `CommunityMemberAttachPacket` carries a
`UserOnlineStatus` in field 5 for precisely that reason. `COMMUNITY_JOINED`
(5302) and `COMMUNITY_LEAVE` (5303) are the genuine membership packets, and
they are now the only two things that can fire `on_member_join` /
`on_member_leave`.

**What this breaks:** an `on_member_join` handler was being fed presence
without being told. Anything counting joins was counting people opening the
community in their client — a different event with a different rate, and one
that fires again for the same person the next time they open it. A handler that
wants that firing rate back should move to `on_member_online`; a handler that
meant *joined* was wrong before and is right now, without being touched.

### Bugs that shipped

- **Every kick and every ban decoded as `UNKNOWN`.** `CommunityLeaveReason` had
  `KICKED = 2` and `BANNED = 3`, and neither value exists on the wire:
  `RootApp.WebApi.Shared.Enums/CommunityLeaveReason` is `Unspecified = 0`,
  `User = 1`, **`Kicked = 4`**, **`Banned = 7`**. `from_value()` fell through to
  `UNKNOWN` for both — which is also what it returns for a value it could not
  parse at all, so a kick was indistinguishable from a decode failure. The names
  are unchanged; only the numbers moved. `UNKNOWN` keeps 0 deliberately, because
  that is the app's `Unspecified`.

- **Any call that received an SDP died on an `AttributeError`.**
  `CallService._parse_session` called `cls._parse_description(value)` for
  `WebRtcSessionCreateResponse` field 4. The method is
  `_parse_session_description`; there was no `_parse_description` on the class,
  so a session whose response carried a description failed on the name, not on
  the protocol — and field 4 is `SessionDescription`, the half of the handshake
  a join exists to read. `rootpy/services/calls.py:189` calls the method that
  exists.

- **`is_text` was `False` on every channel edit and delete.** `ChannelType`
  rides on `ChannelCreatedPacket` only (field 12); the edited packet writes
  fields 1 and 3–11 and the deleted packet only 1/3/4/5 — never 12 — so a flat
  `channel_type = 0` made `if event.is_text:` drop 100% of them. It is
  **three-valued** now: `True`/`False` when the type is known — the builder
  fills it in from the client's channel cache when the packet omits it — and
  `None` when nothing here knows, which is a different answer from `False`.
  `True` means `TEXT` (1) or `THREADED_TEXT` (2).

- **`UserDeviceOnlineStatus.Active` was `0`, which is `Unspecified`.** The
  descriptor generator dropped the hex literal from `Active = 0x10`
  (`UserDeviceOnlineStatus.cs:14`) and wrote 0 — the finding that made 1.11.1
  stop routing online-status encoding through `resolve_enum` at all.
  `data/enums.json` carries **16** now, and the same sweep corrected
  `RootApp.WebApi.Shared.Enums.UserOnlineStatus.Active`.
  `RootApp.Browser.Models.UserOnlineStatus` is a *different* enum whose `Active`
  really is 3; it was left alone, and it is still what makes the leaf name
  ambiguous.

- **`AccountFactory.create_and_verify` asked for a second verification email by
  default.** Signup already emails the code, and the resend is gated behind its
  own Turnstile challenge (`action=resend_verification`) that the signup token
  does not satisfy — so the unasked-for resend usually raised
  `TurnstileRequired` and threw away credentials that had just been minted
  successfully. The new keyword-only `resend: bool = False` defaults to waiting
  for the email signup already sent. Pass `resend=True` only if the first one
  genuinely never arrived, and expect to solve that second challenge.

### The gateway reads field 1 now

- **Every packet carries its own `PacketType`, and all 90 schemas decode it.**
  Field 1 is `('packet_type', 'enum', False)` on every entry in
  `packet_schemas.py`, so `pkt.fields["packet_type"]` holds the app's real type.
  It is not redundant with the container's oneof case, because one slot
  multiplexes several packet types: case 173 carries both
  `ChannelMessageReactionCreated` (5801) and `ChannelMessageReactionDeleted`
  (5802), and field 1 is the only thing on the wire that tells an add from a
  remove.

- **`COMMUNITY_ROLE` sub-routes on it.** Slot 90 carries `CommunityRolePacket`
  for create *and* edit, and the app discriminates on the packet's own
  `PacketType` before deciding what to do with it (`RoleService.cs:74-92`).
  Routing on the slot alone meant a rename, a recolour or an `is_mentionable`
  toggle all arrived as `on_role_create`, carrying a `RoleEvent` no different
  from a genuine creation. Now 5401 → `role_create`, 5402 → `role_edit`,
  5403 → `role_delete`, 5404 → `role_move`. 5403 and 5404 have their own slots
  and never reach here; they are in the table because they are legal values of
  the same field. A packet whose field 1 does not decode keeps the slot's
  default route rather than guessing.

- **Enum payload fields decode as enums.** A new `'enum'` kind in the packet
  schemas, so values that used to be dropped on the floor now arrive named:
  `UserSetStatusPacket` field 4 `online_status`, `UserSetMaxStatusPacket` field
  3 `max_status`, `NotificationPacket` field 7 `notification_type`,
  `MessagePacket` field 11 `message_type`, `CommunityMemberAttachPacket` field 5
  `online_status`, `CommunityLeavePacket` field 5 `leave_reason` — the one the
  first bug above made useless — and `BillingSubscriptionStatusChangedPacket`
  field 11 `status`.

- **New event: `sync_lost`.** `PacketErrorCode` (`UNSPECIFIED = 0`,
  `SYNC_LOST = 1`) is new in `rootpy/enums.py` and exported from `rootpy`.
  `ClientNotification` field 1 is decoded now, and a `SYNC_LOST` frame logs a
  warning and dispatches `sync_lost` with the sequence number as its only
  argument. This is the server saying our resume cursor is no longer valid,
  stated in band and *before* the socket goes away — the same condition the 4016
  close code reports, but the one form of it that does not have to be inferred
  from close-code text. It is an extra signal, not a replacement: everything
  downstream still runs, because a `SYNC_LOST` frame can also carry a packet.
  The field is absent on the overwhelming majority of frames — the server skips
  `PACKET_ERROR_CODE_UNSPECIFIED` on write — so `_notification_info` returns the
  code *last* and the long-standing three-value unpack keeps working unchanged.

### The registries were one client behind

One cause, several consequences: `rootpy/data/*.json` was generated from client
**0.9.127** and never regenerated for **0.9.128**. Everything in this section
falls out of rebuilding them. Most of it is the WebRTC surface, which is where
0.9.128 did its work.

- **`calls.create_session` gained keyword-only `supports_v2: bool = False`**,
  which emits `WebRtcSessionCreateRequest` field 13 only when true. The app
  omits it when false, so the default keeps the bytes on the wire
  byte-identical to what this client sent before 0.9.128.
- **`CallSession` gained `backend: int = 0`, `server_url` and `access_token`** —
  response fields 17/18/19. When the server routes a call to the second media
  backend it answers with the URL and token to use, and that was unreachable
  before, because the response decoder stopped at field 16. `backend` is a
  `WebRtcBackend`: 0 unspecified, 1 V1, 2 V2.
- **`WebRtcBackend` is a new enum** in `data/enums.json`, which now has **86**
  entries (was 85).
- **`ErrorCodeType` gained `WEB_RTC_BACKEND_MISMATCH = 6000` and
  `WEB_RTC_CALL_BANNED = 6001`** — 50 members now, and `coerce(6000)` returns
  the named member instead of a bare int.
- **`CommunityMemberShort` gained field 12 `JoinedAt`** (`joined_at`,
  `Timestamp`) in `messages.json`. Member lists ride along inside
  `CommunityGetExtended`, so this is free wherever that is already being read.

Two more things the rebuild fixed:

- **The `*OneofCase` entries in `enums.json` were repaired, not deleted.** They
  are oneof discriminators, which the decompiler emits with implicit ordinals
  and no explicit values, and the generator wrote nothing usable for them. All
  **11** of them carry the C# enum's real ordinals now — checked against the
  source, e.g. `RootApp.Assets.LinkOneofCase` is `None = 0`, `Url = 1`,
  `Image = 2`, `Video = 3`, `Invalid = 4`, `File = 5`, matching
  `AssetInformation.cs:12-20`. That matters more than it looks:
  `PacketOneofCase` is where `packet_case` comes from, and `rootpy/packets.py`
  takes those numbers verbatim.
- **`message_schemas.json` went from 571 entries to 831, and now includes
  repeated fields** (179 of them, carried as `RepeatedField<T>` types).
  `RawMethod.request_schema` used to return `None` for 25 methods; it returns
  `None` for exactly one now — `root.AppReviewGrpcService/ListMine`, whose
  request is `Empty` and has no descriptor to find. `response_schema` resolves
  for all 259.

`rootpy/data/` is 5 files and **1,120 KB**. **There is no generator in this
repo** — those JSON files are produced by external tooling against a decompiled
client, so regenerating them is a manual task that happens when the client
version moves, and nothing here will tell you it is due. The version drift above
is what that costs when it is skipped.

### Smaller corrections

- **`RawMethod.response_schema`**, mirroring `request_schema` — same direct
  lookup, same unique-suffix fallback, same `None` when the suffix is ambiguous.
  `RawAPI.describe()` puts both in the dict it returns, so inspecting a method
  no longer tells you half of its shape.
- **Four exceptions joined the `RootError` tree**, so the library-wide
  `except RootError` that `docs/errors.md` tells applications to use actually
  catches them: `CallActionError` (`permissions.py`), `CommandError`
  (`commands.py`), `MediaDependencyMissing` (`media_bootstrap.py`) and
  `AlreadyCreatedError` (`accounts.py`). Non-breaking in both directions —
  `CallActionError` and `MediaDependencyMissing` keep `RuntimeError` in the MRO
  for handlers that already catch that, and the other two were plain `Exception`
  subclasses, which `RootError` still is. `CallActionError` is also **exported
  from `rootpy`** now; it was the base its three subclasses shared and the only
  one you could not name.
- **`DetailedMember.is_deleted`** forwards from the profile, like the other
  profile fields it already flattens. It was the one that did not.

### Tests

- `test_account_verification_flow` now asserts `calls["sent"] == []` — no
  resend on the default path. It used to read `["tok-abc"]`, which was the
  defect written down as an expectation.
  `test_account_verification_can_opt_into_a_resend` is the new companion
  covering `resend=True`.
- Two registry assertions were updated for the same reason: `len(ENUMS) == 86`
  (was 85, before `WebRtcBackend`), and
  `test_only_the_wire_descriptor_matches_the_corrected_enum`, which used to
  assert that *neither* `UserOnlineStatus` descriptor matched the corrected
  enum. Exactly one agrees now. The ambiguity is still what makes the lookup
  untrustworthy — the value was only ever the symptom.

### Why 1.29.0 and not 2.0.0

The routing change is breaking, and it is called that above. It is still a
minor bump, on the same reasoning every prior correction in this file used:
these events were being delivered to the wrong handler, and no code that was
correct about what it was receiving changes meaning. 1.19.0 shipped
`create_voice_channel` actually creating a voice channel as a minor for the
same reason. Major is reserved for a surface that was redesigned, not one that
was wrong.

## 1.28.0

**Repository split.** The library is a library again. Three things that used
it — the archivers and the site — moved out to `visjs/`, and the project root
went from 13 top-level files to 8.

| | |
|---|---|
| `ophanim.py`, `accounts.py` + their JSON | → `visjs/ophanim/` |
| `tests/test_archives.py` | → `visjs/ophanim/tests/` |
| `HANDOFF.md`, `CONTINUING.md` | merged into `LLMS.md` §17–21 |
| `CHANGELOG.md` | → `docs/changelog.md` (this file) |
| `allflows.txt`, `hub-capture.txt` | deleted — both were empty scaffolding |

`LLMS.md` §9 stays the compact trap index and now points at §17, which carries
the evidence. The guards that kept HANDOFF.md honest were repointed rather than
dropped, so version, test-file names, fixture names and every named trap are
still asserted — against one file instead of three.

One deliberate deviation from the brief: the 2,600-line changelog was **moved,
not merged**. Folding release history into README.md would bury the guide it is
supposed to be, and deleting it would lose the only record of why several
decisions were made.

The scripts now find the SDK through `visjs/ophanim/_bootstrap.py`:
`ROOTPY_PATH`, then an installed package, then a sibling checkout searched
upwards — with an error that names the two fixes instead of a bare
`ImportError` four frames down.

## 1.27.1 — the site

**New: `discover.py`.** People advertise their servers, and two files this
project already writes are full of invite links to places the account has never
been. It pulls every `https://rootapp.gg/<code>` out of archived message
content and 39,000 about-mes, asks Root what each points at, and reports.

On the stress-test account: **78 codes → 75 communities**, 22 already joined,
**38 new**, 15 dead links.

Joining stays opt-in. Scanning is free and reversible; joining is neither, so
`--join-all` needs `--yes` as well, and the site prints the command rather than
offering a button — a mis-click should not put the account in forty servers.

The regex is the part that quietly goes wrong: `[^\s]*` swallows the `)` that
closes a markdown link, and Root writes links as
`[https://rootapp.gg/x](root://external/0)`, so that is the *common* case, not
an edge one. Ten tests pin it.

**The site runs the archivers.** With the Node server up, four buttons —
`backfill`, `reindex`, `accounts`, `discover` — shell out to the same scripts
you would run by hand; output streams into the page and the data reloads when a
task succeeds. They are a fixed table in `server.js`, spawned with a fixed
argument vector and no shell: a request chooses only which one by name, and an
unknown name is a 404 before anything spawns. One at a time, since they all
write the same files. Under Apache the bar hides itself rather than offering
buttons that cannot work.

**Removed: the Services and Performance tabs.** They listed endpoint constants
and test assertions — information the repository holds in a more useful form,
answering questions nobody had. **Added: Discovery.** Activity and API stay.

## 1.27.0

**`visjs/`** — a Node site over the SDK's surface, its enforced request
budgets, and the live activity index.

```bash
cd visjs && npm run build && npm start     # here: build and preview
scp -r visjs you@server:~/                 # upload the folder
ssh you@server 'cd ~/visjs && ./push.sh'   # there: install into Apache
```

Four tabs: **Activity** (multi-series charts — servers or members, messages/day
or active users/day, raw or 7/30-day smoothed, 30d/90d/1y/all, legend chips to
toggle a line — plus the ranked tables), **API** (34 namespaces, 405 members),
**Services** (59 RPC endpoints), **Performance**.

**Nothing on it is transcribed.** `visjs/tools/extract.py` derives all five
payloads: signatures from `inspect`, endpoints from the module constants that
actually get called, request budgets by AST-parsing the `assert budget.calls ==
N` the live suite enforces, and the activity index copied out of `ophanim.json`
rather than recomputed — one definition of "most active" in the codebase, not
two that drift. `npm run check` exits 1 when the extracted surface no longer
matches what is deployed.

**Build here, install there.** `tools/extract.py` imports `rootpy` and reads
`../ophanim.json`, so the build has to happen on the machine that has them;
`push.sh` runs *on the server*, installs `public/` into `/var/www/html/visjs`,
and refuses with instructions if the generated data is missing rather than
installing a site with empty charts. It never touches the Apache config,
restarts nothing, sudos only when the target is not writable (`--no-sudo`
refuses instead), and will not clear `/`, `/var/www`, `/var/www/html`, `/usr`,
`/etc` or your home directory. `--data-only` refreshes just the JSON.

**No dependencies, no CDN.** It has to run on a LAN-only Apache box where
`npm install` may not have, so the chart is 250 lines of canvas, and Apache
needs no modules, no vhost changes and no `.htaccess`.

Live data has two paths and the page says which it got: `/api/activity` (the
Node server reading `ophanim.json` on request, cached on mtime) or
`data/activity.json` (the snapshot Apache serves).

### The index now carries per-day series

`activity.series` holds `{days: [...], communities: {ref: [[day, messages,
people], ...]}, people: {...}}` — sparse and index-based, 45 KB for 28
communities over 238 days. That is what the charts draw.

### Fixed: the index could go permanently stale

`density` divides by member counts, and membership moves without anyone
posting — so keying the rebuild on the message count alone left density stale,
and **zero forever** on an archive written before member counts were captured,
since the run that learns them adds no messages.

Keying on the inputs was not enough either: one run saved fresh member counts
beside an index built before it had them, and every later run agreed the inputs
matched and kept the stale index. The index now carries a fingerprint of what
it was built from (`activity.inputs`), so it is asked rather than inferred, and
a stored index cannot drift from the directory saved next to it. Four more
tests cover it.

Also fixed, found by driving the page in a browser: the chart measured its
parent's `clientWidth` (which includes padding, so the canvas overflowed its
box) and drew before layout settled, pinning a 20px inline width that the
stylesheet could never override. It now measures the canvas after clearing its
own inline width, and leaves it alone until there is a size to measure.

## 1.26.0

**`ophanim.json` now carries an activity index**, and `--index` prints it.
Communities, channels and people ranked by how alive they are.

```bash
python ophanim.py --index                 # print the rankings
python ophanim.py --index --window 7      # over a different window
python ophanim.py --index --reindex       # recompute rather than read
```

Three numbers per community, because "most active" means three things:

- `per_day` — messages over the calendar span they cover. Rewards sustained
  activity, punishes a place that was busy once.
- `recent_per_day` — the same over the last `--window` days (30 by default).
  What is busy *now*.
- **`density`** — `recent_per_day` per 1,000 members, which is the one worth
  having. On the account this was built against, **Root (31,578 members,
  24 messages/day) ranks 26th** while a 49-member community doing 45/day ranks
  **1st**. Size stops flattering a dead server. The ×1,000 is only for
  readability; the ranking is identical to raw messages/day/member.

Also per row: `recent_users_per_active_day` (the mean number of distinct people
who posted on a day that had any posts), `recent_users`, `days_active`,
`busiest_day`, `first`/`last`.

Two things that needed deciding rather than assuming:

- **"Active users per day" is a mean over active days, not distinct users
  divided by the window.** The second reports "0.77 users/day" for a community
  with 23 regulars — arithmetically true, useless. The first says 8.03.
- **The window is anchored to the newest message in the archive, not to the
  clock.** A file left alone for a month still ranks its contents against each
  other rather than declaring everything dead. `anchor` in the index says which
  day it used.

**Rebuilt on new data, not on every write**, as asked. The index is one pass
over every message — measured at 8.5 µs each, 157 ms for 18,488 — cheap enough
to redo when the archive grows and far too expensive to redo per message. It
runs after a backfill and at any flush that had new messages; `Book.reindex()`
returns immediately when the message count has not moved. Nothing about it
touches the path a message takes.

The `density` denominator is free: member counts ride along on the
`CommunityGetExtended` the backfill already makes. A community with no known
member count gets no `density` rather than a guessed one. The index costs 11%
of the file (516 KB on a 4.5 MB archive) and sits *before* the messages,
since it is what a person opens the file to read.

13 more offline tests in `tests/test_archives.py` cover the arithmetic, the
small-beats-big ranking, the anchoring, and the rebuild-only-on-new-data rule.

## 1.25.0

**`accounts.py`** -- the companion to `ophanim.py`. Where that keeps what was
said, this keeps who is there: every member of every community the account can
reach, with username, uid, about-me, custom status, online status, avatar and
banner URIs, and which communities they are in with what roles.

```bash
python accounts.py            # build or refresh accounts.json
python accounts.py --stats
```

On an account in 28 communities: **39,078 distinct people** across 55,948
memberships, in **15 s and 423 requests**, at 185 bytes each (6.9 MB, 2.1 MB
gzipped).

The split in cost is worth knowing. Member lists are free -- they ride along
inside the `CommunityGetExtended` each community already needs, so 56,000
memberships cost 28 requests and two seconds. Profiles are the other 391,
batched a hundred per `GetExtendedUsersById`.

Choices that needed making:

- **Asset URIs, not URLs.** Root's asset URLs are signed and expire, so a
  stored URL is a broken link by tomorrow. The URI is the durable handle.
- **`EVERYONE` is dropped from roles.** Every member has it; recording it
  56,000 times distinguishes nobody.
- **A refresh merges, it does not replace.** Someone who has left every
  community you can see is kept with a `gone` date. Cleared if they return.
  Fields they have since cleared -- an emptied about-me -- are dropped rather
  than left stale.
- **`accounts.json` was already in `.gitignore`** as a credentials name, and
  `devscripts/_paths.accounts_file()` still points at it, though nothing reads
  it. The name was asked for; the ignore rule now sits with the other archives,
  which is where a file of 39,000 profiles belongs.

Also: both scripts now force UTF-8 on stdout/stderr. Usernames and about-mes
are full of emoji and a Windows console defaults to a codepage that cannot
encode them -- logging one line of chat would have killed a run with a
`UnicodeEncodeError`. The files were always UTF-8; this is only about what gets
printed.

21 offline tests cover both file formats (`tests/test_archives.py`): record
round-trips, absent-means-absent, reference collisions, journal recovery from a
torn last line, the v1 → v2 upgrade, and the merge semantics above.

## 1.24.0

**`ophanim.py`, stress-tested and rebuilt.** Run against an account in 28
communities and 476 channels, the first version was slow in the one place that
matters and three times larger than it needed to be. Both are fixed, and the
file is now more readable, not less.

### Storage: 650 → 249 bytes per message

Same 9,099 messages, same content. Where the bytes went, measured field by
field:

| | v1 | v2 | gzipped |
|---|---|---|---|
| bytes/message | 650 | **249** | **80** |
| 9,099 messages | 5.64 MB | 2.16 MB | 0.69 MB |

A full unlimited backfill of the same account settles at **225 bytes/message**
(18,488 messages, 3.97 MB; 1.19 MB gzipped) -- longer histories carry
proportionally more content and less ceremony.

Three changes, in order of what they were worth:

1. **Names, not ids, with one directory at the top.** Every message carried
   108 bytes of UUID (channel + community + author) for a median 30-byte
   message. Each entity now appears once in `communities` / `channels` /
   `users`, and messages read `"channel": "#general@Root", "user": "someone"`
   — shorter *and* legible. The community is gone from the message entirely:
   it is already in the channel reference. Channel names repeat across
   communities (54 did) and people share display names, so a colliding
   reference gets a short id suffix; the directory maps every reference back.
2. **Absent means absent.** `deleted_at`, `message_type` and `attachments`
   carried a value on **zero** of those 9,099 messages and `pinned_at` on 34.
   Saying "no" 9,099 times cost 10% of the file. Optional fields are written
   only when set.
3. **No bookkeeping in the record.** `source` and `first_seen_at` were 14% of
   the file and answered nothing an archive is asked.

Plus one record per line instead of `indent=2` — another 21% in leading
spaces, and a file that greps, diffs and streams. `--compress` writes
`ophanim.json.gz` alongside.

Existing books upgrade in place on load, offline, in under a tenth of a
second.

### Speed: messages first

| | before | after |
|---|---|---|
| recording one message | a **0.46 s** full re-serialisation, every 5 s, on the event loop | **13.7 µs** (73,000/s) |
| backfill, 476 channels | — | 18,488 messages in **17.1 s** (1,079/s, 749 requests) |
| live message → on disk, 20k-message book | — | **0.26 s** median, 0.64 s worst, 8/8 |

The old design called `save()` from the message handler, which re-serialised
every message ever recorded — fine at a hundred, ruinous at twenty thousand,
and paid by the gateway read loop. The hot path now builds the record, stores
it, appends one line to `ophanim.jsonl` and returns; a background task
compacts that journal into the book on a timer and deletes it once the write
succeeds, so a killed process loses at most one line.

Name resolution stays off the hot path too: an unknown author is *queued*, and
a background resolver batches ids a hundred per request. Records hold raw ids
in memory and render to references at write time, so a name learned an hour in
retroactively fixes every message from that person — including in a book
carried over from an older run.

## 1.23.0

**`ophanim.py`** -- the archivist. Reads back through every channel and
conversation an account can see, then watches, and writes messages and major
events to `ophanim.json`.

```bash
python ophanim.py            # backfill, then watch
python ophanim.py --stats    # what is already in the book
```

Resumable, atomic on write, and de-duplicated by message id, so stopping it
loses nothing and starting it again does not re-fetch what it has. Presence,
typing, profile and status churn are excluded by design; `--include-minor`
keeps everything if you want to see what you are dropping.

### Fixed: `messages.history()` stopped after one page

A real bug, found by ophanim asking for everything and getting 50. The paging
cursor stepped back by `page_size` **seconds** between pages -- on the theory
that the message model carried no timestamp -- so any container whose 50
messages spanned more than 50 seconds put the next cursor back inside the page
just read. The server returned the same rows, the duplicate check found nothing
fresh, and the walk stopped believing it had reached the bottom.

Measured on one real channel: **50 walked, 1024 actually there.** A direct
message conversation went from 50 to 1596.

Now that ids are known to carry their own timestamps the boundary is exact, so
the cursor steps to just before the page's oldest message. Unparseable ids fall
back to the old fixed step, so the walk still terminates. The existing paging
tests missed this because their fake `list` ignored the cursor and their ids
were not Root GUIDs -- both halves of the faulty step were invisible. The new
tests use real time-ordered GUIDs and a cursor-honouring fake, and fail against
the old code.

### Protocol facts this turned up, all in HANDOFF now:

- **Channel creation is not reliably announced.** With every packet recorded
  and nothing filtered, creating a channel in an attached community produced
  *no packet at all*. A rename pushes `CHANNEL_EDITED`; a creation may push
  `COMMUNITY_PERMISSION_UPDATE` or nothing. Messages in the new channel still
  arrive -- the attach is per community -- but knowing the channel *exists*
  means re-listing, so ophanim does that on a timer rather than waiting.
- **Joining a community pushes `COMMUNITY_PERMISSION_UPDATE`, not
  `COMMUNITY_JOINED`.** Keying on the join packet -- the obvious choice, and
  the one this script shipped with for an hour -- leaves the whole community
  unattached and silently unrecorded. Found by running it.
- **Root ids are timestamp GUIDs.** New: `rootpy.root_guid_datetime()` and
  `root_guid_type()`, matching the client's `MessageGuid.ToDateTime()`. Every
  archived record is dated from its id, so a message backfilled from last month
  and one pushed a second ago sort on the same clock.

## 1.22.0

**Membership is not a subscription. `Attach` is.**

Until a community is attached, the hub sends no packet for it at all -- not a
message, not a channel edit -- while DMs, mentions and status changes keep
arriving on the same socket. That is why channel push looked impossible, and
why an account could seem perfectly connected and never react to a post.

```python
await client.community.attach(community_id)     # root.CommunityGrpcService/Attach
await client.community.detach(community_id)
await client.community.detach_many(ids)         # several, one request
```

Measured in one run, same channel, post before and after the attach:

| | plain post | post mentioning the listener |
|---|---|---|
| **not attached** | nothing | `NOTIFICATION` |
| **attached** | `MESSAGE` at **+0.2 s** | `NOTIFICATION` + `MESSAGE` |

The desktop client calls it from `Community.attachAsync`, which runs off its
full-load path, and detaches in `FullyUnload` -- which is exactly why it
receives live traffic only for communities it has actually opened, and why
`initializeReconnectServicesAsync` re-pulls `IsFullyLoaded` communities on
every reconnect.

Attaching is **visible**: the server broadcasts `COMMUNITY_MEMBER_ATTACH`
(5502) and clients show attached members as present in the community.
`detach` undoes it.

### `UnreadReader` is now event-driven

It attaches to every community on start, does one reconciliation sweep to clear
whatever went unread while it was down, and then **makes no requests at all**
until something arrives.

```
3 communities x 3 channels, 9 posts, second account:
  9/9 read      median 0.22 s      worst 0.27 s
  9 requests subscribing, then 1 per message -- the click, not a poll
```

Reproduced across runs by `devscripts/unreadtest.py`, which builds the
communities, waits for the peer to actually see every channel, and reports the
request count alongside the latency -- because a polling reader's cost grows
with time and this one's does not.

Against the previous release's own numbers for the same shape (8/8, median
1.7 s, 102 requests) that is **~9x faster and an order of magnitude cheaper**,
because there is no longer a tick to wait for.

- `interval` now defaults to `0` -- no periodic sweep. Pass a number for a
  safety net; the pacing and backoff options still work when you do.
- New: `attach=True` (subscribe on start), `detach_on_stop=True` (leave as you
  found it), `ReaderStats.attached`.
- The subscription lives with the hub connection, so the reader watches
  `is_connected` and re-attaches on the rising edge. Watching a local flag
  costs nothing.
- `on_unread` now fires for pushed channel messages too, named from cache so
  reporting a channel costs no extra request.

### Corrections to things this project previously documented as fact

Both were artefacts of measuring without the attach:

- *"Ordinary channel messages are not pushed."* They are, once attached.
- *"The account must be activated by a real desktop client session."* No. Every
  negative behind that theory still holds as a negative -- `SetDeviceOnlineStatus`,
  repeating it on a timer, the full startup RPC sequence, the token-embedded
  device id, forced resync, `GetExtended`, `MessageList`, `SetViewTime` -- none
  of them is the trigger. `Attach` is, and it was never being called.

And one measurement that did not survive a controlled re-run:

- *"Posting any message pings every member's socket with an identical
  notification."* Re-measured as plain / mention / plain in a single run
  against a channel the peer can actually see: the plain posts produced **no
  frame at all**. The notification is mention-specific. The earlier reading
  was taken against a channel in a fresh channel group, which a plain member
  cannot see.

`watch_unread` still works and still polls; it is now documented as the option
for watchers that must stay out of the presence list.

## 1.21.0

`UnreadReader` — read everything the moment it goes unread, the way a person
sitting at the client does: the sidebar shows what is bold, they click it, it
clears, they go back to what they were doing.

```python
from rootpy import UnreadReader

async with UnreadReader(client, on_message=handle):
    await asyncio.Event().wait()
```

Measured on 3 communities x 3 channels with a second account posting at random
intervals: **8/8 detected, median 1.7 s, max 2.4 s.**

### It is a different question from `watch_unread`, not a faster one

Head-to-head on identical communities and posts, both at a 2 s interval:
**2.0 s vs 2.1 s median, 102 vs 104 requests.** Effectively identical, and that
is the honest result — polling `CommunityGetExtended` is the floor and both sit
on it.

What differs is the *question*. `watch_unread` asks "did this channel's
activity increase since I started watching?" and never reads
`user_last_viewed_at`: it misses anything already unread when it starts, and
clears nothing. `UnreadReader` implements Root's own state — the predicate is
taken from the decompiled client's `Channel.HasActivity`:

    LastActivityAt > UserLastViewedAt, both non-null, and not a voice channel

The both-non-null half is the one that fails misleadingly: a channel never
opened has no `UserLastViewedAt`, and treating that as unread makes every
channel in every community you have ever joined bold forever.

One `GetExtended` answers for every channel in a community at once, so the cost
is one request per community per tick, not one per channel. The community list
is cached between sweeps (`community_refresh`, default 60 s), which removes
another request per tick. `sweep()` is public and returns what it acted on, so
a pass can be driven and asserted directly — 25 offline tests do exactly that.

Optional idle backoff (`idle_interval`) is **off by default**: backing off makes
the first message after a lull as slow as the backoff, which is the opposite of
the point.

### `SetDeviceOnlineStatus` is now sent on connect

`RootSession.initializeStartupServicesAsync` ends with
`SetDeviceOnlineStatusAsync(UserDeviceOnlineStatus.Active)`, and
`initializeReconnectServicesAsync` repeats it on every reconnect. rootpy never
did, so every connection looked like a device that never came online.

It measurably matters: in a paired probe the DM-delivery control **failed** on
a connection that had not announced and **passed at +0.5 s** on one that had —
same account, same process.

### Channel push works — it needs two conditions, and I had neither

An earlier draft of this entry said no community-scoped packet ever reaches a
rootpy gateway connection. That was **wrong**, and wrong in an instructive way:
every probe behind it used a community created seconds earlier, on an account
with no desktop client running. Both required conditions were violated at once,
so the failure looked total.

Measured matrix, same SDK, same accounts:

| receiver | client signed into it | community | result |
|---|---|---|---|
| biteangelss | yes | existing | **MESSAGE (170) at +0.3 s** |
| biteangelss | yes | seconds old | nothing in 40 s |
| chance777eyyoo | no | existing | nothing in 45 s |
| chance777eyyoo | no, token device id | existing | nothing in 35 s |

1. **The account must be activated by a real client session.** With the desktop
   client signed into an account, *any* rootpy connection for that account gets
   the full stream. Switching which account the client held flipped the result
   both ways in the same window: 0 non-ping frames became 72, and 86 became 0.
2. **The community must not be brand new.** The stream set looks fixed when the
   client connects.

Not the trigger, each measured against traffic on cue:
`SetDeviceOnlineStatus(Active)`, repeating it every 10 s, the whole
`initializeStartupServicesAsync` sequence before or after connect, and the
device id embedded in the token.

**The device id is embedded in the token**, and rootpy invents one.
`ClientToken.Parse` base64url-decodes it: bytes[0:16] is the user id and
bytes[16:32] the device id, plain big-endian UUIDs — verified against a real
token, whose first 16 bytes are exactly that account's user id. Not the
activation trigger, but worth matching.

### Also recorded

- **A plain member cannot see channels the owner creates in a fresh group** —
  only the community's default `Text` channel, until an access rule grants
  `channel_view`. A two-account channel test without that rule silently
  watches nothing, which is how the first version of this one scored 2/6 while
  appearing to work.

## 1.20.0

The live suite had no performance coverage at all. It has ten budgets now, and
adding them found two real inefficiencies on the most ordinary paths in the SDK.

**The budgets count requests, not seconds.** Wall clock on a live API moves
with the network, so a timing assertion is either too loose to catch anything
or too tight to survive a bad afternoon. Request count is deterministic, and it
is what actually costs: a round trip is ~190 ms almost regardless of payload.
Every performance bug this project has found has been a request-count bug.
`devscripts/perfmap.py` prints the same numbers as a ranked table when you want
to go looking rather than guarding.

### `list_communities()` made 10 requests where 1 would do

`CommunityManager.list` called `refresh_communities()` bare and so took that
method's `expand=True` default. The most basic call in the SDK therefore fired
one `ListMine` **plus a full `CommunityGetExtended` per community** — each one
that community's entire member, role and channel dump. Measured on an account
in 9 communities, one of them with 31,555 members: **10 requests and 1.73 s,
against 1 request and 0.19 s. 9.1x.**

Nothing needed that data: `list` returns `Community`, not `CommunityExtended`.
It only warmed a cache the caller may never read — and it made the login cost
table in README.md and LLMS.md ("eager: 3 + one per community", opt-in) false
for this path, because a *lazy* client silently did the eager thing.

The login path already passed `expand=self.expand_communities`. This now agrees
with it, so both modes behave as documented and detail is still fetched and
cached on first access.

### AssetGet wanted *smaller* batches, not bigger

`assets.get`/`resolve` chunked at 10 with 8 in flight. The obvious move was to
batch harder, the way `get_members_detailed` went from 100 to 500. **That is
exactly wrong here**, and the reason is worth recording:

**One URI Root refuses rejects the entire request**, and about **6% of the
asset URIs on real profiles are refused** — stale references that individually
answer `Uris: The specified condition was not met for 'Uris'`. Measured on 120
real URIs: 113 returned data, 7 were refused, and a batch of 9 good + 1 refused
was refused whole. So the bigger the chunk, the likelier it contains a poison
URI, and the whole chunk then degrades to one request per URI.

Measured over 200 real URIs, 3 rounds each, zero rate limiting:

| chunk | conc | seconds | | chunk | conc | seconds |
|---|---|---|---|---|---|---|
| 100 | 16 | 18.56 | | 10 | 8 | 2.62 *(old)* |
| 50 | 8 | 9.55 | | 5 | 8 | 2.26 |
| 25 | 8 | 5.06 | | **5** | **16** | **1.63** *(now)* |

**1.6x faster.** Concurrency past 16 flattens — 24, 32 and 48 measured 1.55,
1.44 and 1.53 s, indistinguishable — so the extra rate-limit exposure buys
nothing. `resolve`'s own defaults were updated in lockstep and pinned by a
test: it is a keyword forwarder, so a stale default there would have silently
overridden the tuned one.

A first ladder reported that AssetGet rejected any batch above 1. That was
wrong — one bad URI in the sample, not a size limit — and it is the third time
this session an instrument has had to be disbelieved before it told the truth.
The ceiling is at least 100 (2.90 ms/uri, against 18.9 at chunk 10).

### Also

- `devscripts/perfmap.py` — ranks operations by requests-made against
  requests-needed. It is what found the `list_communities` N+1.
- An AST sweep for `await` inside loops found 36 candidates and **all but two
  were false positives** — `for x in await self.list()` evaluates the await
  once, before the loop. The two real ones (`clone_community`,
  `watch_unread`) were already in the audit backlog.

## 1.19.2

**LLMS.md's surface listing is generated now.** It was 850 hand-typed lines,
and the docs experiment showed what that costs: 49 signatures had their keyword
arguments elided to `*, ...` — the only rendering those methods had anywhere,
so three readers working from the docs each had to guess at them.

`devscripts/gendocs.py` builds the section from the live package. With no
arguments it diffs and exits 1; `--write` updates; `--print` emits.
`TestDocumentationMatchesThePackage` runs the same check in `pytest -q`, so a
stale listing now fails the build.

What regenerating found, none of which was a writing problem:

- **52 public methods were missing entirely**, including the whole 13-method
  command `Context` API — the object every command handler receives — plus
  `user.dm`, `user.call`, `user.mute`, `directmessage.send/history`, five
  `friend_requests` methods and `notifications.counts_by_container`. A
  hand-written list has no way to know what it omitted, which is why absence
  outlasted every inaccuracy.
- **`Asset` was undocumented** because it lives in `rootpy.services.assets`
  rather than `rootpy.models`, taking `asset.url_for_size` with it.
- One malformed heading (`#### \`### client.admin\``), two subsections
  disagreeing about whether to quote annotations, and one return type quoted
  twice (`-> "'CallSession'"`) — all artifacts of hand-editing a listing that
  had been half-generated at some point in the past.
- **`shared_transport=False`** in the multi-account sketch, still claiming the
  pre-1.18.0 default. Found while deciding which sections were safe to
  generate.

Deliberately *not* generated: the mental model, protocol traps, worked examples
and performance notes — the half a machine cannot check, and the half that
measurably worked (the traps section scored 3/3 with the agents). The curated
import lists at the end of section 14 are spliced back verbatim for the same
reason: someone chose which imports matter and annotated them.

Two bugs in the generator itself, both caught before it was trusted:

- A first version dropped the `*` marker, making keyword-only parameters look
  positional, and stripped quotes globally so `picture_hex: str = ''` rendered
  as `= `. Rebuilt from `inspect.Parameter` fields instead of text editing.
- Its output depended on **import order**: scanning the live `rootpy` namespace
  meant a test that imported more of the package added six entries, so the
  staleness check passed alone and failed in a full suite run. It walks every
  submodule explicitly now. A generator whose output depends on what ran before
  it cannot gate anything.

Also pinned: `test_every_public_client_verb_is_listed`, because accuracy was
never the failure mode here — absence was.

## 1.19.1

Findings from a documentation-sufficiency experiment: three agents were given
only the nine markdown files and asked to write a working script, citing a
`file:line` for every call they made. Anything uncited counted as a guess even
when correct, so the output was a list of what independent readers *had* to
invent. Where all three invented the same thing, that is a documentation defect
rather than model variance.

Three of the four fixes below are things no amount of proofreading would have
surfaced, because they are invisible to anyone who already knows the library.

- **49 signatures hid their keyword arguments behind `*, ...`.** All three
  agents named this among their top gaps, and for those methods the elided line
  was the *only* rendering anywhere — so the keywords were genuinely
  unobtainable from the docs. Every one is now written out in full, generated
  from `inspect` rather than by hand. `permissions.create_rule`/`edit_rule`
  gained the 21 `ChannelOverlay` field names their `**permissions` accepts.

  The docs' own answer to this was `client.explain()` — "prefer these over
  guessing at a request shape" — which requires the installed library. One
  agent noticed that the prescribed cure for the largest gap was itself out of
  reach of a docs-only reader.

- **`ChannelGroup` had no field list.** Ten other objects had one. Creating a
  group is mandatory and `create_channel` needs its `id`, so all three agents
  assumed `.id` existed without being able to cite it. Documented.

- **`README.md` demonstrated a call that cannot work.**
  `create_role(community_id, "New Role")` — role names reject spaces. Laddered
  live to settle a flat contradiction one agent found (LLMS.md said role names
  reject spaces; the README showed one containing a space). LLMS.md was right.

  The full rule, measured, is worth stating because two of the three are exact
  opposites: **channel groups** take letters, digits and apostrophes in at most
  two words but reject hyphens; **roles and channels** take letters, digits and
  hyphens but reject spaces. `test area` and `new-channel` are both valid;
  `test-area` and `new channel` are both refused. Length constrains none of
  them.

- **`client.list_messages` silently changed its callee's default.** A thin
  forwarder whose docstring says "see MessageService.list", defaulting
  `direction` to `"older"` against that method's `"both"`. Three agents each
  found three renderings (`messages.list` both, `client.list_messages` older,
  `channel.history` both) and could not tell which applied. Not a doc bug — the
  docs described each method accurately — but an API inconsistency that no
  amount of documentation would have fixed. Now `"both"` everywhere, pinned by
  a test.

  `defaultcheck.py` does not cover this shape and deliberately still does not:
  generalising it from "falsy default defeats a `None` sentinel" to "any
  differing default" flags nine legitimate cases (`mute(muted=True)`,
  `from_tokens(gateway=False)`, `history(limit=200)` …) to catch this one.

  Its stale docstring — still claiming Root rejects any request carrying
  `Limit` — was corrected to the measured 10–50 window.

**What the experiment says works.** Unprompted and unanimously, all three
agents passed a valid `picture_hex`, respected the two-word channel-group rule,
kept `limit` inside 10–50, and polled instead of sleeping. The explicit
"Protocol traps" section scored 3/3, including both rules added the previous
day. The failures clustered not in what the API *does* but in the shape of its
arguments and returns.

## 1.19.0

An audit release. Nothing new was designed; the existing surface was tested
harder, measured, and corrected where it disagreed with the server or with
itself. 1746 offline tests (was 1683), 332 live (was 314), coverage unchanged
at 209/231 methods. Both suites pass.

Every performance number below was measured on this build against the live API,
and every bug below is pinned by a test that was verified to fail against the
code as it was.

### The live suite found one bug, and it was in an offline helper

`TestNotificationLifecycle::test_delete_all_empties_the_peer_inbox` had been
failing every run and burning its full 20-second window doing it. `delete_all`
was fine. **`responses.items()` reported an empty list as one element.**

Root omits an empty repeated field from a response entirely, so an empty
`NotificationListResponse` arrives as a bare envelope with no `notifications`
attribute. `items()` fell back to "the payload must already be the sequence"
and handed the envelope to `as_sequence`, which wraps a non-sequence in a
list — so zero rows read back as `[<envelope>]` and `len(...) == 1`. An empty
inbox could never compare equal to zero. The fallback now applies only to a
payload that really is a `list`/`tuple`.

The existing pin covered three shapes and not the one that mattered; the empty
envelope is pinned now in all three spellings it arrives in. Fixing it also
took ~20 s off every live run.

### Broadcast is tested against a real server now

`MultiClientHost.broadcast` shipped in 1.18.0 with offline coverage only: one
test against three fake clients, plus a guard pinning *where* it lives.
`tests/test_live_broadcast.py` adds 18 live tests over the things a fan-out
actually gets wrong — and they pass.

- **Each account acts as itself.** The question a shared transport raises is
  whether a request issued over it carries the right account's credentials, and
  the only proof is two distinct identities coming back from a real
  `UserGetSelf` per account. (The first draft of this test called `whoami()`,
  which returns the *cached* login identity and proves nothing — it reported
  "one call 0 ms, broadcast 0 ms, 26.76x" and passed on a floor in the
  assertion. `refresh=True` now, deliberately.)
- **The multiplexing claim holds:** a two-account broadcast measured **190 ms
  against a single call's 172 ms — 1.10x**.
- **A repeat join does not answer `ALREADY_EXISTS`.** Root answers
  `UNAUTHENTICATED (16)` for an account that is already a member *and* for the
  community's owner; a bad code answers `NOT_FOUND (5)`. The docstrings,
  README, LLMS.md and docs/api.md all claimed `ALREADY_EXISTS` and are
  corrected. Match on the exception type.
- Also covered: shared vs isolated transports, `only=`, unknown account names,
  per-account timeouts, `concurrency=1`, and that neither client owns a shared
  transport (the regression that once let the first `close()` kill every other
  account).

### Bugs

- **`get_members_detailed` was 17.8x slower than it needed to be.** It fetched
  profiles 100 at a time and `await`ed each batch in a `for` loop. Root accepts
  far larger batches — laddered live, a single request returned 4,000 ids — and
  a round trip costs ~190 ms almost regardless of payload, so per-id cost falls
  from 1.96 ms at 100 to 0.31 ms at 4,000. Now 500 per request, 8 in flight.
  On a real 31,520-member community: **316 requests and 63.7 s → 64 requests
  and 3.6 s**, identical results. `concurrency=1` restores the old behaviour.

- **`AuthClient.signup` could never raise its typed conflict exceptions.** The
  guard was `exc.status == "6"`; `exc.status` is a `GrpcStatus` IntEnum, so the
  comparison was never true and the whole mapping was dead code.
  `UsernameAlreadyExists` / `EmailAlreadyExists` are exported from `rootpy` and
  caught by `AccountCreator`, and neither could ever be raised.

- **`Community.create_voice_channel` silently created a *text* channel.** It
  sent `channel_type=1`, which is `TEXT`; `VOICE` is 4. `create_text_channel`
  sent 0 = `Unspecified`, which Root rejects. This is the exact bug
  `CommunityManager` records fixing at `object_api.py:82-92`, left unfixed on
  the model surface. Both take their value from `rootpy.enums.ChannelType` now.

- **`create_channel` defaulted `channel_type` to `Unspecified`** on both the
  admin service and the manager — a value this codebase documents Root
  rejecting with a `PredicateValidator`, so the three-argument call could not
  succeed against any server. Defaults to `TEXT`, which is what README.md and
  docs/api.md already showed callers passing.

- **`MemberManager.list` returned the raw envelope** while its sibling
  `list_all` returned the rows, from the identical response message
  (`CommunityMemberExtendedListResponse`). Iterating it yielded the field name
  `'community_members'`. It was simply missing from the parametrized guard that
  covers every other list method; it is in the table now.

- **`edit_role` turned a transient read failure into a destructive replace.**
  `CommunityRoleEdit` is a replace, so the method reads the role first to carry
  forward what the caller did not supply — and then wrapped that read in
  `except Exception: current = None`, so a 429, a `PERMISSION_DENIED` or a
  network blip silently converted `edit_role(cid, rid, name="x")` into "rename
  this role and wipe its colour and both permission sets". The read now
  propagates, matching `edit_community`, and a role that is genuinely absent
  raises rather than sending the replace.

- **`client.timings()` understated its totals without bound.** `EndpointStats`
  keeps a 1000-entry sample so the median stays cheap, but
  `total_roundtrip_ms` summed *the sample* rather than the calls, so past 1000
  calls to one endpoint the total converged toward `1000 × mean` however many
  requests were made. `report()` ranks endpoints by that number, so the ranking
  inverted too — a hot endpoint could be shown below a quiet one it dominated.
  Exact accumulators now; the sample is still what the median comes from.

- **A `429` on the final attempt threw away `Retry-After`.** The cooldown was
  recorded inside the retry branch, so the last 429 — the one that gives up —
  registered no cooldown and no `rate_limited` stat, exactly when a fan-out is
  hitting the limit hardest. Every other coroutine on the transport then sailed
  through `_respect_rate_limit` into a limiter that had just said "wait".

- **`httpx.RemoteProtocolError` was never retried.** It is neither a
  `NetworkError` nor a `TimeoutException`, so the HTTP/2 GOAWAY that routinely
  ends a keep-alive connection — common on a long-lived client and on a
  `MultiClientHost` sharing one pool — escaped the retry block entirely, and
  bypassed the stats too, so it did not even appear in the timing report.
  `LocalProtocolError` is still not retried: that one is our bug.

- **`play_audio` wrote a file to the user's Desktop and printed to stdout on
  every call.** Leftover scaffolding from working out Root's SDP layout. It
  dropped an unrequested file in someone's home directory, hard-failed on any
  host without a Desktop folder (`write_text` does not `mkdir`), and corrupted
  the stdout of any program piping a call's output. It logs at debug now, and a
  guard bans any shipped module from choosing its own path under `Path.home()`.

### Performance

- **`resolve_enum` was 68% of structured decode CPU.** It hits a dict for a
  fully-qualified name and falls through to scanning all of `ENUMS.items()`
  otherwise — which is what a schema's bare field type looks like — once per
  enum field, on both encode and decode, on the event loop. Measured **0.40 µs
  qualified against 28.94 µs bare**. Memoised (`ENUMS` is a read-only
  `Mapping`, so it is a pure function): **28.94 µs → 0.18 µs, 161x**, verified
  identical across 2,696 name/hint combinations.

- **The gateway built two throwaway JSON trees per frame.**
  `data["notification"]` and `data["packet_container"]` are exploratory
  protobuf-to-JSON dumps that nothing in the library, tests, devscripts or
  examples reads — and `packet_container`'s tree is a strict *subtree* of
  `notification`'s, recomputed from scratch. On a 556-byte frame that was
  **107.7 µs of 169.1 µs, 64%**, paid on every frame including keepalive pings.
  Both keys still exist and now carry `None` unless
  `client.gateway.decode_debug_trees = True`: **169.5 µs → 58.2 µs, 2.9x.**

### Protocol

- **`MessageList`'s `Limit` is optional and must be 10–50 inclusive.** LLMS.md
  said "must not include `Limit`; sending it gets the request rejected", which
  is wrong in both directions — and it contradicted `messages.history()`, which
  sends `Limit=page_size` on every page and works. Laddered live, and Root
  answered in its own words via the structured error payload:

      Limit: Must be between 10 and 50 [InclusiveBetweenValidator]

  Omitted → 50 returned; 1, 2, 5, 9 → `INVALID_ARGUMENT`; 10–50 → exactly that
  many; 51 and up → `INVALID_ARGUMENT`. `messages.list` and `messages.history`
  range-check it locally now, so a bad `page_size` fails once, up front,
  instead of identically on every page.

### Documentation

The docs were checked against the code rather than reread. What was wrong:

- **`docs/package/rootpy-internals.md` documented an API that does not exist.**
  About 320 of its 442 lines described a `RootClientPool` — `pool.command()`,
  `pool.run(console=True)`, `pool.validate_clients()`, `pool.startup_counts`,
  plus `client.send_message()` and `pip install rootpy` (the package is
  `rootpy-client`). None of it has ever shipped. Rewritten against the real
  package: the layering, lazy registries, response shapes, errors, the gateway,
  `MultiClientHost`, and where the time goes.
- `docs/errors.md`'s "Pool errors" section had the same problem; it is written
  against `broadcast`/`Outcome` now.
- **Two byte-identical duplicate docs removed** —
  `docs/package/rootpy-package-notes.md` and
  `docs/package/direct-messages-internals.md`. Neither was linked from
  anywhere, and both would have drifted from the originals.
- README's Quick start told users to run `apitour.py`, which does not exist —
  the first command a new reader types. `docs/api.md` repeated it. Both point
  at `example.py --list` now.
- README documented `provision.py` as the lead "included script"; it is not in
  the repo and HANDOFF.md says it is deliberately out of scope.
- README's "Project layout" claimed **97** regression tests while its own
  Testing section said 1683.
- LLMS.md claimed `get_profiles` costs "one request per 100 ids" — it is one
  request for any number, and the wrong figure invites hand-rolled chunking
  loops that turn one request into N. It also contradicted the same file's own
  prose four lines later.
- `message.reply()`, `send_to_channel()` and `reply_with()` were annotated and
  documented as returning `Message`; they return `MessageSendResult`. Type
  checkers agreed with `(await m.reply("x")).author` right up until it raised.
- `friend_groups.move`'s `before_group_id` was documented optional; it is
  required, and the docstring already said so.
- Test counts, per-call timings and the coverage table were re-derived from an
  actual run rather than carried forward.

Scalability and discoverability, without widening the account scope. Everything
here is additive except one default flip, and that flip was earned by a
measurement rather than assumed. 1683 offline tests (was 1663); live coverage
unchanged at 314 tests and 209/231 methods.

### Running several accounts got cheaper and safer to share

- **`MultiClientHost.broadcast(action)`** runs one action as every registered
  account, concurrently, and returns one `Outcome` per account. It **never
  raises**: partial success ("3 of 5 joined") is the normal case for a fan-out,
  so an account answering `ALREADY_EXISTS` or `PERMISSION_DENIED` is a result
  you inspect, not an exception that costs you the others. `only=`,
  `concurrency=` and `timeout=` bound it; `host.join(code)` is the named
  shorthand, and `MultiClientHost.from_tokens([...])` builds a host for a
  handful of bare tokens.

- **`shared_transport` now defaults on.** One transport means one TLS + HTTP/2
  handshake for the whole host instead of one per account — measured at ~831 ms
  per connection, so for a three-account host it is the difference between ~1.1 s
  and ~3 s to first usefulness, and HTTP/2 multiplexes the accounts' requests
  over it with no measurable contention (85 vs 92 ms/call).

  It used to default *off* for one concrete reason: the transport's rate-limit
  cooldown table was shared along with the connection pool, so one account's
  `429` paused every account on it. That reason is gone — cooldowns are keyed
  **per account** now (`GrpcWebTransport._account_scope` hashes the caller's
  authorization header; nothing reversible is retained), so one account backing
  off no longer stalls the rest. `shared_transport=False` still gives each
  account its own pool for hard socket isolation.

  The two offline tests that pinned the old behaviour were rewritten to assert
  the new contract — that A's cooldown holds A and not B on a shared transport,
  and that the host shares one transport by default while keeping three
  independent clients — each verified by breaking the code validly and watching
  the assertion fail. A scope guard also pins that no mass-messaging helper
  (`spam`, `bulk_message`, `sweep_all`, a stray `broadcast`) sits on the
  single-account client surface: the one sanctioned fan-out lives on the host.

### Discovery: see what a call sends without sending it

- **`client.explain(target)`** gives, offline, the path to the wire shape that
  only `client.high.<service>.describe()` used to reach. Spell a name the way
  you would — a manager (`community_files`), a wire service (`file`), or a
  `service.method` in either — and it shows the Python signature and the wire
  fields together. The manager-to-wire link is read from the code (which
  `high.<service>.<method>` calls a method makes), not a hand-kept table, so it
  cannot drift.

- **`client.preview(target, **kwargs)`** encodes a request with the real codec
  and shows the framed bytes that would go on the wire — without sending them.
  Because encoding validates field names and values, an unknown keyword or a
  malformed GUID raises *here*, offline: a preview that encodes is a call you
  know is well-formed, for the price of zero round trips. Both return an object
  that prints itself and carries `.as_dict()`.

  (Building these surfaced a real cross-platform bug and fixed it: the rendered
  output was ASCII-only from the start once a `cp1252` Windows console was found
  to raise `UnicodeEncodeError` on an em-dash. Two tests pin it.)

### Internal: one place to read a response

- **`rootpy/responses.py`** is the single home for reading a field off whatever
  shape a structured payload arrived in — `field`, `first`, `as_sequence`,
  `items`, `shape`. `features.py` had two `_field` definitions and the live
  tests had their own `_get`/`_shape` plus inlined `getattr(raw, "…", raw)`
  envelope reads; all of them fold onto this now, and a guard pins the
  de-duplication so a third copy cannot quietly reappear.

## 1.17.0

First release where the live suite was **run** during development rather than
handed over to be run. That changed what the work looks like: a probe reports,
the candidate list narrows, the probe becomes an assertion, all in one sitting.
Five runs, each narrowing the last; the suite ends green at 314 live tests and
209/231 service methods (90%), up from 232 and 150/230. Round trip re-measured
at **~187 ms** over 851 calls (was 213 ms over 496).

Nearly everything below came out of that loop, and none of it could have come
out of a single batched run — three of the findings were reached only by
*disbelieving an instrument* that had reported a confident, wrong answer.

### Protocol findings

- **Blocking someone deletes the friendship, and unblocking does not restore
  it.** Measured in both directions. `block` then `unblock` is not a no-op.
  DMs and calls both depend on the friendship, so the damage surfaces later
  and elsewhere as `PERMISSION_DENIED`.

  This had been happening on every live run since the blocking tests were
  written — nothing after them looked at the friendship, and the next run's
  fixture quietly re-established it. `BlockManager.block` documents it now and
  `TestBlocking` restores in an **autouse** fixture, because the first fix
  patched only the test that asserts the destruction and left the two ordinary
  block/unblock tests still tearing it down.

- **Mentions are markdown links, and they do not notify.** Two separate
  findings that arrived together.

  The syntax is `[@name](root://user/<id>)`, not `<@id>` —
  `rootpy.commands.USER_MENTION_RE` has said so since it was written.

  The notification behaviour is the other half: no spelling produces a
  notification-list entry, and the socket event that `TestMentionsArePushed`
  waited on fires identically for a message containing no mention at all
  (`
` — no user id, no type, no content). So that
  test was passing on an activity ping. It asserts both halves now, and the
  format question is a strict-false `xfail` with a control test, because *why*
  is still unknown.

- **Root content-addresses assets.** Identical bytes give the identical asset
  id, URI and derivatives. Any test asserting "the image changed" must upload
  fresh bytes; `conftest.make_png()` generates a unique valid PNG per call
  without adding an image dependency.

- **`FileEdit` re-appends the extension.** Send the stem `renamedabcd`, the
  record reads `renamedabcd.png`. Which explains both halves of the name rule:
  why a name with a dot is refused, and why comparing the stored name to the
  stem timed out after the edit had actually succeeded.

- **Re-requesting a friendship after a decline is allowed.** Three
  request/decline rounds in a row all went through.


- **`FileEdit` takes the stem, not the filename.** `Name` is validated with a
  `RegularExpressionValidator` that refuses **any dot** and the space;
  hyphens, underscores, digits and uppercase are all fine. Measured one
  variable at a time over ten candidates.

  The trap is the asymmetry with `FileCreate`, which stores the uploaded
  filename complete with its extension — so reading a name off a record and
  handing it back to `FileEdit` fails, which is the obvious thing to do.

- **A community file's bytes cannot be retrieved.** Two routes, both closed.
  `FileGrpcService/Download` answers `UNIMPLEMENTED (12)` for every argument
  shape — asset id from the record, file id in its place, field omitted. And
  the asset service does not substitute: a file's asset resolves only under
  the `"file"` kind, to an **unsigned** `static.rootapp.com` URL that is
  refused with 403, while the `"image"` kind that yields signed working
  `imagedelivery.net` URLs for avatars and emoji does not resolve for a file
  at all. Listing, renaming, moving and deleting work; the content does not
  come back. Pinned by tests that fail if Root ships either half.

- **Asset URIs decoded.** `root://asset/<b64url(guid16 + 0x0A <len> kind)>`,
  confirmed by reconstructing two URIs Root itself produced, byte for byte.

### Fixed — in the library

- **`Channel.mention` produced a string the library's own parser refuses.** It
  emitted `<#{id}>`, Discord-style, while `parse_channel_mention` matches
  `[#name](root://channel/<id>)`. So the two halves of the package disagreed,
  and there was no user-mention builder at all — which is why the live suite
  invented `<@id>` and, when nothing arrived, *skipped*.

  `Channel.mention` is correct now, `User.mention` exists, and
  `models.build_user_mention()` / `build_channel_mention()` are the shared
  builders. Round-tripping producer through parser is asserted offline, and a
  guard fails if any live test hand-rolls a mention again.

### Added

- **`AssetService.uri_for_id(asset_id, kind="image")`** — several records
  carry a bare `asset_id` and every asset method takes a URI, so there was no
  way across. `kind` is not cosmetic: it selects the derivative, and the
  derivatives live on different backends with different signing.

### Coverage

`community_files` 9/9, `directories` 6/6, `assets` 8/8, `members` 10/10,
`permissions` 8/8, `notifications` 7/7, `users` 9/9, `dm` 5/5, `voice_admin`
3/3, `search` 2/2, `logs` 2/2, `moderation` 5/5.

`friends` 10/11 and `friend_requests` 9/10 — the request/response surface was
previously unreachable, because every one of those methods answers a *pending*
request and the two test accounts are permanently friends.
`TestFriendshipCycle` unfriends the pair and walks remove → decline → reject →
accept → remove → accept, verifying restoration in a `finally`. It is the last
test in the last live module, so nothing downstream depends on the friendship
while it runs.

### Fixed — in the tests, which is where these bugs were

- **The profile-picture test left a real account wearing a 1×1 test PNG.** The
  set succeeded, the *restore* hit Root's shared profile rate limit, and the
  original bytes had already been discarded. Guarding the set against
  `RESOURCE_EXHAUSTED` was not enough — the dangerous call is the one that
  puts things back. Anything mutating durable state now writes the original to
  disk **before** changing it, retries the restore through the rate limit, and
  verifies it landed, naming the saved file if it could not.

- **The mention-format probe measured the wrong thing and produced a false
  negative.** It reported that none of four spellings notify — which would
  have meant "only mentions and DMs are pushed" was wrong — by counting
  `len(notifications.list())`. That list is paginated, so a new arrival does
  not change the length once the page is full. The same run's
  `TestMentionsArePushed`, which waits on a pushed event, passed on `<@id>`:
  two instruments disagreeing, and the count was the wrong one. It compares
  notification id sets now.

- **`moderation.invite_user` was being tested against an existing member**, so
  `already_exists` was the only answer it could ever give and the call was
  never really exercised. The peer is kicked first now, and the
  already-exists case is kept as its own assertion.

## 1.16.0

Live coverage 150/230 -> 200/230. Two SDK bugs, both found **offline**, and
four new offline checks so that class of bug cannot come back.

### Fixed

- **`user_settings.set_community_invite_requirement`,
  `set_dm_invite_requirement` and `set_friend_invite_requirement` could never
  have worked.** All three sent `is_required` at requests whose field is
  `IsEmailVerified`, and `encode_message` raises
  `TypeError: <Request> has no field 'is_required'` *before* building the
  request — so every call raised, from the beginning, on every account.

  The parameter is now `email_verified`, which is also what it means: these
  carry two independent conditions, ANDed. `connection` is how closely someone
  must already be linked to you (`ANY` / `CONNECTED` / `FRIEND` / `NONE`);
  `email_verified` additionally requires them to have a verified address. The
  old name said "is this rule required", which is not a thing the request can
  express.

  Worth noting *how* this survived. There were already offline tests pinning
  these methods' signature and pinning the connection enums against the
  descriptor, and a deliberate decision not to exercise them live because the
  setting cannot be read back and getting it wrong would break every DM on the
  account. All of that was reasonable and none of it could see the bug,
  because nothing compared the keyword to the schema.

- **`client.community.edit_role(cid, rid, name="x")` could not be called.**
  `CommunityRoleEdit` is a replace, so `CommunityAdminService.edit_role` reads
  the current role and carries forward anything the caller omitted — keyed on
  `color_hex is None`. `CommunityManager.edit_role`, the spelling almost every
  caller uses, declared `color_hex: str = ""` and forwarded it verbatim. `""`
  is not `None`, the carry-forward never fired, and
  `normalize_hex_colour("")` raised `ValueError` before the request was built.
  `client.admin.edit_role` with identical arguments worked.

  Fourth time the replace semantics have bitten, and the first time it was the
  wrapper rather than the request.

- **`test_sweep_surfaces_first_new_message` was wall-clock flaky** — one
  failure in three full offline runs, from a fixed `asyncio.sleep(0.35)`
  against a 0.15 s poll. It polls for the result now. A flaky gate in front of
  a live run is worse than a slow one.

### Added — offline checks, each verified against a real break

- **`TestEveryHighCallSiteSendsRealFields`** / `devscripts/kwargcheck.py` —
  every `*.high.<alias>.<method>(...)` call site in the package, checked
  against the request schema. Handles the `kwargs = {...}; kwargs["x"] = ...;
  f(**kwargs)` shape as well as literal keywords. 92 sites, 0 broken, 0
  unresolved.
- **`TestLiveSuiteCallsBind`** / `devscripts/bindcheck.py` — every
  `client.*` call in the live suite bound against the real signature. The
  await-guard checked the *name*; this checks the *arguments*, which was the
  remaining way to discover a `TypeError` part-way through a live run. For the
  `**kwargs` forwarders the keywords are traced one hop to the
  `high.<alias>.<method>` they call and checked against the wire schema
  instead. 527 calls checked, 24 that neither half can verify, listed.
- **`TestManagerDefaultsDoNotDefeatTheSentinel`** /
  `devscripts/defaultcheck.py` — every `f(x=x)` forward where the caller's
  default is falsy and the callee's is `None`. 141 forwards compared.
- **`TestEverySourceFileCompiles`** — `ast.parse` succeeding is not the same
  as the file being valid. `lambda: len(await thing())` parses cleanly and
  fails at `compile()`; `_parsed_sources` *skips* what it cannot parse, so a
  file with that mistake is silently exempted from every AST guard while the
  suite still reports green. This happened while writing the notification
  tests. `compile()` is strictly stronger than `ast.parse`, so it subsumes it.

- **`rootpy.enums.UserDirectMessageInviteConnection`,
  `UserCommunityInviteConnection`, `UserFriendshipInviteConnection`** — the
  three setters above had no named constants, so the only way to call them was
  a raw int. Pinned against the descriptor.

### Added — live coverage

`test_live_files.py` is new; the rest are additions to existing modules.

- **`community_files` 3/9 -> 9/9, `directories` 3/6 -> 6/6, `assets` 3/7 ->
  7/7** — get, edit, move, download and both searches, plus `upload_bytes` /
  `upload_file` / `url_for` / `download`.
- **`search` 0/2 -> 2/2.** It was written off as "undocumented kwargs"; the
  fields were available offline the whole time from
  `StructuredMethod.signature()`.
- **`members` 5/10 -> 10/10, `community` 30/39 -> 38/39, `moderation` 4/5 ->
  5/5** — the manager spellings of ban/kick/unban and the bulk forms, the
  `move_*` trio, `get_members`, `edit_role`, and `moderation.invite_user`.
- **`permissions` 4/8 -> 8/8** — the four constructors, exercised by using
  what they build in requests Root has to accept.
- **`users` 7/9 -> 9/9** — `set_profile_picture` and `set_banner`, each
  restoring the previous image *byte for byte* by downloading it first. That
  is why these are tested and the invite gate is not: this can be put back.
- **`dm` 2/5 -> 5/5, `notifications` 4/7 -> 7/7, `voice_admin` 1/3 -> 3/3,
  `community_apps` 0/8 -> 2/8, `logs` 1/2 -> 2/2, `friend_requests` 4/10 ->
  7/10, `friends` 6/11 -> 7/11.**

### Added — measurement rather than assertion

- **`conftest.ladder()`** — generalises the candidate-walk that settled
  `picture_hex` and the channel-group name rule. Takes
  `(description, coro_factory)` pairs, reports which shape the server accepted
  and what it said about the others, and puts the whole report in the failure
  message when everything is refused. Used for the five things here with no
  documented server-side rule: `DirectoryMove`'s parent fields, `FileMove`'s
  directory fields, `FileDownload`'s asset id, `FileSearch`'s cursor and
  `CommunityAppList`'s app type.
- **`TestMentionFormat`** — "only mentions and DMs are pushed" is load-bearing
  and the mention *spelling* had never been confirmed; the existing test sends
  `<@id>` and skips when nothing arrives, so a world where mentions do not
  push looks exactly like a world where they do. Four spellings, reported.
- **`devscripts/covermap.py`** — the 150/230 figure was counted by hand. This
  computes it, and more usefully names what is missing. `--strict` gives a
  lower bound. It deliberately under-counts one helper-mediated case rather
  than over-count: crediting every definition the walk lands on was tried and
  inflated the total by five, and an instrument that flatters is worse than
  one that is wrong in a documented direction.

### Changed

- The `png` fixture and its 1x1 PNG moved from `test_live_services.py` to
  `conftest.py` — four modules need image bytes now.
- The await-contract guard understands `lambda: client.thing(...)` handed to
  `eventually()` or `ladder()`: both call the thunk and await only what is
  awaitable, so neither spelling is a mistake there. A lambda that *nothing*
  consumes is still flagged, and both original failure modes were re-verified
  against a valid break.

## 1.15.1

### Documentation, rewritten against measured facts

Every figure below was measured at the time of writing, not recalled.

- **HANDOFF.md rewritten.** Version, test counts, the per-service coverage
  table (150/230), what is verified and what is explicitly not, all fourteen
  protocol traps with the evidence for each, measured performance
  (~213 ms/call over 496 calls), and an honest section on what the soak does
  and does not prove.
- **LLMS.md** — client-verb listing regenerated from the live package, now
  marking async generators separately from coroutines and properties. New
  testing section covering the markers, the timing flag, the soak, and the
  conventions the suite enforces on itself.
- **README.md** — testing section updated from "51 tests" to the current
  counts, with the two-account rationale stated: one account can send a DM and
  see it in its own history, but only a second proves it was delivered.
- **devscripts/README.md** — documents `soak.py`, which it predated.
- **CONTINUING.md** — new. How the work has been going, what to do next, and
  the failure modes that cost live runs, so the next session does not
  rediscover them.

### Added

- `TestHandoffIsAccurate` — the version matches `pyproject`, every named test
  file exists, every named fixture exists, all documented commands are real,
  and the protocol traps are still present. HANDOFF has carried stale claims
  before ("~330 ms per round trip" when the measured figure was 202; "DMs and
  friends verified" for a surface that had never executed), and a wrong
  handoff is worse than a missing one because it gets believed.

### Fixed

- `~330 ms per round trip` appeared in README and LLMS as an absolute. It was
  measured on a slower connection; the current figure is ~213 ms. Both now say
  so and mark the per-mode tables as ratios rather than absolutes.

## 1.15.0

Stopping the loop rather than iterating on it again.

### What was going wrong

Four live runs were spent on "a memory leak in the SDK". Each time the finding
was the measuring tool: a listener stacked on every reconnect, an RSS reader
that built a `ctypes.Structure` class per sample, a zero that meant
"unmeasured", each discovered only after the next run had been spent.

The structural error was not any of those bugs. It was that **there was never
a control.** A counter that drifts on its own makes every reading it produces
uninterpretable, and its drift cannot be established from what it says about
something else. I also validated each fix against a live run, so the
instrument and the subject changed together every time.

### What actually happens, measured offline in a second

    200 client construct+close cycles     +0 objects
    100 transport build+close cycles      +0 objects  (real httpx, real SSL)
    control (no work at all)              +0 objects

The client lifecycle and the transport retain nothing. Most of a reconnect is
`close()` + `login_token()` + `connect()`, and only the last two need the
network -- so the part most likely to leak was answerable offline at any point
in the last four rounds, and I did not look.

The arithmetic also closes: ~27 classes per reconnect block at ~40 tracked
objects each is ~12,000, against the 12,870 reported. The class-per-sample was
essentially the whole of it.

### Added -- the tool proves itself first

- `calibrate()` runs the full sampling loop against a null client and measures
  what the sampling alone costs. Current build: **0 objects over 500 idle
  rounds**. Runs offline, in about a second.
- `--selftest` does exactly that and exits, touching nothing. Exit 1 if the
  instrument is not flat.
- Every run now calibrates before measuring, subtracts the instrument's own
  per-round cost from any retention figure, and **refuses to present a
  retention finding as trustworthy** when the instrument itself drifts.
- `TestClientLifecycleRetainsNothing` makes the offline measurements
  permanent, including a control assertion -- because without one, the rest
  of the file is measuring nothing.

## 1.14.2

The type histogram named the leak on its first run, and it was the soak again.

    types that grew: tuple +4369, getset_descriptor +1268, dict +1157, ...

`getset_descriptor` is **class-creation debris**, and `rss_mb()` was the only
place in the project that builds a class at runtime: it defined its
`ctypes.Structure` subclass *inside the function*, so a 300-round stress run
created 300 classes. On Windows only -- the Linux path reads `/proc` and never
enters that branch -- which is why the offline suite never saw it and why it
took a histogram to find.

### Fixed

- **`rss_mb()` allocates nothing per call.** The Structure, the DLL handles
  and the function lookup are all built once at import. 500 calls now retain
  fewer than 100 tracked objects.

### Added

- `TestSamplersDoNotAllocate` -- a sampler that grows the heap makes its own
  reading meaningless. The class check is done on the **AST**, so it holds on
  platforms where that branch cannot run; the others assert that `rss_mb`,
  `_type_histogram` and `observe` each retain nothing across repeated calls.

### Standing back

This is the third time the instrument has been the finding rather than the
SDK: the listener leak that read as a gateway replaying batches, the RSS zero
that read as no memory used, and now the class-per-sample that read as a
reconnect leak. Each was caught by making the instrument sharper rather than
by being more careful, which is the argument for these checks existing at all.

**How much of the ~1170 objects and ~1.6 MB per reconnect was this?** Unknown
until the next run. 300 classes across 11 cycles is roughly 27 per reconnect,
each carrying ten getset descriptors plus tuples and dicts -- plausibly most
of it, but that is arithmetic, not a measurement.

## 1.14.1

The object-count check answered the question it was added for.

### Confirmed: it is a real leak, not fragmentation

300 rounds, 300/300 delivered, 0 duplicated -- and:

    RSS MB        51.1 -> 68.3     +17  (+57.3/1000 rounds)
    gc objects   47815 -> 60685  +12870  (+42900/1000 rounds)

Both climbing together means retention, not heap fragmentation. Attributing it
to the eleven forced reconnects: **~1170 retained objects and ~1.56 MB per
close/login/connect cycle**, with the per-block deltas consistent and the
occasional negative one showing the collector is running and simply not
reclaiming it.

Two things worth recording about the measurement itself:

* The soak's own bookkeeping is **not** in that number. Its sample dicts hold
  only scalars, and CPython untracks dicts with no container values, so they
  never appear in `gc.get_objects()`. I checked before attributing anything to
  the SDK -- having already reported the tool's own listener leak as a gateway
  bug once, the observer was worth ruling out.
* RSS alone would have been unreadable here. It grew by the same amount in the
  previous run, when the cause was different.

### Added

- **A type histogram.** "12870 more live objects" leaves the reader guessing
  which class is accumulating, and guessing has been the expensive part of
  this whole exercise. The summary now diffs the most common live types
  between the first and last sample and names what grew -- including types
  that did not exist at round zero, which is the strongest signal.

Next run names the retained class, at which point the leak is a fix rather
than an investigation.

## 1.14.0

**227 live tests passing, zero failures.** The group-name ladder settled it on
the first candidate (`"grp xxxx"` -- 8 chars, two words), and `edit_role` is
finally correct. The soak came back 300/300 delivered, 0 duplicated, 0 missed.

### What the clean soak found

RSS still climbed 50.8 -> 66.5 MB across 300 rounds, unchanged by the listener
fix -- so that was never the cause. Attributing it to the eleven forced
reconnects gives **~1.07 MB per reconnect cycle**, consistent across all of
them.

The transport's close path is correct: it clears the client and awaits
`aclose()`, and `_get_client()` builds a fresh one. So this is either real
retention or heap fragmentation, and **RSS cannot tell those apart** --
CPython does not reliably return freed arenas to the OS. Reporting one as the
other would send someone hunting a leak that may not exist.

So the soak now samples `gc.get_objects()` alongside RSS:

* objects growing **and** RSS growing -> retention, reported as a finding
* objects flat, RSS growing -> fragmentation, reported as a note

### Fixed

- **The RSS threshold under-reported.** 16 MB over 300 rounds looked harmless
  against a 200 MB absolute check, and is 52 MB per thousand rounds. There is
  a rate check now, and it attributes the growth to reconnects when they are
  what drove it. The previous run said "nothing drifted" about exactly this.

- **`close()` called `stop_audio()` on None for every text-only client.** The
  else branch ran whenever `_calls` was None -- the common case, since voice
  is built lazily -- raising AttributeError on every close and swallowing it.
  Harmless in effect, but it meant the voice teardown block was dead code that
  did nothing except raise.

## 1.13.1

First stress run: 300 rounds in ~2 minutes, 300/300 delivered, 11 forced
reconnects, median latency 459 ms. It also reported "275 probes arrived more
than once -- the resync is replaying without deduplicating", which was wrong,
and the way it was wrong is the most useful thing in this release.

### Fixed -- a false finding in the soak itself

`close()` does not clear listeners, and `cycle_gateway()` called `attach()`
again afterwards. Each forced reconnect therefore stacked another copy of the
message handler, and every message incremented the counter once per copy.

The tell was in the data: duplicates began at **round 27**, one past the first
forced reconnect at 25. Not a gateway replaying a batch -- a soak counting the
same delivery twice. `attach()` now detaches first, and 40 rounds across five
reattaches produce zero duplicates.

Worth stating plainly: a measurement tool that reports a bug in the thing it is
measuring is the worst kind of wrong, because the finding looks like exactly
what you built it to find.

### Fixed

- **`admin.edit_role()` still returned INTERNAL** -- third attempt.
  `CommunityRole` names the channel set `permissions`, not
  `channel_permissions`, so the carry-forward read the wrong attribute,
  silently got None, and left the field omitted. It reads `role.permissions`
  now.

### Changed -- channel group names get a ladder, not a fourth guess

Three guesses have been wrong: hyphens (`rootpy-ag-1a2b`), then digits
(`rootpy group 7269`), then letters-and-spaces-only (`rootpy group bbcy`) --
all rejected, while the sandbox's `test area` is fine. So it is not the
character set; what is left is length or word count.

`create_channel_group()` in conftest now walks five candidate shapes and
prints which one Root accepted, with its length and word count. Same approach
that settled `PictureHex` in a single run.

## 1.13.0

### Added -- stress mode, so most of the soak fits in 90 seconds

    python devscripts/soak.py --stress 300 --reconnect-every 25

Most of what a soak watches is **volume**-dependent, not time-dependent, and
volume compresses. A leak of 8 MB per thousand messages is invisible in a
30-second idle run and obvious in 300 back-to-back probes. Reconnect behaviour
is the same: what matters is the 500th cycle, not the wall clock it happened
at, so `--reconnect-every N` forces full close/reconnect cycles instead of
waiting for the hub's own.

Growth is reported against whatever drove it -- per 1000 rounds while
stressing, per hour when idling. "200 reconnects in 90 seconds" is not
"8000 per hour"; it is 200 reconnects.

Forced reconnects are counted separately from natural ones, because a
reconnect we caused is not evidence about the hub's cycle. Listeners are
reattached after each cycle, since they do not survive a `close()`.

**What still needs the long run**, and there is no way around it: token or
session expiry, a server deploy mid-run, anything keyed to time of day. Those
are genuinely time-dependent. Everything else -- leaks, handle growth, cache
bounds, delivery under repeated reconnects -- now fits in a coffee break.

## 1.12.3

219 live tests passing. The `add_role`/`remove_role` fix landed; 8 failures
left, both causes my own incomplete work.

### Fixed

- **`admin.edit_role()` -- the previous fix was half a fix.** 1.12.1 carried
  `color_hex` forward but left `community_permissions` and
  `channel_permissions` conditional, so a replace still blanked them and Root
  still answered INTERNAL (13). Every field is now carried from the current
  role: both permission sets, both flags, and the colour.

  Third appearance of this pattern (`CommunityEdit`, `RoleManager.edit`, now
  the admin service) and the second time I fixed it one field at a time.

- **Channel group names reject digits, not hyphens.** Two rounds to pin down,
  and I blamed the wrong thing first: `rootpy-ag-1a2b` failed so hyphens
  looked like the cause, but `rootpy group 7269` failed too while `test area`
  is fine. New `word_tag()` yields four lowercase letters, since `tag()` is
  hex and unusable for group names.

### Confirmed by the soak

First real two-account run: 3/3 probes delivered, median latency 850 ms,
0 reconnects, and RSS reading correctly at 50.6 MB -- the third Windows
attempt works.

One number worth a longer look: RSS went 50.6 -> 58.4 MB across 62 seconds.
Over three samples that is startup noise; at that slope over a day it would
not be. Telling those apart is what the soak is for and needs a real run.

## 1.12.2

### Fixed

- **The Windows RSS reader still did not work**, and the offline suite failed
  because of it. Two earlier attempts both failed silently: the first had no
  Windows branch at all, the second called `ctypes.windll.psapi` without
  declaring argument or return types -- which is where 64-bit handle
  truncation bites. It now types the handle properly and tries
  `K32GetProcessMemoryInfo` (kernel32, Windows 7+) before `psapi.dll`.

- **An unmeasurable RSS no longer fails a soak run.** It was in the findings
  list, so every run on a platform where it could not be read exited 1 over
  something that is not drift. It is a `note:` now -- losing one diagnostic
  signal is worth saying, but it is not a failure. Two tests cover the
  distinction, including that a note must not suppress a real finding.

  This was the offline test failing on your machine: the detector was right
  that RSS was unreadable, and wrong that this made the run bad.

## 1.12.1

217 of 232 live tests passing. All 10 failures were in `test_live_admin.py` --
the surface with no prior verification -- which is where they were expected.

### Fixed

- **`community.add_role()` and `remove_role()` never sent a request.** The wire
  field is `UserIds` (repeated); both sent the singular `user_id` and died in
  the encoder with "has no field 'user_id'" before anything left the process.
  Root assigns roles in bulk -- `client.roles.add_to_members()` is the same
  call for several people, and it worked, which is why this went unnoticed.

- **`admin.edit_role()` blanked the colour.** `color_hex` defaulted to `""`,
  and `CommunityRoleEdit` is a replace -- so editing a name wiped the colour
  and Root answered INTERNAL (13). Now defaults to None and carries the role's
  current colour forward. This is the same replace-semantics bug fixed in
  `RoleManager.edit` one layer up; the service beneath it still had it, which
  is exactly the argument for testing `admin` directly.

- **Channel *group* names reject hyphens** while channel names accept them.
  `rootpy-ag-1a2b` was rejected; `test area` is fine. The rules differ per
  object type, so none can be inferred from another.

### Changed -- the soak verifies rather than observes

The first soak run reported `events: 0` for its entire duration, which is an
unreadable number: nothing was sent, so nothing arriving proves nothing, and a
gateway that had silently stopped delivering would look identical.

It now runs **two accounts**. Each round the sender DMs a nonce and the
receiver must see it within a deadline, so every sample answers a real
question: did it arrive, how long did it take, did it arrive exactly once.
Duplicates are tracked separately because a hub that resyncs by replaying a
batch is exactly where they would come from, and a send failure is reported
separately from a delivery miss -- they are different findings.

Latency drift is now detectable: a gateway degrading from 300 ms to 8 s
overnight still "works" by every other measure.

Also fixed from that run's own output: `rss_mb` was `0.0` for the whole
Windows run, because `resource` does not exist there and `/proc` is absent, so
both branches failed silently. There is a Windows branch now, and an
unmeasurable value reports `-1.0` rather than zero -- a zero that means "not
measured" reads as "no memory used".

With only `ROOT_TOKEN` it degrades to observe-only and says so.

## 1.12.0

188 live tests passing, zero failures, before this batch.

### Added -- the admin service and the community manager gaps

`tests/test_live_admin.py`, 44 tests. `client.admin` was **0/19** -- not
unused, but only ever reached *through* `CommunityManager`. Every community
operation in the suite delegates to it, so it was exercised constantly and
verified never; given the manager layer has produced more bugs than anything
else here, calling the layer beneath it directly is worth doing.

Covers channel and group lifecycle, `edit_*`, the `move_*` ordering methods
(never exercised at all), access rules with allow and deny overlays, clone
with and without roles, and the synchronous cache readers on the manager.

### Added -- the soak script

`devscripts/soak.py`. HANDOFF has said since the beginning that nothing has
ever run continuously; this is that.

    python devscripts/soak.py --hours 24 --watch <channel-id>

Samples cache size, reconnect count, background-task count, transport counters
and RSS on an interval, writes one JSON object per sample to `soak.jsonl` (so
a run that dies keeps everything up to that point), and prints a verdict.
It reports findings rather than just numbers: a cache that climbs past its LRU
cap, background tasks growing faster than they finish, RSS growth, or a
connection that flaps. Exit code 1 when anything drifted.

Ten offline tests exercise the sampling and summary logic against a real
client, because a script meant to be started and forgotten must not fail
hours in with nothing recorded.

### Coverage

Service methods **112/230 (49%) -> 141/230 (61%)**. `admin` 0/19 -> 19/19,
`community` 10/39 -> 34/39.

## 1.11.1

185 of 192 live tests passing. Both notification assertions I flagged as
unproven held -- `mark_all_viewed()` does clear the count, and
`count_unviewed()` does equal the sum of `counts_by_container()`.

### Fixed

- **The structured codec could not encode an enum-typed field.**
  `set_max_online_status()` died with `TypeError: Unsupported protobuf field
  type 'UserOnlineStatus' for MaxStatus` -- but the type is supported; the
  *name* was ambiguous. `UserOnlineStatus` exists in two packages, so
  `resolve_enum` found two candidates, could not choose, returned None, and
  the encoder blamed the type.

  Resolving it would have been wrong regardless: the two descriptor entries
  **disagree with each other**, and neither matches the wire -- `ACTIVE` is
  `0x10`, and the generator dropped the hex literal (a HANDOFF finding from
  long before this). An int, including a `rootpy.enums` member, is now
  encoded directly with no schema lookup, because the caller's enum is right
  by construction. `bool` is excluded, being an int subclass.

  The error message for a genuinely unsupported value now names the fix.

### Notes

- `direct_messages.create()` answers ALREADY_EXISTS on a second call -- that
  is the contract, and `get_or_create()` is the idempotent one. The test now
  asserts the split rather than assuming `create` was idempotent.
- Only 2 of 82 enum short names are ambiguous (`UserOnlineStatus` and
  `ItemOneofCase`), so this affected a narrow slice -- but silently, and only
  at the point of sending.

## 1.11.0

Clean confirmation run first: 161 passed, 0 failed, 82.4 s -- the polling
change held, and "sleeps and client work" stayed at 20% (was 33% before it).

### Added

`tests/test_live_settings.py`, 26 tests. Service coverage **100/230 (43%) ->
112/230 (49%)**:

| service | before | after |
| --- | --- | --- |
| `friends` | 2/11 | **9/11** |
| `direct_messages` | 1/4 | **4/4** |
| `notifications` | 2/7 | **6/7** |
| `user_settings` | 1/8 | **4/8** |

Highlights: notes are checked to be *private to the setter* (the peer must not
see what you wrote about them), friendship is asserted symmetric from both
accounts, and `count_unviewed()` is checked to equal the sum of
`counts_by_container()` -- the two disagreeing would be a silent-wrong bug of
the kind this suite keeps finding.

### Deliberately not tested

`set_dm_invite_requirement`, `set_friend_invite_requirement` and
`set_community_invite_requirement` take a `UserDirectMessageInviteConnection`
(`Any`/`Connected`/`None`/`Friend`) and are exactly the gate deciding whether
the two accounts can DM each other. **There is no getter**, so a test could not
restore the previous value -- and setting it wrong would break every DM, call
and invite test on that account permanently, in a way that reads as an SDK bug
rather than a test that overreached.

The enum values are pinned from the descriptor instead, with a test that fails
if a getter ever appears, since that would make them safely testable.

`friends.remove()` is skipped for the same reason: it would tear down the
`friendship` fixture the DM and call tests depend on.

## 1.10.2

### Fixed

- **`eventually` was imported into four of the five live files.**
  `test_live_integration.py` imports from `.conftest` with a different name
  list than the others, so the edit that added the import silently did not
  apply there. The file still parsed, collection still succeeded, and the
  `NameError` only appeared partway through a live run -- two tests that had
  been passing for rounds.

### Added

- `TestNoUndefinedNamesInTests` walks every test module's AST, collects what
  is defined or imported at module level, and flags any global load with
  nowhere to come from. Checking that a file *parses* says nothing about
  whether its names *resolve*, which is exactly what went wrong.

  Deliberately narrow -- locals, comprehension targets, arguments, exception
  names, `global` declarations, builtins and module dunders are resolved
  first, so a hit is a real missing name. Verified by removing the import
  again and watching it name the file and line.

### Measured

The polling from 1.10.1 did what it was meant to:

| | before | after |
| --- | --- | --- |
| wall clock | 94.2 s | **85.1 s** |
| sleeps and client work | 32.6 s (33%) | **17.3 s (20%)** |
| round trip | 54.2 s (55%) | 54.8 s (64%) |

Round-trip time is unchanged in absolute terms, as it should be -- the same
270 calls to the same server. What went away was waiting for no reason. The
suite is now dominated by network, which is the right shape.

## 1.10.1

161 live tests passing, zero failures -- including the whole new permissions
file first time out. The timing data from 1.10.0 then paid for itself.

### Where a live run actually goes

From `--timing` over 166 tests, 94 s wall:

| | |
| --- | --- |
| round trip | 54.2 s (55%), 268 calls, **202 ms/call** |
| fixture setup | 11.3 s (12%) |
| everything else | 32.6 s (33%) |

That last third was almost entirely the suite sleeping.

### Changed

- **Fixed sleeps replaced with bounded polling.** Reads-after-write used
  `await asyncio.sleep(2)` before asserting, which pays the full two seconds
  even when the value landed in 200 ms *and* still fails when the server takes
  longer -- slow and fragile at once. New `eventually(check, timeout=,
  describe=)` polls until the condition holds, accepts sync or async checks,
  and raises with the description on timeout.

  Applied to the six worst offenders, including `test_kick_then_rejoin`
  (6.0 s non-network across 8 calls) and `test_watch_channel_delivers_a_new_message`
  (2.7 s). A test asserts no fixed multi-second sleep comes back.

- HANDOFF's performance section carried "~330 ms per round trip" from an
  earlier estimate; the measured figure is 202 ms.

### Notes

- The await-guard caught two mistakes in this change before they cost a run:
  `messages.history` is an async generator (awaiting it is the bug), and
  `eventually(lambda: client.direct_messages.list())` hides an un-awaited
  coroutine behind a lambda. The second was fixed by making the closures
  explicitly `async def` rather than by teaching the guard to see through
  lambdas -- the guard being unable to is the correct behaviour.

## 1.10.0

### Added -- per-test timing

`tests/timing_plugin.py`, enabled with `--timing`. Reports setup / call /
teardown per test in milliseconds and, for live runs, correlates each test
with the SDK's own `TransportStats` -- so the summary separates round-trip
time from rate-limit waiting from client-side work. `--timing-json=PATH`
writes the same data for comparing runs.

    pytest -m "live or live2" --timing --timing-json=timings.json

`--durations` reports seconds and only the slowest few, which says which test
is slow but not slow *doing what*. Session fixture cost lands in the setup
column of whichever test triggered it, which matters before optimising the
wrong thing.

### Changed

- The repo-wide AST guards parsed every `.py` once **per test** -- ~270 ms
  each, found by the new timing output. Cached for the session; the unwrap
  guards now cost 30 ms across 24 tests.
- The await-contract guard understands **async generators**. It was reporting
  `messages.history` as "a coroutine that is not awaited", when in fact
  awaiting it is the mistake -- it is iterated with `async for`. Now fails on
  awaiting one, with the reason.

### Added -- coverage

`tests/test_live_permissions.py`: permission rules, member queries, profile
writes and the message methods the main suite never reached. Plus offline
tests for the four pure permission builders. Service coverage **88/230 (38%)
-> 100/230 (43%)**; `permissions` goes 0/8 -> 6/8 and `members` 1/10 -> 6/10.

Profile writes share a rate-limit quota and a suite this size hits it, so
those tests skip on RESOURCE_EXHAUSTED. Username validation is asserted to
fail *locally*, with no round trip.

## 1.9.2

129 of 139 live tests passing. The seven failures were one self-inflicted bug
plus two real ones.

### Fixed

- **`unwrap_list(response).data` in seven methods -- my own regression.**
  The 1.9.1 edit was applied with a regex that replaced `return (` with
  `return unwrap_list(` and left the trailing `.data` *outside* the new call,
  so the envelope was unwrapped and then `.data` was read off the resulting
  list: `AttributeError: 'list' object has no attribute 'data'`. A second
  attempt to repair it by AST position produced
  `await x.list(...).data`, which reads `.data` off the coroutine. Both are
  invisible in a diff and neither had a unit test to catch them.

  All seven rewritten by hand. Two AST guards now fail on either shape
  anywhere in the package, and one of them immediately caught an eighth
  instance in `community_apps.list` that I had missed.

- **`emojis.list()` and `list_mine()` returned the envelope** -- missed in
  1.9.1 because `EmojiManager` lives in `features.py`, not `emoji.py`.

- **`friend_groups.move()` had an optional argument that could only fail.**
  `before_group_id` defaulted to None and was omitted, but Root applies a
  NotEmptyValidator to `BeforeFriendshipGroupId` -- so the default was not
  "move to the end", it was a guaranteed rejection. Now required, and an
  empty value raises locally with the reason.

### Notes

- The emoji upload path now works end to end: `create` uploads a real file,
  which was blocked by the token-only asset bug fixed in 1.9.1.
- Four regex-driven code edits have now misfired in this project. The two new
  AST guards exist because the failure mode is specifically "looks right in a
  diff, wrong at runtime".

## 1.9.1

The first run of the new service tests found four SDK bugs.

### Fixed

- **Asset uploads refused to work on a token-only client.**
  `_require_web_api_url()` raised "Client is not logged in" whenever there was
  no session -- so emoji creation, community files and avatar changes all
  failed, even though `login_token()` defaults `web_api_url` to a constant it
  would then have used anyway. Same shape as the `_optional_token` bug.
  Genuinely session-bound state (the gateway hub URL, the device id) still
  requires a login.

- **List methods returned the envelope, not the list.** Every list RPC answers
  with a message wrapping one repeated field, and the managers handed that
  back. Iterating `client.roles.list(...)` yielded field *names*, so
  `role.id` raised `AttributeError`, and `moderation.list_bans()` had the same
  shape -- which is why a ban never appeared in its own list. New
  `unwrap_list()` applied to `roles`, `friend_groups`, `community_files`,
  `voice_admin`, `directories`, `moderation.list_bans` and the file searches.

- **`roles.edit()` blanked everything it was not given.**
  `CommunityRoleEdit` carries Name, ColorHex, both permission sets and two
  flags -- a replace, like `CommunityEdit`. Sending only `name` made the
  server answer INTERNAL (13). Unsupplied fields are now carried forward from
  the current role.

- **`**kwargs` on public managers hid required arguments.**
  `directories.delete(directory_id=...)` reads perfectly and the wire field is
  `Id`; `community_files.list()` looked complete and Root requires a
  `DirectoryId`. `DirectoryManager` and `CommunityFileManager` now have named
  parameters taken from the request schemas, so the signature is the
  documentation. A test asserts no public method on either is `**kwargs`-only.

### Notes

- `CommunityLeave` on a community you own answers PERMISSION_DENIED. That is
  correct -- an owner deletes rather than leaves -- so the test now asserts it.
- Profile writes (`update_status`, `update_description`) are rate-limited by
  Root; those tests skip on RESOURCE_EXHAUSTED rather than failing.
- `invites.code_exists()` rejects the code `invites.create()` just returned
  (`Code: PredicateValidator`) while answering False for nonsense -- the two
  ends disagree about what a "code" is. Marked `xfail` with the detail rather
  than hidden, so a fix is visible.

## 1.9.0

### Added — coverage for the services nothing had ever reached

An audit put 16 of 27 services at zero coverage. `tests/test_live_services.py`
(43 tests, single account) plus additions to the two-account suite take the
SDK from **46/230 service methods exercised (20%) to 88/230 (38%)**.

Six services went from nothing to complete:

| service | before | after |
| --- | --- | --- |
| `roles` | 0/9 | **9/9** |
| `invites` | 0/7 | **7/7** |
| `emojis` | 0/5 | **5/5** |
| `friend_groups` | 0/5 | **5/5** |
| `community_service` | 0/3 | **3/3** |
| `moderation` | 0/5 | **4/5** |

Also first coverage for `directories` (3/6), `community_files` (3/9),
`logs` (1/2) and `voice_admin` (1/3).

**Moderation** was the priority: kick, ban, unban and `list_bans` are
destructive and had never run. The kick test re-invites the peer afterwards,
because a kick left in place silently breaks every later membership test.

Emoji and file uploads generate a 1x1 PNG at runtime rather than shipping a
binary fixture, so `emojis.create` and `community_files.create` exercise the
real upload path -- both take a filesystem path and upload it.

`community_service.leave` gets its own disposable community, since leaving the
sandbox would take the rest of the session with it.

### Preserved

The original baseline is still runnable exactly as it was:

    pytest -m live tests/test_live_integration.py tests/test_live_gateway.py

`-m live` now also picks up the new service tests. Anything created mid-test is
removed in a `finally` block even though the community delete would take it
anyway -- a leak inside a test should be visible, not masked by teardown.

### Still untouched

`admin`/`community_admin` (0/19, though reached constantly via
`CommunityManager`), `permissions` (0/8, needs a multi-role setup to mean
anything), `community_apps` (0/8, needs a real app), `search` (0/2, kwargs
undocumented).

## 1.8.4

### Changed

- `wait_for()` documents that something has to dispatch the event -- normally
  the gateway, so `connect()` first. A token-only client makes every RPC
  happily but has no event source, so a waiter on one can only time out, with
  nothing to indicate why.

  Deliberately **not** made to raise without a gateway: `test_features.py`
  pairs `wait_for` with manual `dispatch()` on an unconnected client, which is
  a legitimate pattern that predates this work. Changing designed behaviour to
  suit a test would be the wrong fix.

### Fixed (two-account suite)

- `test_the_peer_can_reply` waited on the token-only `client` fixture instead
  of the connected `gateway_client`, so the reply could never arrive. The
  neighbouring test passed because it waits on `peer`, which is connected --
  the asymmetry was the clue.

## 1.8.3

### Fixed

- **`friend_requests.accept()` responded with your own user id.**
  `NotificationPacket.UserId` is the notification's *owner*, not whoever sent
  it -- a request from account A arrives in account B's inbox carrying **B's**
  id at the top level. `_unpack()` read that field and passed it to
  `FriendshipInviteRespond` as `FriendUserId`, so accepting a request asked
  Root to befriend yourself.

  The counterparty is in the payload:
  `NotificationPayloadFriendshipInviteCreated {UserId, FriendUserId}`. The new
  `_notification_counterparty()` reads both and returns whichever is not the
  owner, so it does not depend on assuming which of the two is the sender --
  tested both ways round.

  Like the `pending()` bug before it, this never raised. It just did the wrong
  thing quietly.

### Added

- `friend_requests.accept_all()` -- accept everything pending, returns the
  count. A sender-agnostic path for when you do not care who sent them.

### Notes

- Found by the failure diagnostic added in 1.8.2, which printed the peer's
  actual inbox: `type=FRIENDSHIP_INVITE_CREATED user_id=<the peer's own id>`
  next to the sender it was looking for. Two bugs in this area have now been
  identified in one run each rather than by guessing.

## 1.8.2

### Fixed

- **`friend_requests.pending()` silently returned nothing.** It read
  `notification_type.name` and matched the substring `"FRIEND"`, but the wire
  carries a plain integer -- `getattr(1, "name", str(1))` is `"1"`, so the
  test was never true and the list came back empty every time.
  `NotificationType.coerce()` existed for precisely this case and was not
  used.

  A filter that returns the wrong *set* rather than raising is the worst shape
  for a bug: `pending()` looked like it worked, and every caller concluded
  there were no requests.

- **`pending()` also over-matched.** `"FRIEND"` catches
  `FRIENDSHIP_INVITE_RESPONDED` -- somebody answering a request *you* sent.
  Those are not pending and cannot be accepted. It now matches
  `FRIENDSHIP_INVITE_CREATED` exactly, with `responded()` added for the other
  kind.

### Changed

- The `friendship` fixture now prints the peer's actual inbox when it times
  out -- notification types, senders, and what it was looking for. Given
  `pending()` had just been silently empty, "no request arrived" and "the
  request arrived and we failed to recognise it" need to be distinguishable
  without spending another live run.

## 1.8.1

### Added

- **`friend_requests.pending_from(user)` / `accept_from(user)` /
  `decline_from(user)`** -- answer a request from somebody you already know.

  `pending()` returns every incoming request, so responding to a known person
  meant listing notifications, filtering by type, and matching ids by hand at
  the call site. Each accepts a user id, a user object or a username, compares
  ids without caring about encoding, and returns `False` rather than raising
  when there is nothing pending -- callers poll these.

### Fixed (two-account suite)

- DM and call tests ran before the accounts were friends. Root's default
  privacy setting only accepts DMs from friends, so all ten failed with
  `PERMISSION_DENIED`.

  Not an SDK bug: `DMMemberService._diagnose_dm_gate` had already replaced the
  raw gRPC error with "you're not friends, and Root accounts commonly only
  accept direct messages from friends -- send a friend request first with
  client.add_friend(username)". That diagnosis was exactly right, and is the
  reason this took one run to identify rather than several.

  A session-scoped `friendship` fixture now sends the request from one account
  and accepts it from the other before anything that needs it. That makes the
  request/accept round trip a tested prerequisite on every run rather than a
  test nobody depends on, and it is idempotent so burner accounts that stay
  friends between runs skip straight through.

### Notes

- `pytest -m live` remains exactly 71 tests and is untouched by any of this.

## 1.8.0

### Added — two-account tests

`tests/test_live_two_accounts.py`, 24 tests behind a new `live2` marker. These
cover what one account structurally cannot prove: a single client can send a DM
and see it in its own history, but only a second account shows it was
*delivered*.

- **DMs both directions** with real push delivery, asserted via
  `peer.wait_for("message", check=...)` rather than polling history.
- **Call signalling** -- `call_user()` opens the DM and creates a session,
  which rings the other side; the peer receives
  `packet_direct_message_ring`. None of that path touches WebRTC, so it is
  testable without the optional voice extra installed. This is the first
  coverage `client.calls` has ever had.
- **Mentions**, the one channel case Root actually pushes.
- **Friend requests, blocking, membership and nicknames** across the pair.
- `joined_peer` puts the second account in the sandbox community through a real
  invite, so the invite path gets exercised rather than bypassed.

`ROOT_TOKEN2` is required; `distinct_accounts` fails loudly if both tokens
point at the same account, which would make every send/receive test pass
trivially.

### Preserved

`pytest -m live` still selects exactly the same 71 tests and does not depend on
the second token. The two-account work sits under its own marker precisely so
the passing single-account suite stays a stable baseline:

| command | selects |
| --- | --- |
| `pytest -q` | 519 offline |
| `pytest -m live` | 71 (one token) |
| `pytest -m live2` | 24 (two tokens) |
| `pytest -m "live or live2"` | 95 |

### Changed

- The await-contract guard scans `peer.*` calls too, and now recognises
  `create_task` / `gather` / `ensure_future` / `shield` as valid consumers of a
  coroutine -- it was flagging `create_task(peer.wait_for(...))`, which is the
  normal way to arm a waiter before triggering what it waits for. Verified it
  still catches a genuinely missing `await`.

## 1.7.0

### Added

- **`client.drain_events(timeout=None)`** -- wait for spawned event handlers to
  finish; returns how many were awaited.

  Event handling is deliberately fire-and-forget: `message` events and every
  `add_listener()` handler are spawned as background tasks so a slow handler
  cannot stall the gateway read loop and a raising one cannot take the socket
  down. That is right for the socket. What was missing is the other half --
  `dispatch()` is a public coroutine, but awaiting it does not mean the
  handlers ran, and there was no supported way to find out. Callers were left
  with an arbitrary `asyncio.sleep` or a poke at `_background_tasks`.

  Drains handlers spawned by handlers too, so a chain settles rather than only
  its first link, and skips its own task so it cannot deadlock on itself.

### Changed

- `dispatch()` now documents that contract in its docstring rather than
  leaving the behaviour to be discovered. `wait_for()` is unaffected -- waiters
  resolve inline, before any background work is scheduled.
- LLMS.md section 5 covers it, since a model reading "await dispatch" would
  reasonably assume the handlers had run.

### Notes

- Found by three live gateway tests that asserted immediately after
  `dispatch()`. The behaviour they hit was correct; the API had no way to
  express "and now wait". Six offline tests reproduce all three cases without
  a token, including the raising-handler case -- which passes, confirming a
  bad handler never did suppress the others.

## 1.6.1

### Fixed

- **Three test reads used the platform default encoding.** `read_text()`
  without `encoding=` is UTF-8 on Linux and cp1252 on Windows, so the docs
  (which contain em dashes) read fine in CI and raised `UnicodeDecodeError`
  on Windows -- eleven failures from one missing keyword argument.

  The shipped `rootpy/` package was already clean; this was confined to the
  documentation-drift tests added in 1.6.0.

### Added

- `TestTextIOAlwaysDeclaresEncoding` walks every `.py` in the repo and fails
  on any `open()` / `read_text()` / `write_text()` that omits `encoding=`,
  skipping binary modes and non-filesystem `open()` calls like `dm.open()`.
  Fixing the three call sites would have left the next one just as invisible.
  Verified by planting an unqualified read in `rootpy/cache.py` and watching
  it fail.
- CI runs the matrix on **windows-latest** as well as ubuntu. This class of
  bug cannot be caught on Linux at all. The wheel-packaging steps stay
  Linux-only.

## 1.6.0

### Added — gateway coverage

`tests/test_live_gateway.py`, 21 live tests. The gateway had none at all: 91
packet types, eight typed events, `wait_for`, `watch_channel`, `unwatch_all`
and the reconnect cycle, never exercised.

Written against how Root actually behaves rather than how a push gateway
usually does: **ordinary channel messages are not pushed** (only mentions and
DMs), so `watch_channel` is tested by polling for a marker rather than by
awaiting `on_message`, which would hang forever by design. A **close is the
normal cycle**, not a failure. Also covers the cases that break event loops in
production -- a handler that raises must not suppress the others, `close()`
must be idempotent, `wait_for` must time out cleanly.

A separate `gateway_client` fixture does `login_token()` + `connect()`, kept
apart from the token-only `client` so a socket problem cannot take the RPC
tests down with it.

### Added — error paths

`tests/test_error_paths.py`, 75 offline tests. Two of twenty-one exception
classes were referenced anywhere, yet this machinery is where every bug in the
live bring-up surfaced. Covers all 16 gRPC status mappings, 10 HTTP code
mappings, the hierarchy, the description/hint surface, and that decoded
validation detail stays reachable as data rather than only in the message.

### Changed

- The await-contract guard now scans **every** `test_live_*.py` and both
  client fixtures. It was hard-coded to one filename, so a new live module got
  no protection -- exactly when it is most useful. 42 checks -> 52.
- `connect()` gives an accurate error. It said "Call login() before connect()"
  when `login_token()` is what a token-holder wants.

### Documentation

- **LLMS.md**: seven protocol findings from the live bring-up added to section
  9 -- where Root hides validation detail, the `#rrggbb` rule, CommunityEdit's
  replace semantics, channel type values, per-object name rules, message
  tombstones, and that ordinary channel messages are not pushed. Section 11
  rewritten around the error hierarchy and reading `validation_errors`. The
  client-verb listing is regenerated from the live package and now marks
  synchronous entries and lists properties separately, since awaiting those is
  the recurring mistake.
- **docs/errors.md**: how to read a rejection, the attribute table, and why
  HTTP 401 and gRPC UNAUTHENTICATED are different problems.
- Tests guard both against drift.

### Not done

- `connect()` still requires an explicit `login_token()` for a token-only
  client. Making it implicit was tempting and would have suited the new
  fixture, but it is documented behaviour with a test asserting it, and
  `connect()` should not start making login round trips as a side effect.
- DMs remain near-uncovered (`direct_messages` 0/4). They need a second
  account.
- `client.calls` remains 0/14. Testing a headless-Chromium voice stack before
  deciding whether to keep that architecture would be wasted effort.

## 1.5.0

### Added

- **`rootpy.validation`** -- one home for Root's field format rules, checked
  before the request goes out. Root answers a violation with a generic
  INVALID_ARGUMENT and a `RequestValidatorList`; that is diagnosable, but it
  costs a round trip to learn something the client already knew.

  - `validate_username()` implements Root's stated rule: 3-20 characters of
    letters, numbers, underscores and periods, with underscores and periods
    neither at the start or end nor next to each other. Applied in
    `UserService.set_username`. The error names the part of the rule that was
    broken and the offending character; the value is returned unchanged
    rather than "fixed up", because silently rewriting somebody's chosen
    username would be worse than refusing it.
  - `validate_nickname()` applies the same rule, now enforced in
    `MemberManager.edit_nickname`. Confirmed live: `rootpytester`,
    `RootpyTester` and `rootpytester1` accepted; `rootpy tester` and
    `rootpy-tester` rejected. Underscores and periods are inferred from the
    shared charset -- a live test covers that specifically, so the inference
    gets confirmed or corrected rather than assumed.
  - `normalize_hex_colour()`, `DEFAULT_PICTURE_HEX` and `PICTURE_HEX_LENGTH`
    moved here from `services/community_admin`, which was never the right
    home for a general format rule. Still importable from the old location.

  All exported from `rootpy` and covered by 38 offline tests.

## 1.4.1

### Fixed

- **The message list parser dropped most of what it decoded.**
  `_parse_message_list` ran `decode_packet("MESSAGE", ...)` and then mapped
  only `id`, `container_id`, `user_id`, `content` and `community_id` into
  `Message` -- discarding `deleted_at`, `edited_at`, `pinned_at` and
  `payload`, all of which the packet schema supplies and the model declares.

  `deleted_at` being permanently None meant `Message.is_deleted` was always
  False, so 1.4.0's tombstone filter silently did nothing: deleted messages
  still came back from history looking live. Adding the filter without
  checking that the field reaching it was ever populated was the mistake.

### Notes

- Nicknames: Root rejects `"nick 43c1"` and `"nick-b8e4"` with a
  `RegularExpressionValidator` and no pattern, while role and channel names
  accept both hyphens and digits. Those two samples do not separate the
  cause, so `test_edit_own_nickname` now probes five shapes that vary one
  property at a time and prints the resulting map. Validation goes into
  `edit_nickname` once the rule is known rather than guessed.

## 1.4.0

Everything here is fixed in the SDK. Where an earlier round had worked around
a problem in the test suite, the workaround is removed and the cause fixed.

### Fixed

- **`edit_community()` still could not do a partial edit.** 1.3.0 carried
  `Name` forward but left `PictureHex` conditional, so the next live run was
  rejected for `PictureHex` instead of `Name` -- the same bug, one field
  along. CommunityEdit is a *replace*: every replace-semantics field is now
  carried forward from a single read of the current community, and
  `PictureHex` is normalised on edit as it is on create.
- **`count_unviewed()` returned a payload object, not a count.** Root answers
  with a repeated per-container list; the manager handed it straight back for
  something whose entire purpose is a number. Now returns `int`, with
  `counts_by_container()` for the breakdown.
- **`get_note()` returned the whole `UserNoteResponse`.** Callers asking for a
  string got a dict, and an unset note looked like a populated object. Now
  returns the note text or `None`.
- **Deleted messages were returned as if they still existed.** MessageList
  includes tombstones with `deleted_at` set; nothing filtered or flagged them.
  Added `Message.is_deleted`, and `list()` now drops tombstones by default
  (`include_deleted=True` to see them).
- **`StateCache.members` was unbounded on the community axis.** The per-
  community bucket was capped at 2000; the dict holding those buckets never
  evicted, so a long-running client accumulated one cache per community it
  ever saw. Now an LRU of LRUs, `max_communities=500` by default.
- **`TransportStats.report()` said "no requests recorded" for a client that
  had only ever been throttled** -- hiding the exact situation you open the
  report to investigate.
- **Role `color_hex` is normalised** to the same `#rrggbb` form as
  `picture_hex`, now proven against the live API. `"3498db"` and `"#3498DB"`
  both work instead of costing a round trip.

### Changed

- The six synchronous cache readers (`get_community`, `get_channel`,
  `get_channel_group`, `get_message`, `get_user`,
  `messages_for_container`) had no docstrings at all. Each now states that it
  is a cache read, returns None on a miss, and raises TypeError if awaited --
  the mixup that cost three separate live runs to find one at a time.

### Removed from the test suite (the SDK handles it now)

- Explicit `picture_hex` in the sandbox fixture -- the default is valid, and
  passing one hid regressions in it.
- `asyncio.wait_for(..., timeout=30)` around asset calls -- the transport
  already enforces a 30s timeout.
- Assets are called with a single URI string rather than a one-item list;
  `resolve`/`get` have always accepted both.

## 1.3.0

Three SDK bugs, all found by the first live run that got far enough to reach
them (33 passing, 11 failing, in four groups).

### Fixed

- **A token-only client was unauthenticated on half the API.**
  `_optional_token()` returned `""` whenever `session is None`, ignoring a
  token passed to the constructor -- unlike `_require_token()`, which falls
  back to it. `StructuredAPI`/`RawAPI` omit the authorization header entirely
  when the getter is empty, so `RootClient(token=...)` without a login got
  **HTTP 401** on everything routed through `client.high`: notifications,
  friends, blocks, user notes, member edits -- most of `features.py` and
  `domain_managers.py`. The `services/` layer worked on the same client,
  which is what made it look like a per-endpoint permissions problem.

  Note this breaks the "fire-and-forget, zero login round-trips" usage that
  `_require_token`'s own docstring advertises.

- **`list_messages()` sent a `Limit` the server rejects.**
  `MessageService.list` documents "and no Limit, which the server rejects"
  and defaults it to `None`. `HighLevelMixin.list_messages` defaulted to
  `limit=50` and passed it through, reintroducing the bug one layer up. Now
  `None`, with a test asserting the two layers agree.

- **`edit_community()` could not do a partial edit.** CommunityEdit is a
  replace, not a patch: Root applies a `NotEmptyValidator` to Name, so
  changing only the description was rejected. The current name is now carried
  forward automatically (one extra read; pass `name=` to skip it). This also
  fixes `clone_community`, which calls `edit_community` internally.

### Notes

- HANDOFF listed "DMs and friends -- conversations, friend requests, blocks"
  as verified. Only `friend_requests.send` was ever exercised;
  `notifications.list`, `friends.list`, `blocks.list` and `get_note` had
  never been run, which is why the token bug survived. Corrected there.

## 1.2.2

### Fixed (live test suite)

- **Session-scoped fixtures were running on function-scoped event loops.**
  `client` and `sandbox` are `scope="session"` and hold an httpx `AsyncClient`
  and a websocket, but pytest-asyncio defaulted to a fresh loop per test. The
  first test closed the loop those objects were built on, so everything after
  it failed with `'NoneType' object has no attribute 'send'`, `Event loop is
  closed`, or a spurious `401` -- 19 failures from one cause, passing or
  failing by ordering luck. `asyncio_default_fixture_loop_scope` and
  `asyncio_default_test_loop_scope` are now both `"session"`.
- `client.get_message()` and `client.messages_for_container()` are synchronous
  cache reads and were being awaited.

### Added

- `TestLiveSuiteAwaitsCorrectly` walks the live suite's AST, resolves every
  `client.*` call against the real object, and fails offline if a coroutine
  is not awaited or a synchronous method is. Understands `asyncio.wait_for`
  as an await. 42 checks, and verified to fail when an await is reintroduced.

  This replaces finding these one per live run: `community.get`,
  `get_message` and `messages_for_container` were each discovered separately.

## 1.2.1

### Fixed

- **`CommunityManager.TEXT` was 0 and `VOICE` was 1 — both wrong.** The
  descriptor says `ChannelType {Unspecified: 0, Text: 1, ThreadedText: 2,
  Voice: 4, App: 8}`. So `create_text_channel()` sent UNSPECIFIED and Root
  rejected it (`ChannelType: The specified condition was not met.
  [PredicateValidator]`), and `create_voice_channel()` sent TEXT — which
  *succeeds* and silently creates a text channel. The constants are now taken
  from `rootpy.enums.ChannelType` rather than redefined, with a regression
  test pinning them to the generated descriptor.

  Note `client.admin.create_channel(..., channel_type=1)` was always correct;
  only the `CommunityManager` convenience wrappers were affected, which is why
  `fullrun.py` and `example.py` (both of which pass the type explicitly) never
  showed it.

### Changed

- Live suite uses the name shapes `fullrun.py` is known to succeed with:
  `rootpy-role-<tag>`, `rootpy-text-<tag>`. Root applies a
  `RegularExpressionValidator` to role names and rejected `"tester 3a8f"` —
  community names allow spaces, role and channel names do not.
- Sandbox setup now makes only the minimal proven calls. Anything less
  certain (role colours, permission sets) moved into individual tests, where
  a rejection isolates instead of taking the whole sandbox down.

## 1.2.0

### Fixed

- **gRPC errors now name the field Root rejected.** `GrpcWebError.__init__`
  already decoded `root-exception-bin` into `self.validation_errors`, then
  built the message string without them -- so every `INVALID_ARGUMENT` read
  "One or more request values were rejected by Root" while the actual reason
  sat one attribute away, unread. The detail is now part of the message, so
  it appears in tracebacks, logs and any caller's error handling:

      CommunityCreate failed — INVALID_ARGUMENT (3): ...
        -> PictureHex: 'Picture Hex' must be 7 characters in length.
           You entered 6 characters. [ExactLengthValidator]

  Up to five fields are listed, then `(+N more)`. Falls back to
  `RootExceptionInfo.summary()` for the non-validation payload kinds.
- `[Root: ...]` is suppressed when it merely repeats the status description.

### Changed

- The live `sandbox` fixture attempts every setup step even after one fails
  and reports them together, instead of aborting on the first. A fail-fast
  fixture meant each broken call cost a separate live run to find.

### Notes

- The regression test builds a real `root-exception-bin` header. Worth knowing
  if you extend it: `Errors` is a *repeated* field 10, so entries are
  concatenated field-10 records. Wrapping them in a single field 10 still
  makes a one-error assertion pass by substring-matching the raw bytes -- a
  false green that hid the mistake until a two-error case was tried.

## 1.1.3

### Fixed

- **`picture_hex` must be the 7-character `#rrggbb` form.** 1.1.2 fixed the
  empty default but normalised *away* the leading hash, which was backwards:
  Root validates PictureHex with an `ExactLengthValidator` of 7 and answered
  "must be 7 characters in length. You entered 6." `DEFAULT_PICTURE_HEX` is
  now `"#3f51b5"` and `normalize_hex_colour()` always returns `#rrggbb`.
  Eight-digit alpha values are now rejected, since 7 is exact.

### Notes

- `picture_hex` is a **colour**, not an image. The image slot on
  `CommunityCreateRequest` is field 13, `IconUploadTokenUri`, which takes a
  Root upload-token URI from the asset service -- not an arbitrary external
  URL.
- Role `color_hex` now uses the same `#rrggbb` form in the live suite. That
  is assumed from PictureHex rather than proven; if Root disagrees, the
  fixture's `_root_detail` prints the expected length.

## 1.1.2

### Fixed

- **`create_community()` could never succeed with its own defaults.** Root
  applies a `NotEmptyValidator` to `PictureHex`, but `picture_hex` defaulted
  to `""` and the body was guarded with `if picture_hex:` -- so the default
  path omitted field 11 entirely and every such request came back as a
  generic `INVALID_ARGUMENT` naming no field. `example.py`, which calls
  `create_community` without a colour, was broken by this too.

  `picture_hex` now defaults to `DEFAULT_PICTURE_HEX` ("3f51b5"), field 11 is
  always sent, and the new `normalize_hex_colour()` accepts `"3f51b5"`,
  `"#3F51B5"` and the 8-digit forms while raising a `ValueError` that names
  the field rather than letting an unusable value reach the server.

### Notes

- Found by the live suite's diagnostic ladder. Root *does* populate
  `RequestValidatorList` with `{property_name, error_message, error_code}`;
  rootpy already decoded it in `root_exception.py`, it just was not being
  printed. The live fixture now surfaces it, which turned an opaque
  "one or more request values were rejected" into
  `PictureHex: 'Picture Hex' must not be empty. [NotEmptyValidator]`.
- The community-name ladder has been removed: the name was never the problem.
- `edit_role(color_hex="")` keeps its empty default, where it means
  "leave unchanged" rather than "send empty".

## 1.1.1

### Fixed (live test suite)

First live run against a real account exposed two mistakes in the tests
themselves -- no SDK bugs:

- The sandbox fixture called `client.community.create_community()`, but
  `client.community` is `CommunityManager` (whose method is `create()`);
  `CommunityAdminService.create_community()` lives at `client.admin`. The
  suite now uses `CommunityManager` throughout, which also exercises
  `clone`, `create_text_channel`, `create_voice_channel`, `get_channels`
  and `get_roles` -- all previously unreferenced.
- `client.is_connected` is a property and was being called as a method.
- `permissions_for(user_id, channel_id)` and `get_message(message_id)` were
  called with the wrong arguments; `assets.resolve/get` take a sequence.
- Asset calls are wrapped in `asyncio.wait_for(..., timeout=30)` after the
  first run appeared to hang there.

### Added

- `TestLiveSuiteContract` in the offline suite asserts the attribute names,
  property-vs-method distinctions and signatures the live tests rely on, so
  wrong-layer mistakes now fail in CI instead of mid-run against a real
  account. Offline tests: 216 -> 253.

## 1.1.0

### Fixed

- **rootpy no longer installs anything at runtime.** `client.run()` used to
  call `pip install` for FFmpeg, PyAV and aiortc before it logged in, and
  `calls.py` would `pip install playwright` and download Chromium mid-call.
  This broke in frozen builds, read-only containers, CI images and managed
  virtualenvs, and failed with an error that pointed at pip rather than at
  rootpy. Missing pieces now raise `MediaDependencyMissing`, naming the exact
  install command. Install voice support with `pip install "rootpy[voice]"`.
- `ChannelPermissions.has()` returned `True` for *any* string when
  `channel_full_control` was set, so `perms.has("channel_veiw")` silently
  reported a typo as granted. Unknown flag names now return `False`;
  full control still implies every real permission.
- `RootClient.close()` no longer constructs the voice service just to tear it
  down.

### Changed

- `RootClient(auto_install_media=True)` is now `RootClient(require_media=False)`.
  It verifies the media stack is present and fails fast; it never installs.
  Default is off, so text-only programs are unaffected.
- `client.calls` is built on first access instead of in `__init__`.
- The generated registries moved from ~1 MB of single-line Python literals
  (one line was 788 KB) to JSON under `rootpy/data/`, loaded lazily by
  `rootpy._registry_loader`. `SIMPLE_MESSAGES` is now derived from
  `MESSAGES` keys rather than stored a second time.
- `_read_audio_bytes` sends `User-Agent: rootpy` instead of a forged Chrome
  string.

### Performance

- `import rootpy` is **27% faster** (161 ms -> 117 ms) and loads **93 fewer
  modules**, because `httpx` and the voice stack are imported on first use
  rather than at import time.

### Packaging

- `[tool.setuptools.package-data]` ships `rootpy/data/*.json` — required now
  that the registries are data files. CI asserts the wheel contains them and
  that an installed wheel can read them.
- `rootpy.examples`, `tests` and `devscripts` are excluded from the wheel.
- New `[voice]` extra: `av`, `aiortc`, `imageio-ffmpeg`, `playwright`.

### Tests

- 109 -> **216 offline tests**, plus **44 live tests**.
- `tests/test_untested_surface.py` covers modules that previously had none:
  permissions, pagination, stats, enums, packets, models, commands, the
  registry loader and the high-level surface.
- `tests/test_live_integration.py` + `tests/conftest.py` add a
  self-bootstrapping live suite. It needs only `ROOT_TOKEN`, hard-codes no
  IDs, creates a community it owns (so permissions are never the blocker),
  exercises the API inside it, and deletes it afterwards. Skipped unless the
  token is set; run with `pytest -m live`.

### Repo layout

- Deleted `rootpy/examples/` and `rootpy/docs/` (duplicates of the top-level
  `examples/` and `docs/`); `docs/errors.md` and `rootpy/docs/errors.md` were
  byte-identical.
- Moved `rootpy/DM_README.md` and `rootpy/README.md` into `docs/package/`.
  `rootpy/` is now code and data only.

## v1.0.0

First tagged release. The client is verified end to end against a live server:
a 39-step lifecycle run covering join, create, message, verify and clean up,
plus a two-account test confirming delivery and latency.

### Working

- **Messaging** — send, reply (threaded), edit, delete, react, pin, paginated
  history
- **Communities** — join/leave, create, channel groups, channels, roles,
  members, permissions
- **Members and profiles** — listing, batched profile fetches, random picks
- **DMs and friends** — conversations, friend requests, blocks
- **Account** — presence, custom status, username, avatar, banner, about-me
- **Real-time** — 91 typed packet types, 13 friendly events, `wait_for`,
  multi-handler listeners, unread-driven channel watching
- **Assets** — `root://` URIs resolved to signed URLs at every size, or saved
  to disk
- **Account creation** — one-shot or as three explicit steps, with email
  verification

### Under the hood

- Request framing lives in the transport, so no call site can omit it
- Structured error decoding: named `ErrorCodeType`, all seven payload
  variants, per-field validation errors
- Per-endpoint rate-limit cooldowns shared across callers
- Resync-cycling gateway that treats Root's batch-and-close as normal
- LRU state cache fed from traffic; never fetches on a miss
- Request timing counters split into round trip, waiting, and overhead

### Performance

- Login ~950 ms (was ~2,600 ms) — communities expand lazily, and `GetSelf`
  and `ListMine` run in parallel
- Single-message send: one request, no login
- Import ~140 ms / ~30 MB — the generated registries load on first use
- 50 accounts in one process: ~30 MB, versus ~1,490 MB as separate processes

### Protocol findings worth keeping

- Request bodies must be gRPC-web framed; responses must be unwrapped
- `MessageList` must omit `Limit` and always send `DateAt`
- Signup: the Turnstile token is field 3, and the device id must be reused
  across a challenge retry
- Asset links are signed and expire
- `UserOnlineStatus.ACTIVE` is `0x10`; Root has no do-not-disturb state
- Mentions and DMs are pushed; ordinary channel messages are not

### Tests

97 regression tests, each locking in a bug that was expensive to find.
