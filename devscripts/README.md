# devscripts

Tools that aren't part of the library — testing, benchmarking, and the
diagnostics that were written to solve specific protocol puzzles.

For learning the SDK, use `../example.py` instead. These are for when
something is broken or you want to measure it.

Each reads credentials from `tokens.txt` — either in this folder or in the
project root above it (`_paths.py` checks both). It's gitignored.

## Testing

| script | what it does |
|---|---|
| `fullrun.py` | 39-step lifecycle test — join, create, message, verify, clean up. Times every step. The best single check that everything works. |
| `twotest.py` | Two accounts: one sends, one watches. Confirms delivery and measures latency. |
| `monitor.py` | Live monitor — prints every message and event as it arrives. Useful for watching an account in real time. |

## Performance

| script | what it does |
|---|---|
| `benchmark.py` | Eager vs lazy login, per account, alternating order to cancel out warm-up. |
| `logintimes.py` | Times all five login modes (token-only, minimal, lazy, eager, gateway). |
| `sendone.py` | Sends one message in a single request — the fastest possible path. |
| `multihost.py` | Runs several accounts in one process. ~30 MB total vs ~30 MB *each*. |

## Offline static checks

**These need no token and no network.** All four also run inside `pytest -q`;
the scripts exist because a readable report is better than a parametrized
failure when you are working on the thing.

| script | what it answers |
|---|---|
| `covermap.py` | Which of the 230 service methods the live suite does not reach. `--strict` for a lower bound, `--service X` for one service with the tests that reach each method, `--json` to diff between runs. |
| `kwargcheck.py` | Does every `high.<alias>.<method>(...)` in the library send keywords the request message actually has? A wrong one raises `TypeError` before the request is built, so the method has never worked. Found all three `set_*_invite_requirement`. |
| `bindcheck.py` | Would every `client.*` call in the live suite bind to its signature? `**kwargs` forwarders are traced one hop and checked against the wire schema instead. `--all` lists what neither half can verify. |
| `defaultcheck.py` | Any `f(x=x)` forward where the caller's default is falsy and the callee's is `None` — i.e. a wrapper that makes the layer below's carry-forward sentinel unreachable. |
| `gendocs.py` | Regenerates LLMS.md's "Complete surface" section from the live package. No arguments diffs it and exits 1 if stale; `--write` updates it; `--print` emits to stdout. That section is generated because 49 of its signatures had been hand-elided to `*, ...` and 52 public methods were missing outright. |

Run all five before asking anyone to spend a live run:

```bash
python devscripts/kwargcheck.py && python devscripts/bindcheck.py \
  && python devscripts/defaultcheck.py && python devscripts/gendocs.py \
  && python devscripts/covermap.py
```

## Diagnostics

Written to solve specific problems. Kept because the pattern is reusable when
a new RPC misbehaves.

| script | what it does |
|---|---|
| `diagnose.py` | Isolates a failing RPC — tries request shapes and reports which the server accepts. |
| `signupdebug.py` | Dumps a signup request and response field by field, decoding Root's structured errors. |
| `assetdiag.py` | Prints an asset response as a protobuf tree. Found the URL nesting. |

## Other

| script | what it does |
|---|---|
| `useraccounts.py` | Scrapes member profiles from a community into a CSV. |

## fanout.py

One prompt, every token in `tokens.txt`. Type a command and all of them do it
at once — built on `MultiClientHost.broadcast`, which is the only thing in the
SDK that reaches more than one token.

```bash
python devscripts/fanout.py
```

```
> ,send Root Chat hi
[1] ,send -> 3/3 ok  (0.42s)
```

It says how many tokens it found and waits for you to agree before logging any
in. `,help` lists the commands; `,accounts`, `,servers` and `,channels` show
what it can see; `,only` narrows the next command to some of the accounts.

Three things worth knowing:

- **It only writes.** `gateway=False` throughout — nothing here reads packets,
  and skipping the websocket handshake is most of the startup time.
- **Startup scales.** Logins run with a progress bar and ETA, bounded by
  `--login-concurrency` (default 24) rather than by a fixed delay. A `stagger`
  costs `stagger x (N-1)` before the last account has even started — 3.6
  minutes at 1,089 accounts — while a stagger of 0 releases every login at
  once. A ceiling does neither. The index also reuses the community list login
  already fetched, saving a 185ms `ListMine` per token.

