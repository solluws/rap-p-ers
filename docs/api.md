# API reference

See also: [errors.md](errors.md) · [accounts.md](accounts.md) · [two-step-signup.md](two-step-signup.md)

For a runnable version of all of this, see [`example.py`](../example.py) —
`python example.py --list` for the sections, `python example.py --token` to
run them.


A map of the surface. Everything is async unless noted.

## Client

```python
client = RootClient(
    token=None,                    # bearer token; enough on its own for RPCs
    command_prefix=">",            # for @client.command
    auto_reconnect=True,
    preload_caches=True,           # load identity + community list at login
    expand_communities=False,      # fetch every community's detail at login
    preload_direct_messages=False,
    contention_pause=0.0,          # stand off on repeated 4016 closes
    contention_threshold=3,
)
```

**`contention_pause`** is for one specific failure that does not resolve
itself. Close code **4016, "Sequence out of range"**, means the hub rejected
your resume cursor — normally harmless (the gateway drops the cursor and the
next attempt succeeds), but if a *second client is live on the same account*
both sides keep advancing that cursor and whichever resumes second is always
out of range. Reconnecting is then actively counterproductive, and the backoff
is not slow enough to save you: it is exponential with *full jitter* — each
delay is a uniform draw from `[0, ceiling]`, and the ceiling stops growing at
`reconnect_max` (30 s by default) from the sixth attempt on. Steady state is
therefore uniform on [0, 30 s] — ~15 s on average, 30 s at worst, so roughly
four cursor fights a minute, indefinitely.

Set it to a number of seconds to stand off after `contention_threshold`
consecutive 4016 closes; the streak resets on any connection that closes
cleanly. It is `0.0` (off) by default, because whether to sit out five minutes
is an application's decision. The gateway dispatches `gateway_paused` (with the
duration) and `gateway_resumed` around the wait, and resumes with a fresh
cursor.

### Proxying

`proxy=` covers **everything**: the gRPC-web API, the gateway websocket, and
the two plain fetches that are not either of those — the remote-image download
behind `change_profile_picture(url)` / `change_banner(url)`, and
`assets.download()` / `.save()` off Root's CDN. Those two used to build their
own `httpx.AsyncClient` and go direct; every client in the package is now built
in `transport.py` and inherits the proxy, and a test fails if a new one appears
anywhere else.

`AccountFactory(proxy=...)` passes it to all three signup paths. Solving the
Turnstile challenge happens in *your* code, so proxying that is on you — a
solve from one address and a signup from another, carrying the same token
minutes apart, is linkable.

`provisioner/proxiedexample.py` runs the whole lifecycle through one proxy
and audits itself; `--selftest` checks the guarantee offline.

Every non-API fetch goes out through one call on the transport *instance* —
not the module, which has no such function. It is the one thing here that is
not a coroutine: it returns a proxied `httpx.AsyncClient` to use as a context
manager.

    client.transport.open_plain_client(**options)

### Session

| method | notes |
|---|---|
| `login_token(token=None)` | authenticate; no websocket |
| `connect()` | open the gateway; returns once up |
| `start_token(token=None)` | login + connect + **blocks forever** |
| `close()` | shut down cleanly |
| `whoami()` | current user |
| `RootClient.send_once(token, container, content, community_id=...)` | classmethod; one request, no login |

### Messages

| method | notes |
|---|---|
| `messages.send(container, content, community_id=..., parent_message_ids=[...])` | |
| `messages.list(container, community_id=..., direction="both", after=None, limit=None)` | one page |
| `messages.history(container, limit=200, page_size=50)` | async iterator, paginates |
| `messages.set_view_time(container, community_id=...)` | mark read |
| `messages.pin_list(container, community_id=...)` | pinned messages |
| `reply(message, content)` · `edit_message(message, content)` · `delete_message(message)` | |
| `reply_to([messages], content, notify=False)` | reply to up to 5 at once |
| `message.reply_with([others], content)` · `channel.reply_to([messages], content)` | same, from an object |
| `react(message, emoji)` · `unreact(message, emoji)` · `pin(message)` · `unpin(message)` | |

Direction values: `"newer"`, `"older"`, `"both"`.

### Objects

```python
channel.send(content)              channel.history()
channel.history_iter(limit=500)    channel.mark_read()
channel.is_text                    channel.mention
channel.edit(**kwargs)             channel.delete()

message.reply(content)             message.edit(content)
message.react(emoji)               message.unreact(emoji)
message.pin()                      message.unpin()
message.delete()                   message.is_dm

dm.send(content)                   dm.history()

member.add_role(role_id)           member.remove_role(role_id)
member.kick()                      member.ban()

role.edit(**kwargs)                role.delete()   role.move(...)
```

### Communities

| method | notes |
|---|---|
| `list_communities()` | your communities |
| `community_detail(id, refresh=False)` | full detail, cached |
| `fetch_community(id)` | always fetches |
| `refresh_communities(expand=True)` | refresh the list |
| `get_members(community, refresh=False)` | every member (cached) |
| `get_member(community, user)` · `member_count(community)` | |
| `members_with_role(community, role)` | filter by role |
| `member_names(community)` | `{user_id: username or None}` (cache-only) |
| `get_members_detailed(community, refresh=False, batch_size=500, concurrency=8)` | members + profiles, batched and fanned out |
| `get_profile(user)` · `get_profiles([ids])` | username, pictures, about-me, status |
| `get_random_member(community, with_profile=True, exclude_self=True)` | one at random with username/pictures, or None |
| `get_random_members(community, count, with_profile=True)` | `count` distinct with profiles (batched), capped at available |
| `extended.channels` · `.text_channels` · `.member(id)` · `.role(id)` | on CommunityExtended |
| `invites.join(code)` · `leave_community(id)` | |
| `community_admin.create_channel(community, group, name, channel_type=1)` | |
| `community_admin.create_role(community, name, ...)` | |
| `community_admin.delete_channel/delete_role/edit_channel/move_channel` | |
| `roles.add_to_members(community, role, [user_ids])` · `remove_from_members(...)` | |

Both `get_members_detailed` defaults were measured against a real
31,520-member community, where the call used to take **60 seconds**. The
numbers below are the run recorded in that method's own docstring
(`highlevel.py`); the changelog's release entry reports a second run of the
same benchmark with slightly different figures.
`batch_size` is how many user ids ride in one profile request: a request costs
~190 ms whether it carries 1 id or 200, so per-id cost falls from 1.96 ms at
100 to 0.5 ms at 1,000 and then flattens — 500 is the knee, and a failed batch
still only costs 500 profiles instead of thousands.

`concurrency` is the knob that actually governs fan-out, and it is the
expensive half: the batches used to be awaited one at a time. HTTP/2
multiplexes them, so issuing 8 at once costs barely more than one — the same
316 requests went from 60.2 s serial to 9.4 s at `concurrency=8`. Together the
two defaults take that community from 316 requests and 60 s to 64 requests and
roughly 2 s. `concurrency=1` restores the old strictly-serial behaviour. A
batch that fails is logged and skipped; those members come back with
`profile=None` rather than costing you the rest.

### UserProfile / DetailedMember

```python
profile.username           profile.about_me          # == .description
profile.avatar_url         # == .profile_picture_uri
profile.banner_uri         profile.custom_status
profile.online_status      profile.is_deleted

# DetailedMember exposes all of the above directly, plus:
member.user_id             member.role_ids
await member.add_role(id)  await member.kick()
```

### Friends, DMs, blocks

| method | notes |
|---|---|
| `friend_requests.send(username)` · `.accept(n)` · `.decline(n)` · `.pending()` | |
| `list_friends()` · `remove_friend(user)` | |
| `open_dm(user)` · `direct_message(user, content)` | |
| `block(user)` · `unblock(user)` · `list_blocked()` | |

### Account

| method | notes |
|---|---|
| `go_online()` · `go_idle()` · `go_invisible()` · `set_online_status(s)` | presence |
| `update_status(text)` | custom status; `None` clears |
| `change_profile_picture(source)` · `remove_profile_picture()` | path / Path / bytes / file object / URL / None |
| `change_banner(source)` | same source types |
| `update_username/description/profile` | |
| `assets.upload_file(path)` · `assets.upload_bytes(data, filename=...)` | returns an asset URI |
| `asset_url(uri)` · `asset_urls([uris])` | `root://asset/...` -> https URL (best size) |
| `get_asset(uri)` | full `Asset`: every size, expiry, animated flag |
| `download_asset(uri)` · `save_asset(uri, path)` | bytes / straight to disk |
| `save_profile_images(profile, dir)` | avatar + banner to files |

Asset links are **short-lived** (`asset.expires_at`) — store bytes, not URLs,
if you need something to last.