**At a few hundred accounts the defaults that matter are the concurrency
ones.** `broadcast` defaults to 8 at a time, which is 28 seconds per command
at 1,089 accounts and a 200ms round trip; this passes `--concurrency`
(default 64, ≈3.8s) instead. The username cache samples the first 25 accounts'
friends rather than sweeping all of them — two round trips each is 2,178
requests otherwise, and anyone missing is looked up on demand anyway.
- **Nothing blocks the prompt.** Each command is dispatched as its own task, so
  you can queue one behind a slow one. Results print tagged with the command
  number they belong to.
- **Names work wherever an id does.** An index of the accounts' communities and
  channels is built in the background at login — one `GetExtended` per
  deduplicated community, reusing the list login already fetched. Usernames the
  index does not hold are resolved on demand through
  `UserGrpcService/FindByUsername` and cached, so `,dm someone hi` works for
  anyone, not just friends.

  **It publishes on the fast majority, not the slowest call.** Across 48
  communities `GetExtended` measured a 0.25s median and a 0.39s p90 — and one
  call at 91.9s that then died of `ReadTimeout`. It was not the same community
  on the next pass, so this is Root being slow on whichever large community it
  feels like, not a bad id. Anything past five seconds is left running and
  folds itself into the live index when it lands; the summary names what is
  still outstanding. Waiting on it instead made the index take as long as the
  single worst response — 27.6s, 48.9s and 76.2s on three otherwise identical
  runs.
- **An inexact match is refused, not guessed at.** 35 of those 553 channel
  names were shared between communities and `general` alone matched eight.
  `FindByUsername` prefix-matches too — `ohphan` comes back as `ohphanim` —
  so a username must match exactly or you get told the candidates. Every
  result line also names what the command actually hit.

`,messageallchannels <community> <message>` (alias `,mac`) is the one command
that turns a line into dozens of writes *per account*. It covers text and
threaded-text channels, skips voice, runs four at a time against Root's
per-account rate limit, and reports `N/M channels`.

## soak.py

Runs two accounts against each other for a bounded period and reports what
drifts. Unlike the other scripts here it *verifies* rather than observes: each
round the sender DMs a nonce and the receiver must see it within a deadline, so
"nothing arrived" and "nothing was sent" are distinguishable.

```bash
python devscripts/soak.py --selftest                        # offline, ~1s
python devscripts/soak.py --stress 300 --reconnect-every 25 # ~4 min
python devscripts/soak.py --minutes 10 --interval 30
python devscripts/soak.py --hours 24
```

Needs `ROOT_TOKEN` and `ROOT_TOKEN2`. With only the first it degrades to
observe-only and says so.

Tracks delivery rate, latency, duplicate deliveries, cache size, background
task count, live object counts and RSS. Writes one JSON object per round to
`soak.jsonl`, flushed as it goes, so a run that dies keeps everything up to
that point. Exit code 1 if anything drifted.

**`--stress` compresses most of it.** Leaks, handle growth and reconnect
behaviour are volume-dependent, not time-dependent: 300 back-to-back rounds
with forced reconnect cycles surfaces in four minutes what an idle run would
take a day to show. Growth is then reported per 1000 rounds rather than per
hour, since "200 reconnects in 90 seconds" is not "8000 per hour".

Only genuinely time-dependent things still need `--hours`: token or session
expiry, a server deploy mid-run, anything keyed to time of day.

**`--selftest` first.** The script calibrates its own sampling against a null
client and reports what the sampling alone costs. Four earlier memory findings
turned out to be the instrument rather than the SDK, so a run now subtracts the
instrument's per-round cost and refuses to call anything a retention leak when
its own counters drift.

## Regenerating the coverage table

The per-service numbers in HANDOFF.md come from an AST scan that resolves every
`client.<service>.<method>` call in `tests/` against the live objects, and
follows the high-level verbs down to the services they delegate to. There is no
script for it; it is a few dozen lines and lives in the git history of this
file's last update.