### Events

| method | notes |
|---|---|
| `@client.event` | one handler per event, named `on_<event>` |
| `add_listener(name, handler)` · `remove_listener(name, handler)` | many handlers |
| `add_message_listener(handler)` | every message |
| `wait_for(event, check=None, timeout=None)` | returns the event |
| `watch_unread(interval=3.0, include_dms=True)` | see every channel's new messages |
| `watch_channel(id)` · `unwatch_all()` | |

### Timings

```python
client.timing_report()     # readable table
client.timings()           # {"totals": {...}, "endpoints": {...}}
client.reset_timings()     # clear the counters
```

`roundtrip_ms` is network + server; `waiting_ms` is rate-limit/retry holds we
imposed; `overhead_ms` is framing and bookkeeping.

### Cache

```python
client.cache.username(user_id)     # str or None -- never fetches
client.cache.member(community_id, user_id)
client.cache.stats()               # {'users': .., 'hit_rate': .., ...}
```

### Account creation

```python
factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")
```

| call | returns |
|---|---|
| `create(turnstile_token=..., ...)` | `CreatedAccount` |
| `create(..., keep_client=True)` | `(CreatedAccount, RootClient)` |
| `create_return_turnstile(username=..., device_id=...)` | `TurnstileChallenge` (a str URL + context) |
| `create_with_turnstile(turnstile_token=..., challenge=...)` | `CreatedAccount` |
| `create_and_verify(turnstile_token=..., code_provider=..., resend=False)` | `CreatedAccount` |
| `send_verification(account)` · `verify(account, code)` | `None` · `True` |
| `emails()` · `unverified()` · `save(path)` · `load(path)` | list · list · str · list |

`TurnstileChallenge` is a `str` (the URL) carrying `.username`, `.password`,
`.email`, `.device_id`, plus `.context()` and `.describe()`.

`AlreadyCreatedError.account` holds the result when no challenge was needed.

`create_and_verify` does **not** ask for a second email by default. Signup
itself already sends the code, and a resend is gated behind its own Turnstile
challenge (`action=resend_verification`) that the signup token does not
satisfy — so on a fresh account the extra call usually just raises
`TurnstileRequired` and throws away credentials that were already minted. Pass
`resend=True` only when the first email genuinely never arrived, and expect to
solve that challenge.

See [two-step-signup.md](two-step-signup.md).

### Discovery (offline)

```python
client.explain()                          # index of managers and wire services
client.explain("file")                    # a wire service's methods
client.explain("file.search")             # one method's request fields
client.explain("community_files.search")  # a manager method + its wire call
client.preview("message.create", container_id=cid, content="hi")   # encode, don't send
```

`explain` shows the Python signature and the wire fields together; the
manager-to-wire link is read from the code, so it can't drift. `preview` runs
the real encoder — an unknown field or a malformed value raises offline —
without sending anything. Both return an object that prints itself and carries
`.as_dict()`. `client.describe_services(name=None)` is the lower-level listing
`explain` builds on.

### Multiple accounts

```python
host = MultiClientHost.from_tokens([TOKEN_A, TOKEN_B])   # or host.add(name, token, setup=...)
async with host:
    results = await host.broadcast(lambda c: c.invites.join(code))
    # {name: Outcome(name, value=..., error=...)}; never raises
```

One process, one event loop, one shared transport by default (the rate-limit
cooldown table is keyed per account, so a `429` on one does not stall the
others). `broadcast` fans an action out concurrently and returns one `Outcome`
each; `only=`, `concurrency=`, `timeout=` bound it. `host.status()` snapshots
every account; `shared_transport=False` isolates their connection pools.

`max_connections` (default 20) caps whichever **HTTP** pool the accounts use —
the *whole host's* when the transport is shared, *each account's* when it is
not. Its old name `max_connections_per_account` still works and means the same
number; it only ever described the isolated case. It does not bound sockets:
each `gateway=True` account also opens a websocket outside httpx, which is why
`from_tokens` defaults `gateway=False`.

`proxy=` goes on the host, not on `add()` — the proxy lives on the transport,
and the host owns that. `add(proxy=...)` raises rather than proxying an
account's websocket while its API calls go out direct.

A repeat join refuses with `UNAUTHENTICATED (16)` — for an account that is
already a member and for the owner alike — while a bad code gives
`NOT_FOUND (5)`. Match on the exception type, not on `ALREADY_EXISTS`.
