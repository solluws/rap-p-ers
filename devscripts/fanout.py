#!/usr/bin/env python3
"""One prompt, many accounts: type a command and every token does it at once.

    python fanout.py

    > ,send Root Chat hi
    [1] ,send #Chat in Root -> 3/3 ok  (0.42s)

Built on :class:`rootpy.MultiClientHost`, which is the only thing in the SDK
that reaches more than one token -- everything else is bound to a single
client's ``_token_getter``. The host logs every token in, shares one HTTP/2
transport between them, and ``broadcast`` fans one action out concurrently,
returning an ``Outcome`` per account instead of raising.

Three deliberate choices, because they are what make it usable:

**No gateway.** ``gateway=False`` throughout. Nothing here reads packets, and
the websocket handshake is the slowest part of bringing an account up -- so
skipping it is most of the startup time for a fan-out that only writes.
``,presence`` is the one command that has to work around this: what others see
is the lower of a *ceiling* and a *device* status, and it is the gateway that
normally announces the device, so it sends both halves itself. The result
outlives the process -- an account set online here stays online with nothing
running, and ``,presence invisible`` is how it comes back.

That is the *profile* status -- the friends list, the profile card, a DM
header. It is **not** a community's member sidebar, which is attach-gated:
``Member.updateCommunityOnlineStatus`` renders a member Offline unless it is
attached to that community or is your friend, whatever its status says. An
attach does register without a gateway -- the account appears in
``CommunityGetExtendedResponse.AttachedUserIds`` -- but the server drops it
again within 20-30s when there is no hub connection behind it (measured: still
attached at +16s, gone by +31s).

**``,attach <community>`` is the way round that**, and it pays for itself
rather than making startup pay. It opens a socket for the accounts it is
given, attaches them, and :class:`AttachKeeper` keeps them there -- the
subscription dies with the connection, so a socket that drops has to be put
back and re-attached. ``,detach`` drops the socket again when an account is
holding nothing, so an idle fan-out holds no connections at all. The sockets
it does hold have the gateway's four-second resync cycle turned off, since
that exists to deliver messages nothing here reads.

Attaching is visible -- other members see every attached account arrive at
once -- so nothing attaches on its own.

**Nothing blocks the prompt.** Each command is dispatched as its own task and
the reader loops straight back round, so you can queue ``,join`` behind a slow
``,send`` without waiting. Results print when they land, tagged with the
command number they belong to.

**Names, not GUIDs.** ``,send Root Chat hi`` works as well as
``,send 00285411-... 002ddbd2-... hi``. An index of the accounts' communities
and channels is built in the background at startup; anything that does not
parse as a Root GUID is matched against it, longest name first.

That last one is where a fan-out can do real damage, so it errs towards
refusing. A name that matches more than one thing is rejected rather than
guessed at -- of 553 channels across one account's 43 communities, 35 names
were shared between communities and ``general`` alone matched eight. Matching
is exact, then a prefix of the whole name, then a prefix of any one word of
it, and never an arbitrary substring, because ``_span`` walks a line down to
single tokens looking for something that resolves and a substring pass turns
ordinary prose into an id. Every result line names what it actually hit.

Credentials come from ``tokens.txt``, beside this script or in the project
root, in the usual ``name=token`` form (bare tokens, one per line, also work).
``provisioned.json`` -- provisioner/provision.py's ledger, copied here
unchanged -- is read too, and preferred when both exist; it labels each
account with its own username. It says how many it found and which file they
came from, and waits for you to agree before logging any in.
``--proxy socks5://127.0.0.1:1080`` routes every account through a tunnel.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import provisioned_file, tokens_file                 # noqa: E402

from rootpy import MultiClientHost                               # noqa: E402
from rootpy.emoji import normalize_reaction                      # noqa: E402
from rootpy.enums import UserOnlineStatus                        # noqa: E402
from rootpy.exceptions import GrpcFailedPrecondition             # noqa: E402
from rootpy.identifiers import normalize_root_guid               # noqa: E402
from rootpy.models import Message                                # noqa: E402
from rootpy.responses import field as _field                     # noqa: E402

# Root's names are UTF-8 and a Windows pipe is cp1252 by default -- printing a
# community with an emoji in its name raised UnicodeEncodeError before this
# existed. Replace rather than fail: a mangled character in a listing beats a
# dead prompt. stdin matters too, or ,react with a pasted emoji cannot be read.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):              # pragma: no cover
        pass

log = logging.getLogger("fanout")

PREFIX = ","

#: Channel types worth creating from here. Root's enum also has THREADED_TEXT
#: (2) and APP (8); those need setup this script does not do.
CHANNEL_TYPES = {"text": 1, "voice": 4}

#: How many accounts contribute their friend list to the username cache.
#: Every account costs two round trips, and the cache only exists to save a
#: lookup that now works on demand anyway -- so this is a sample, not a sweep.
FRIEND_SAMPLE = 25

#: How long the index waits on the community expansions before publishing what
#: has landed. Measured over 48 communities: median 0.25s, p90 0.39s -- and one
#: outlier at 91.9s that then failed with ReadTimeout. Anything past this is a
#: straggler, and it folds itself in when it arrives rather than holding the
#: other 47 hostage.
EXPAND_DEADLINE = 5.0


def _join_names(names: list[str]) -> str:
    """Name a few, count the rest -- 48 communities would fill the line."""
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
def credentials_file() -> Path:
    """Where to look when ``--tokens`` was not given: the ledger, else tokens.txt."""
    ledger = provisioned_file()
    return ledger if ledger.exists() else tokens_file()


def _ledger_rows(raw: str):
    """provision.py's rows, or ``None`` if this is not that file.

    Decided by content, not by name: ``--tokens`` takes any path, and an
    extension is not evidence of what is inside. A leading brace is the whole
    test -- no token starts with one, and neither does ``name=token``.

    Past that brace the file is a ledger, so a damaged one says so instead of
    falling through. The text reader accepts any line as a bare token, and
    would quietly turn a truncated ledger into one nonsense account rather
    than into the error it is.
    """
    if not raw.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as broken:
        raise SystemExit(f"this looks like provisioned.json but will not "
                         f"parse: {broken}") from broken
    rows = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SystemExit('this is JSON but has no "accounts" list -- '
                         "provision.py writes {\"accounts\": [...]}")
    return rows


def _text_pairs(raw: str):
    """``name=token`` lines, and bare tokens one per line."""
    for number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, token = line.partition("=")
        if not sep:                       # a bare token: numbered on arrival
            yield f"line {number}", None, line
        else:
            yield f"line {number}", label.strip(), token.strip()


def _ledger_pairs(rows: list):
    """provision.py's ledger, one row per account it managed to create."""
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        label = (row.get("username") or "").strip()
        if not label:
            # Every row has an id even when the username did not survive.
            label = (row.get("user_id") or "")[:8]
        yield f"account {index}", label or None, (row.get("token") or "").strip()


def _collect(pairs) -> list[tuple[str, str]]:
    """``[(label, token)]`` in file order, deduped by token, labels unique."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    used: set[str] = set()
    for where, label, token in pairs:
        if not token:
            # In the ledger this is an account whose signup did not finish;
            # the row is still written, so it is expected rather than damage.
            continue
        if token in seen:
            print(f"  {where}: {label or 'it'} repeats an earlier token, skipped")
            continue
        seen.add(token)
        label = label or f"account{len(found) + 1}"
        # Labels are addresses for ,only -- two accounts answering to one name
        # would silently drop one of them from every targeted command. The
        # ledger appends across runs, so a repeat is ordinary there.
        base, suffix = label, 2
        while label in used:
            label, suffix = f"{base}{suffix}", suffix + 1
        used.add(label)
        found.append((label, token))
    return found


def load_tokens(path: Path) -> list[tuple[str, str]]:
    """``[(label, token)]`` from tokens.txt or provisioned.json, in file order.

    Three shapes, because credentials arrive three ways: ``name=token`` (what
    ``_paths.read_token`` reads), one bare token per line (what
    ``benchmark.py`` reads), and provisioner/provision.py's ledger copied here
    unchanged. Labels are what you type at ``,only``, so a named file is worth
    having -- the ledger supplies each account's username for free.
    """
    if not path.exists():
        raise SystemExit(
            f"No credentials at {path}.\n"
            "Either a line per account:\n"
            "    alice=<token>\n"
            "    bob=<token>\n"
            "or copy provisioned.json here as provision.py wrote it."
        )

    raw = path.read_text(encoding="utf-8")
    rows = _ledger_rows(raw)
    if rows is None:
        found = _collect(_text_pairs(raw))
    else:
        found = _collect(_ledger_pairs(rows))
        _note_ledger_proxies(rows)

    if not found:
        raise SystemExit(f"No tokens in {path}.")
    return found


def _note_ledger_proxies(rows: list) -> None:
    """Say what tunnel these were made behind, since the file remembers.

    Accounts created from separate exits and then all logged in from one
    address have had the thing that kept them apart undone, and nothing else
    here would mention it.
    """
    # Only rows that actually contributed an account: a half-made one still
    # records the tunnel it was attempted on, and counting those would claim
    # more exits than there are logins.
    proxies = {row.get("proxy") for row in rows
               if isinstance(row, dict) and row.get("proxy")
               and (row.get("token") or "").strip()}
    if len(proxies) == 1:
        only = next(iter(proxies))
        print(f"  the ledger says these were created behind {only}")
        print(f"  --proxy {only}  keeps them there")
    elif proxies:
        print(f"  the ledger records {len(proxies)} different proxies; --proxy "
              f"takes one for every account, so it cannot reproduce that here")


def mask(token: str) -> str:
    return f"{token[:8]}...{token[-4:]}" if len(token) > 16 else "..."


def confirm(accounts: list[tuple[str, str]], path: Path) -> bool:
    print(f"\n{path.name}: {path}")
    for label, token in accounts:
        print(f"  {label:<16} {mask(token)}")
    print(f"\nThat is {len(accounts)} account(s). Every command you type runs "
          f"as all {len(accounts)}.")
    try:
        answer = input("Log them in? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------
# name -> id
# --------------------------------------------------------------------------
def is_guid(text: str, *, loose: bool = False) -> Optional[str]:
    """The canonical GUID if ``text`` is unmistakably one, else None.

    ``normalize_root_guid`` raises on anything that is not an id, which is
    most of the test needed to tell ``002ddbd2-...`` from ``Chat`` -- and it
    accepts the unhyphenated 32-hex form too, so a copy-paste without dashes
    still resolves.

    It also accepts Root's short form: **any** 22 characters of the base64url
    alphabet decode to a valid id. That is not a shape a name can be told
    apart from. ``GeneralDiscussionRoom1`` is 22 characters with no spaces,
    and ``normalize_root_guid`` turns it into a real, canonical, entirely
    fictional id -- which a fan-out would then send to, as every account, with
    no error to notice. So the short form is only accepted under ``loose``,
    which callers try *after* a name lookup has already failed.
    """
    if not isinstance(text, str):
        return None
    probe = text.strip()
    if not loose:
        # uuid.UUID covers exactly the unambiguous spellings: hyphenated,
        # 32-hex, braced and urn:uuid:. Anything else is a maybe.
        try:
            uuid.UUID(probe)
        except ValueError:
            return None
    try:
        return normalize_root_guid(probe)
    except (TypeError, ValueError):
        return None


class Ambiguous(ValueError):
    """More than one thing answers to that name."""


class NotFound(ValueError):
    """Nothing answers to that name."""


@dataclasses.dataclass
class Entry:
    id: str
    name: str
    parent: Optional[str] = None        # community id, for channels/categories
    kind: int = 0


#: Shortest run that may match by anything but an exact name. An exact match
#: is always allowed, however short. Without a floor, ",send Root a message
#: here" finds "a" inside "Chat" and sends "message here" to it.
MIN_FUZZY = 3

PLURAL = {
    "community": "communities", "channel": "channels",
    "category": "categories", "user": "users",
}

_SEPARATORS = re.compile(r"[\s\-_./\\|:,()\[\]]+")


def _words(name: str) -> list[str]:
    """The parts of a name a person would consider separate words.

    Channel names are routinely ``general-voice``, ``off topic`` or
    ``| chat``, and typing one word of that has to work. Each part is
    also offered with leading non-word characters stripped, so ``chat``
    finds a channel actually named ``chat`` behind an emoji.
    """
    out = []
    for part in _SEPARATORS.split(name.casefold()):
        if not part:
            continue
        out.append(part)
        bare = re.sub(r"^\W+", "", part, flags=re.UNICODE)
        if bare and bare != part:
            out.append(bare)
    return out


def _match(name: str, entries: list[Entry], what: str, describe=None, *,
           fuzzy: bool = True) -> Entry:
    """Case-insensitive exact, else unique prefix, else unique substring.

    Narrowest first so an exact name always beats a longer one that merely
    contains it -- with channels called ``general`` and ``general-voice``,
    typing ``general`` has to mean the one actually called that.

    ``fuzzy=False`` restricts it to the exact pass, for slots that are
    optional: a run of tokens is only allowed to *disappear* into an optional
    slot when it names that slot outright.
    """
    wanted = name.casefold().strip()
    if not wanted:
        raise NotFound(f"no {what} named ''")

    # Exact, then a prefix of the whole name, then a prefix of any one word of
    # it. Deliberately NOT "contains anywhere": _span walks a line down to its
    # individual tokens looking for something that resolves, so an arbitrary
    # substring pass turns ordinary prose into an id. Measured: with a channel
    # called "share", ",send Root are we meeting today" matched "are" inside
    # "share" and delivered "we meeting today" there, as every account, with
    # nothing on screen to say so. Word-prefix keeps every case that pass was
    # for -- "voice" for "general-voice", "chat" for an emoji-prefixed name --
    # and drops the one that reads prose.
    tests = [lambda e: e.name.casefold() == wanted]
    if fuzzy and len(wanted) >= MIN_FUZZY:
        tests += [
            lambda e: e.name.casefold().startswith(wanted),
            lambda e: any(w.startswith(wanted) for w in _words(e.name)),
        ]

    for test in tests:
        hits = [e for e in entries if test(e)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # Several matches, but they may all be the same thing seen from
            # different accounts; one id is not an ambiguity.
            by_id = {e.id: e for e in hits}
            if len(by_id) == 1:
                return hits[0]
            # Listing the names is worthless when the collision IS the name --
            # two channels both called "general" print as "general, general".
            # describe() says where each one lives instead.
            shown = "\n      ".join(
                (describe(e) if describe else f"{e.name}  {e.id}")
                for e in list(by_id.values())[:8]
            )
            advice = ("name the community too, or use an id"
                      if what in ("channel", "category") else "use an id")
            raise Ambiguous(
                f"{name!r} matches {len(by_id)} {PLURAL.get(what, what)} "
                f"-- {advice}:\n      {shown}"
            )
    raise NotFound(f"no {what} matching {name!r}")


class Index:
    """What the accounts can see, keyed by name.

    Built once in the background after login and refreshed with ``,refresh``.
    The account list is the *union* across every token: two accounts in
    different communities give one index covering both, and a command naming
    something only one account can reach simply fails for the others -- which
    is a per-account ``Outcome``, not an error.
    """

    def __init__(self) -> None:
        self.communities: list[Entry] = []
        self.channels: dict[str, list[Entry]] = {}
        self.categories: dict[str, list[Entry]] = {}
        self.users: list[Entry] = []
        self._user_ids: set[str] = set()
        self.ready = False
        self.building = False
        self._slow: set[asyncio.Task] = set()

    def track(self, task: asyncio.Task) -> asyncio.Task:
        """A bare create_task is only weakly held by the loop."""
        self._slow.add(task)
        task.add_done_callback(self._slow.discard)
        return task

    async def _settle(self, pending, named, channels) -> None:
        """Fold the stragglers in once the server gets around to them."""
        before = sum(len(v) for v in channels.values())
        await asyncio.wait(pending)
        gained = sum(len(v) for v in channels.values()) - before
        late = _join_names(sorted(named[t] for t in pending))
        OUT.say(
            f"    {late}: +{gained} channels" if gained
            else f"    {late}: nothing came back"
        )

    # -- lookups ------------------------------------------------------- #
    # Each of these is: unmistakable id, then name, then the ambiguous
    # 22-character id form. Names go in the middle deliberately -- see
    # is_guid. Ambiguous is never caught here; it means the name was real.
    def _short_id(self, text: str) -> Optional[str]:
        """The 22-char id form, but only once names have really been checked.

        ``_require`` reports "not indexed yet" as a NotFound, which is the
        same exception a wrong name raises -- so without this guard, a
        22-character name typed during the three seconds the index is still
        building would skip the name lookup entirely and resolve to a
        fabricated id. Never fall back to guessing while the thing that would
        have disagreed has not loaded.
        """
        return is_guid(text, loose=True) if self.ready else None

    def community(self, name: str) -> str:
        found = is_guid(name)
        if found:
            return found
        try:
            self._require("communities")
            return _match(name, self.communities, "community").id
        except NotFound:
            found = self._short_id(name)
            if found is None:
                raise
            return found

    def channel(self, name: str, community_id: Optional[str]) -> Entry:
        found = is_guid(name)
        if found:
            return Entry(id=found, name=name, parent=community_id)
        try:
            self._require("channels")
            if community_id is not None:
                return _match(
                    name, self.channels.get(community_id, []), "channel"
                )
            everywhere = [c for group in self.channels.values() for c in group]
            return _match(name, everywhere, "channel", self.where)
        except NotFound:
            found = self._short_id(name)
            if found is None:
                raise
            return Entry(id=found, name=name, parent=community_id)

    def describe(self, kwargs: dict) -> str:
        """Where a parsed command is actually aimed, in words.

        Printed with every result. Fuzzy name matching means the thing you
        typed and the thing you hit are not always the same, and until this
        existed there was nothing between parsing and firing at N accounts
        that showed which one won.
        """
        container = kwargs.get("container_id")
        community = kwargs.get("community_id")

        who = ""
        user = kwargs.get("user_id")
        if user:
            who = next(
                (f"@{e.name}" for e in self.users if e.id == user), user
            )
        elif kwargs.get("_username"):
            who = f"@{kwargs['_username']}"

        where = ""
        if container:
            where = str(container)
            for group in self.channels.values():
                for entry in group:
                    if entry.id == container:
                        where = (f"#{entry.name} in "
                                 f"{self.community_name(entry.parent)}")
                        break
        elif community:
            where = self.community_name(community)

        # ",memberinvite bob My Server" is about both halves; everything else
        # has only one of them.
        if who and where:
            return f"{who} -> {where}"
        return who or where

    def community_name(self, community_id: Optional[str]) -> str:
        return next(
            (c.name for c in self.communities if c.id == community_id),
            community_id or "?",
        )

    def where(self, entry: Entry) -> str:
        """``#name in Community`` -- how a channel is told from its namesake."""
        return (f"{entry.name}  in {self.community_name(entry.parent)}"
                f"   {entry.id}")

    def category(self, name: str, community_id: str, *,
                 fuzzy: bool = True) -> str:
        found = is_guid(name)
        if found:
            return found
        try:
            return _match(
                name, self.categories.get(community_id, []), "category",
                fuzzy=fuzzy,
            ).id
        except NotFound:
            found = self._short_id(name)
            if found is None:
                raise
            return found

    def user(self, name: str) -> str:
        found = is_guid(name)
        if found:
            return found
        try:
            self._require("users")
            return _match(name, self.users, "user").id
        except NotFound:
            found = self._short_id(name)
            if found is None:
                raise
            return found

    def _require(self, what: str) -> None:
        if self.ready:
            return
        raise NotFound(
            f"the {what} index is not built yet"
            + (" (building now -- try again in a moment)" if self.building
               else " -- run ,refresh")
        )

    # -- building ------------------------------------------------------ #
    async def build(self, host: MultiClientHost, *,
                    refresh: bool = True) -> str:
        """One GetExtended per community, deduplicated across the accounts.

        A community three tokens share costs one round trip, not three, and
        the expansion carries the channels, the categories and the members --
        so this is a single pass for all three.

        ``refresh=False`` skips the ListMine per account, because login has
        already done exactly that: ``preload_caches`` defaults to True and
        fills ``client.communities`` before the client is handed back.
        Repeating it measured 185 ms per account, paid once per token at
        startup for an answer already in memory. ``,refresh`` passes True,
        which is the case that genuinely wants fresh data.
        """
        self.building = True
        try:
            clients = [
                account.client
                for account in host.accounts.values()
                if account.ready and account.client is not None
            ]
            if not clients:
                return "no accounts are up"

            communities: dict[str, Entry] = {}
            owners: dict[str, object] = {}
            # Which clients actually need asking. NOT "those whose list is
            # empty": an account in no communities has an empty list as its
            # correct answer, and testing for emptiness turned this into a
            # sequential ListMine for every such account -- ~1,000 of them at
            # 1,089 fresh accounts, measured at 27.6s of the index build for
            # 48 communities. preload_caches says whether login already asked,
            # which is the actual question.
            stale = [
                c for c in clients
                if refresh or not getattr(c, "preload_caches", True)
            ]
            if stale:
                listing = asyncio.Semaphore(16)

                async def relist(client) -> None:
                    async with listing:
                        try:
                            await client.refresh_communities(expand=False)
                        except Exception as exc:               # noqa: BLE001
                            log.debug("list_mine failed: %r", exc)

                await asyncio.gather(*(relist(c) for c in stale))

            for client in clients:
                for community in client.communities.values():
                    communities.setdefault(
                        community.id, Entry(id=community.id, name=community.name)
                    )
                    owners.setdefault(community.id, client)

            limiter = asyncio.Semaphore(16)
            channels: dict[str, list[Entry]] = {}
            categories: dict[str, list[Entry]] = {}

            async def expand(community_id: str) -> None:
                async with limiter:
                    try:
                        extended = await owners[community_id].fetch_community(
                            community_id
                        )
                    except Exception as exc:                   # noqa: BLE001
                        log.debug("expand %s failed: %r", community_id, exc)
                        return
                for group in extended.channel_groups:
                    categories.setdefault(community_id, []).append(
                        Entry(id=group.id, name=group.name, parent=community_id)
                    )
                    for channel in group.channels:
                        channels.setdefault(community_id, []).append(
                            Entry(
                                id=channel.id,
                                name=channel.name,
                                parent=community_id,
                                kind=getattr(channel, "channel_type", 0) or 0,
                            )
                        )

            # Publish on the fast majority, not on the slowest call. Root's
            # GetExtended for a large community is wildly variable: measured
            # across 48 communities the median was 0.25s and the p90 0.39s,
            # while one call took 91.9s and then died of ReadTimeout. It was
            # not the same community twice -- whichever big one the server is
            # slow on that run becomes the straggler. Waiting on it made the
            # whole index take as long as the worst single response.
            #
            # ``channels``/``categories`` are handed out by reference below,
            # so a late expansion appends straight into the live index and is
            # usable the moment it lands.
            named = {
                asyncio.create_task(expand(cid)): entry.name
                for cid, entry in communities.items()
            }
            pending: set[asyncio.Task] = set()
            if named:   # accounts in no communities at all: nothing to wait on
                _, pending = await asyncio.wait(
                    named.keys(), timeout=EXPAND_DEADLINE
                )

            self.communities = sorted(
                communities.values(), key=lambda e: e.name.casefold()
            )
            self.channels = channels
            self.categories = categories
            self.users = await self._build_users(clients)
            self._user_ids = {e.id for e in self.users}
            self.ready = True

            if pending:
                self.track(asyncio.ensure_future(
                    self._settle(pending, named, channels)
                ))

            sampled = min(len(clients), FRIEND_SAMPLE)
            return (
                f"{len(self.communities)} communities, "
                f"{sum(len(v) for v in channels.values())} channels, "
                f"{len(self.users)} users"
                + ("" if sampled >= len(clients)
                   else f" (friends of {sampled} of {len(clients)} accounts)")
                + ("" if not pending
                   else f"; still expanding "
                        + _join_names(sorted(named[t] for t in pending)))
            )
        finally:
            self.building = False

    def remember_user(self, user_id: str, username: str) -> None:
        # Membership by set, not a scan, and no re-sort per insert: at a
        # thousand accounts this is called often enough for both to matter.
        if user_id in self._user_ids:
            return
        self._user_ids.add(user_id)
        self.users.append(Entry(id=user_id, name=username))

    @staticmethod
    async def _build_users(clients) -> list[Entry]:
        """Friends, resolved to usernames in one batched profile call each.

        Deliberately friends only. Community members carry a ``user_id`` and
        nothing else -- ``CommunityMember`` has no username field -- so
        indexing everyone the accounts can see would mean a profile lookup per
        member across every community.

        This is a *cache*, not the whole answer: anyone not in it is looked up
        on demand by :func:`resolve_username`, which is why naming a stranger
        works. Having friends here just makes the common case free.

        Which is why it samples rather than sweeps. This used to walk every
        client with two sequential round trips each -- fine at three accounts,
        2,178 requests and roughly seven minutes at 1,089. The hundredth
        account's friend list buys almost nothing that the first twenty have
        not already supplied, and anyone missing still resolves on demand.
        """
        sample = clients[:FRIEND_SAMPLE]
        limiter = asyncio.Semaphore(8)
        found: dict[str, Entry] = {}

        async def one(client) -> None:
            async with limiter:
                try:
                    ids = list(await client.friends.friend_ids())
                    if not ids:
                        return
                    profiles = await client.users.get_profiles(ids)
                except Exception as exc:                       # noqa: BLE001
                    log.debug("user index failed: %r", exc)
                    return
            for user_id, profile in (profiles or {}).items():
                username = _field(profile, "username")
                if username:
                    found.setdefault(
                        str(user_id), Entry(id=str(user_id), name=str(username))
                    )

        await asyncio.gather(*(one(c) for c in sample))
        return sorted(found.values(), key=lambda e: e.name.casefold())


INDEX = Index()


async def resolve_username(client, name: str) -> str:
    """Ask the server for a user id, for anyone at all.

    The startup index only knows *friends*, because ``CommunityMember`` carries
    a ``user_id`` and no username -- so naming anyone else used to be met with
    "could not find a user", even though Root has an RPC for exactly this:
    ``UserGrpcService/FindByUsername``, one string in, a short user list back.

    Prefers an exact casefolded match, since the server answers with anything
    resembling the name and picking the first row would DM a stranger with a
    similar handle. The result is cached in the index, so asking twice costs
    one round trip.
    """
    wanted = name.strip().lstrip("@")
    if not wanted:
        raise NotFound("expected a username")
    # Root's own limit. Without this the server answers a 24-character name
    # with a paragraph of INVALID_ARGUMENT validator prose, which is a worse
    # way to learn you typed the message into the username slot.
    if len(wanted) > 20:
        raise NotFound(
            f"{wanted[:20]}... is too long to be a username "
            "(Root allows 20 characters)"
        )
    try:
        reply = await client.high.user.find_by_username(username=wanted)
    except Exception as exc:                                   # noqa: BLE001
        raise NotFound(f"could not look up {wanted!r}: "
                       f"{type(exc).__name__}: {exc}") from exc

    rows = _field(reply.data, "users") or []
    if not rows:
        raise NotFound(f"no user called {wanted!r}")

    # FindByUsername prefix-matches: asking for "ohphan" comes back with the
    # single row "ohphanim". Taking a lone inexact row would mean ",dm bob hi"
    # quietly DMs bobby123 -- a private message to a person you did not name.
    # Name them instead and let the next keystroke be deliberate.
    exact = [
        r for r in rows
        if str(_field(r, "username") or "").casefold() == wanted.casefold()
    ]
    if not exact:
        names = ", ".join(str(_field(r, "username")) for r in rows[:6])
        raise NotFound(
            f"no user is exactly {wanted!r} -- did you mean: {names}"
        )
    if len(exact) > 1:
        raise Ambiguous(f"{wanted!r} matched {len(exact)} users -- use a user id")

    user_id = str(_field(exact[0], "user_id") or "")
    username = str(_field(exact[0], "username") or wanted)
    if not user_id:
        raise NotFound(f"no user id came back for {wanted!r}")
    INDEX.remember_user(user_id, username)
    return user_id


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def tokenize(text: str) -> list[tuple[str, int]]:
    """``[(token, index_just_past_it)]``, honouring quotes.

    The offsets are the point: a ``text`` argument is the *rest of the line*
    taken raw, so ``,send Root Chat it's "fine"`` sends exactly that and not
    some requoted version of it.
    """
    out: list[tuple[str, int]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] in "\"'":
            quote, i, buf = text[i], i + 1, []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1                                    # past the closing quote
            out.append(("".join(buf), i))
        else:
            start = i
            while i < n and not text[i].isspace():
                i += 1
            out.append((text[start:i], i))
    return out


def _rest(raw: str, cursor: int) -> str:
    """Everything after ``cursor``, unwrapped if it is one quoted string."""
    text = raw[cursor:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] not in inner:
            return inner
    return text


def _span(
    tokens: list[tuple[str, int]], start: int, longest: int, resolve
):
    """Try ``resolve`` on the longest run of tokens first, then shorter.

    Longest-first is what lets a community called "My Server" win over one
    called "My": a shorter match found first would swallow the wrong name and
    then fail on the argument after it, with a confusing error.

    ``NotFound`` means "that is not a name, try a shorter run". ``Ambiguous``
    does **not**, and is left to propagate: it means the run really is a name,
    just not a unique one, and quietly falling back to a shorter run would
    resolve to a *different* channel and send there. One clarifying keystroke
    beats a message delivered to the wrong place by every account at once.
    """
    for length in range(longest, 0, -1):
        if start + length > len(tokens):
            continue
        name = " ".join(t for t, _ in tokens[start:start + length])
        try:
            value = resolve(name)
        except NotFound:
            continue
        return value, start + length, tokens[start + length - 1][1]
    return None, start, tokens[start][1] if start < len(tokens) else 0


def parse_container(raw: str, *, need_after: int) -> tuple[str, Optional[str], int]:
    """``(container_id, community_id, cursor)`` from ``<community> <channel>``.

    The community may be left out when the channel name is unambiguous across
    everything the accounts can see, so ``,send general hello`` works. That is
    a real convenience and a real trap, so it is only taken when naming a
    community outright did not work.
    """
    tokens = tokenize(raw)
    if not tokens:
        raise NotFound("expected a channel")

    budget = len(tokens) - need_after
    if budget < 1:
        raise NotFound("expected a channel and then more arguments")

    community_id, at, _ = _span(tokens, 0, max(1, budget - 1), INDEX.community)
    if community_id is not None:
        channel, after, cursor = _span(
            tokens, at, max(1, budget - at),
            lambda name: INDEX.channel(name, community_id),
        )
        if channel is not None:
            return channel.id, community_id, cursor

        # The community was named and understood, and none of ITS channels
        # matched. Falling through to the bare-channel search below would
        # restart from token 0 and match the community's own name against a
        # channel in some *other* community -- ",send Root caht hi" landing in
        # a "root-standup" channel elsewhere, as every account. A typo must
        # not be able to redirect the target, so this stops here.
        home = INDEX.community_name(community_id)
        typo = tokens[at][0] if at < len(tokens) else ""
        known = ", ".join(
            e.name for e in (INDEX.channels.get(community_id) or [])[:8]
        )
        raise NotFound(
            f"{home}: no channel matching {typo!r}"
            + (f" -- it has: {known}" if known else " -- no channels indexed")
        )

    # No community named at all: try the channel name on its own and take the
    # community from wherever it lives.
    channel, at, cursor = _span(
        tokens, 0, budget, lambda name: INDEX.channel(name, None)
    )
    if channel is not None:
        return channel.id, channel.parent, cursor

    # _span swallows NotFound to try a shorter span, so an unbuilt index looks
    # identical to a wrong name from in here. Say which it was.
    INDEX._require("channels")
    raise NotFound(
        f"could not find a channel in {raw.strip()!r} "
        "-- try ,servers and ,channels <community>"
    )


# Each parser turns the raw argument string into kwargs for its action.
def p_none(raw: str) -> dict:
    if raw.strip():
        raise NotFound("this command takes no arguments")
    return {}


def p_text(raw: str) -> dict:
    text = _rest(raw, 0)
    if not text:
        raise NotFound("expected some text")
    return {"text": text}


def p_word(raw: str) -> dict:
    tokens = tokenize(raw)
    if len(tokens) != 1:
        raise NotFound("expected exactly one word")
    return {"word": tokens[0][0]}


def p_presence(raw: str) -> dict:
    # Resolve the name here rather than in the action: a typo should cost one
    # line before anything is sent, not N identical ValueErrors after every
    # account has already been asked.
    tokens = tokenize(raw)
    if len(tokens) != 1:
        raise NotFound("expected exactly one word")
    try:
        return {"status": UserOnlineStatus.from_name(tokens[0][0])}
    except ValueError:
        raise NotFound(
            f"{tokens[0][0]!r} is not a presence -- use online, idle or "
            "invisible (active, away, inactive, offline and disconnected "
            "work too). Root has no do-not-disturb state."
        ) from None


def p_container_text(raw: str) -> dict:
    container_id, community_id, cursor = parse_container(raw, need_after=1)
    content = _rest(raw, cursor)
    if not content:
        raise NotFound("expected a message after the channel")
    return {
        "container_id": container_id,
        "community_id": community_id,
        "content": content,
    }


def p_container_message_text(raw: str) -> dict:
    container_id, community_id, cursor = parse_container(raw, need_after=2)
    tokens = tokenize(raw[cursor:])
    if not tokens:
        raise NotFound("expected a message id")
    message_id = is_guid(tokens[0][0])
    if message_id is None:
        raise NotFound(f"{tokens[0][0]!r} is not a message id")
    content = _rest(raw[cursor:], tokens[0][1])
    if not content:
        raise NotFound("expected something after the message id")
    return {
        "container_id": container_id,
        "community_id": community_id,
        "message_id": message_id,
        "content": content,
    }


def p_react(raw: str) -> dict:
    """Like a reply, but the trailing text has to be one emoji.

    Checked here rather than left to the server: add_reaction normalizes the
    reaction itself and raises, which would otherwise surface as the same
    ValueError once per account after N round trips.
    """
    parsed = p_container_message_text(raw)
    try:
        normalize_reaction(parsed["content"])
    except (TypeError, ValueError) as exc:
        raise NotFound(str(exc)) from exc
    return parsed


def p_container(raw: str) -> dict:
    container_id, community_id, _ = parse_container(raw, need_after=0)
    return {"container_id": container_id, "community_id": community_id}


def p_community(raw: str) -> dict:
    if not raw.strip():
        raise NotFound("expected a community")
    return {"community_id": INDEX.community(raw.strip())}


def p_community_text(raw: str) -> dict:
    tokens = tokenize(raw)
    if len(tokens) < 2:
        raise NotFound("expected a community and then some text")
    community_id, _, cursor = _span(
        tokens, 0, len(tokens) - 1, INDEX.community
    )
    if community_id is None:
        raise NotFound(f"could not find a community in {raw.strip()!r}")
    text = _rest(raw, cursor)
    if not text:
        raise NotFound("expected some text after the community")
    return {"community_id": community_id, "text": text}


def p_community_maybe_int(raw: str) -> dict:
    tokens = tokenize(raw)
    if not tokens:
        raise NotFound("expected a community")
    uses = None
    # isascii() as well as isdigit(): str.isdigit() is True for superscripts
    # and other No-category digits that int() then refuses, so the guard would
    # pass and the conversion raise a bare ValueError out of the command.
    last = tokens[-1][0]
    if len(tokens) > 1 and last.isascii() and last.isdigit():
        uses, tokens = int(last), tokens[:-1]
    name = " ".join(t for t, _ in tokens)
    return {"community_id": INDEX.community(name), "max_uses": uses}


def p_new_channel(raw: str) -> dict:
    """``<community> [category] <name> [text|voice]``.

    The category and the type are both optional and both resolved by trying:
    a trailing ``text``/``voice`` is the type, a leading run that names an
    existing category is the category, and whatever is left is the new
    channel's name.
    """
    tokens = tokenize(raw)
    if len(tokens) < 2:
        raise NotFound("expected a community and a name")

    kind = 1
    if tokens[-1][0].casefold() in CHANNEL_TYPES and len(tokens) > 2:
        kind, tokens = CHANNEL_TYPES[tokens[-1][0].casefold()], tokens[:-1]

    community_id, at, _ = _span(tokens, 0, len(tokens) - 1, INDEX.community)
    if community_id is None:
        raise NotFound(f"could not find a community in {raw.strip()!r}")

    group_id = None
    if len(tokens) - at > 1:
        # fuzzy=False: the category slot is optional, so a run of tokens is
        # only allowed to vanish into it by naming it outright. Fuzzily,
        # ",newchannel Root general chat" would see "general" prefix-match a
        # category called "General" and quietly create a channel named "chat".
        group_id, at, _ = _span(
            tokens, at, len(tokens) - at - 1,
            lambda name: INDEX.category(name, community_id, fuzzy=False),
        )
    if group_id is None:
        groups = INDEX.categories.get(community_id) or []
        if not groups:
            raise NotFound(
                "that community has no categories indexed -- name one "
                "explicitly, or run ,refresh"
            )
        group_id = groups[0].id

    name = " ".join(t for t, _ in tokens[at:]).strip()
    if not name:
        raise NotFound("expected a name for the new channel")
    return {
        "community_id": community_id,
        "channel_group_id": group_id,
        "name": name,
        "channel_type": kind,
    }


def p_user_text(raw: str) -> dict:
    tokens = tokenize(raw)
    if len(tokens) < 2:
        raise NotFound("expected a user and then a message")

    user_id, _, cursor = _span(tokens, 0, len(tokens) - 1, INDEX.user)
    if user_id is not None:
        content = _rest(raw, cursor)
        if not content:
            raise NotFound("expected a message after the user")
        return {"user_id": user_id, "content": content}

    # Neither an id nor anyone the index knows. Hand the first token off for a
    # server lookup rather than refusing: the index holds friends only, and
    # most people you want to DM are not friends yet. The first token is
    # enough because Root usernames are a single word.
    content = _rest(raw, tokens[0][1])
    if not content:
        raise NotFound("expected a message after the user")
    return {"_username": tokens[0][0], "content": content}


def p_user_community(raw: str) -> dict:
    """``<username> <community>`` -- the order ,memberinvite is written in."""
    tokens = tokenize(raw)
    if len(tokens) < 2:
        raise NotFound("expected a username and then a community")

    # The community is everything after the username, so it may be several
    # words; the username never is.
    community_id = INDEX.community(" ".join(t for t, _ in tokens[1:]))
    name = tokens[0][0]

    found = is_guid(name)
    if found:
        return {"user_id": found, "community_id": community_id}
    try:
        return {"user_id": INDEX.user(name), "community_id": community_id}
    except NotFound:
        return {"_username": name, "community_id": community_id}


# --------------------------------------------------------------------------
# what the accounts actually do
# --------------------------------------------------------------------------
async def a_send(client, *, container_id, community_id, content):
    return await client.messages.send(
        container_id, content, community_id=community_id
    )


async def a_reply(client, *, container_id, community_id, message_id, content):
    # needs_parent_notification defaults to False on messages.send, which
    # makes a reply land without pinging the person replied to. That is a
    # sensible library default and the wrong one here: ,reply is typed by a
    # human who means it as a reply. client.dm.reply sets it the same way.
    return await client.messages.send(
        container_id, content, community_id=community_id,
        parent_message_ids=[message_id], needs_parent_notification=True,
    )


async def a_react(client, *, container_id, community_id, message_id, content):
    # add_reaction reads only id / container_id / community_id off the
    # message; the other two fields are required by the dataclass and unused.
    target = Message(
        id=message_id, container_id=container_id, user_id="", content="",
        community_id=community_id,
    )
    return await client.messages.add_reaction(target, content)


async def a_dm(client, *, user_id, content):
    return await client.dm.send(user_id, content)


async def a_member_invite(client, *, community_id, user_id):
    # role_ids deliberately not passed: this invites, it does not also hand
    # out permissions in the same call.
    return await client.moderation.invite_user(community_id, user_id)


#: Channel types a message can be sent to. Root's ChannelType: TEXT=1,
#: THREADED_TEXT=2, VOICE=4, APP=8.
TEXTLIKE = (1, 2)


async def a_message_all(client, *, community_id, text):
    """Send one message to every text channel of a community.

    Bounded concurrency rather than a flat gather: this is the one command
    here that turns a single line into dozens of writes per account, and
    Root rate-limits per account per endpoint. Failures are counted rather
    than raised, so one locked channel does not hide the rest of the result.
    """
    targets = [
        e for e in (INDEX.channels.get(community_id) or [])
        if e.kind in TEXTLIKE
    ]
    if not targets:
        raise RuntimeError(
            "no text channels indexed for that community -- try ,refresh"
        )

    limiter = asyncio.Semaphore(4)
    done: list[str] = []
    failed: list[str] = []

    async def one(entry: Entry) -> None:
        async with limiter:
            try:
                await client.messages.send(
                    entry.id, text, community_id=community_id
                )
                done.append(entry.name)
            except Exception as exc:                           # noqa: BLE001
                failed.append(f"#{entry.name} {type(exc).__name__}")

    await asyncio.gather(*(one(e) for e in targets))

    summary = f"{len(done)}/{len(targets)} channels"
    if failed:
        shown = "; ".join(failed[:3])
        if len(failed) > 3:
            shown += f"; +{len(failed) - 3} more"
        summary += f"  (failed: {shown})"
    return summary


async def a_join(client, *, word):
    # age_verified=True sends IsAgeVerified on CommunityMemberInviteLinkJoin.
    # An age-restricted community refuses the join with FAILED_PRECONDITION
    # without it, and the assertion is about the account holder, who is
    # whoever is running this.
    try:
        return await client.invites.join(word, age_verified=True)
    except GrpcFailedPrecondition as exc:
        # Root gates joins on more than one condition and answers all of them
        # with the same bare code, so a second FAILED_PRECONDITION here is a
        # *different* problem -- almost always a community with
        # reject_unverified_email set and an account whose email is not
        # verified. Say so, rather than let it look like the age flag failed.
        raise RuntimeError(
            "refused even with 18+ asserted -- the usual remaining cause is "
            "an unverified email on this account, for a community that "
            f"requires one ({exc})"
        ) from exc


async def a_leave(client, *, community_id):
    return await client.community_service.leave(community_id)


#: Opening a hub socket is the slowest thing here by an order of magnitude --
#: ~6s of server-side latency each, against ~200-600ms for any RPC -- so this
#: is the number that decides how long ,attach takes.
#:
#: Measured against the real ledger, connects per second by gate width:
#: 8 -> 6.1, 24 -> 9.9, 64 -> 15.2, 128 -> 16.7, 256 -> 17.0, 512 -> 17.2.
#: The hub saturates at ~17/s; past that the gate only inflates per-connect
#: latency (6s at 120 accounts in flight, 14s at 250) without delivering more.
#:
#: Handshakes in flight are what make every other call slow -- the same
#: Attach costs 248ms alone and 1157ms alongside them -- but ``ATTACHING`` is
#: what keeps the two apart now, not this. Sized narrow it only makes the
#: ramp crawl: at 16, a 1,307-account fleet took on sockets at 4.4/s, a
#: quarter of what the hub offers.
HANDSHAKES = asyncio.Semaphore(64)

#: ,attach is capped by the server, not by us: measured over 400 accounts,
#: throughput is 5.9/s at 16 in flight, 5.9/s at 32 and 6.3/s at 64 -- flat --
#: while the Attach round trip inflates 969ms -> 1794ms -> 5769ms across the
#: same range. The extra concurrency buys nothing and costs latency, and a
#: 17s round trip at full scale is uncomfortably close to broadcast's 45s
#: timeout. So this holds the pipeline near the knee regardless of what
#: --concurrency is set to, which is still free to be wide for everything
#: else. It is a robustness win, not a speed one: ~6/s is the server's number.
ATTACHES = asyncio.Semaphore(24)


class Busy:
    """A counter of commands in flight, so background work can stand aside.

    Opening sockets and issuing attaches are cheap separately and expensive
    together: an Attach costs 248ms on its own and 1157ms next to a handshake
    storm. The keeper's socket work is never urgent -- a refresh holds the
    member-list place meanwhile -- so it waits for the command to finish
    rather than competing with it. Measured at 400 accounts, letting them
    overlap turned a 4.4s attach into 76.6s.
    """

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "Busy":
        self.count += 1
        return self

    def __exit__(self, *exc) -> None:
        self.count -= 1

    def __bool__(self) -> bool:
        return self.count > 0


ATTACHING = Busy()


async def announce_device_once(client) -> None:
    """Send SetDeviceOnlineStatus for this account, at most once per run.

    Presence is the lower of a ceiling and this device status, so it has to
    be sent -- but only once: it persists server-side, and ``connect()``
    re-sends it on any socket it opens with the default announce. Sending it
    per command doubled the round trips of the commonest one.
    """
    if getattr(client, "_fanout_device_announced", False):
        return
    await client.user_settings.set_device_online_status(
        int(UserOnlineStatus.ACTIVE)
    )
    client._fanout_device_announced = True


async def hold_socket(client) -> None:
    """Bring one account's gateway up, tuned for presence rather than reading.

    Two things, both about not paying for message delivery nobody reads:

    **The socket is opened here, not at startup.** Logging in is one round
    trip; adding a websocket handshake to it roughly doubles the cost of
    bringing an account up (measured: login 1.1s, connect 1.0s), and a
    fan-out that never attaches would pay it for every account for nothing.

    **``resync_interval`` is turned off.** The gateway closes and reopens the
    socket every four seconds by design -- that is how channel messages
    arrive, in a batch per connection. Nothing here reads messages, so it is
    a TLS handshake per account every four seconds for nothing. Measured over
    a 40s window: 13.5 reconnects/min with it on, 3.0 with it off, and the
    attach held in both. The remaining three are the hub closing an idle
    connection, which reconnects immediately because pings count as data.
    """
    if not client.is_connected:
        async with HANDSHAKES:
            # Re-check inside the gate: a queue of callers for the same
            # account would otherwise each open a socket in turn.
            if not client.is_connected:
                # announce_device=False: connect() would make that round trip
                # here, inside the gate, where it is the one thing serialising
                # behind the handshake. a_attach makes it alongside the attach
                # instead, where it costs nothing extra.
                await client.connect(announce_device=False)
    if client.gateway is not None:
        client.gateway.resync_interval = 0.0
        # Heartbeat, slowed. The default 6-8s is sized for a socket that is
        # reading; this one only has to stay alive. Measured over 60s at each
        # cadence: 6-8s cost 9 frames/min, 20-25s cost 4 and held the attach
        # just as well, and 45-50s lost it -- the hub stopped resuming the
        # session while the socket still looked up, which is the one failure
        # the keeper cannot see. 20-25s halves the traffic with the far side
        # of the cliff measured, not guessed.
        client.gateway.ping_interval_min = 20.0
        client.gateway.ping_interval_max = 25.0


async def a_attach(client, *, community_id):
    # Attach is what puts an account in a community's member list --
    # Member.updateCommunityOnlineStatus renders anyone who is neither
    # attached nor your friend as Offline, whatever their status says. It
    # needs a socket to live on, so this brings one up rather than requiring
    # the whole fan-out to have been started with one. The keeper remembers
    # it so a reconnect does not quietly undo it.
    # No socket here. Attach on its own is the cheapest call in this file --
    # 400 accounts in 4.4s, 87/s, 248ms median -- while opening a hub socket
    # takes ~6s and the hub only accepts ~17 of those a second. Waiting for
    # one per account is what made this take minutes: the same 400 accounts
    # cost 58.4s, 6.85/s, with the attach round trip inflated to 1157ms by
    # queueing behind the handshake. The socket is what makes an attach
    # durable, not what makes it work, so the keeper opens them behind this
    # and holds the place with a cheap refresh until they land.
    with ATTACHING:
        async with ATTACHES:
            await asyncio.gather(
                client.community.attach(community_id),
                announce_device_once(client),
            )
    KEEPER.hold(client, community_id)
    return community_id


async def a_detach(client, *, community_id):
    KEEPER.release(client, community_id)
    # Same standing-aside as ,attach: a detach across a large fleet was
    # measured at 166.7s while the keeper carried on opening sockets
    # underneath it, against 1.7s for the same call with the field clear.
    with ATTACHING:
        result = await client.community.detach(community_id)
    # Holding nothing means needing no socket. Dropping it is the difference
    # between a fan-out that idles at zero connections and one that idles at
    # one per account.
    if not KEEPER.holds_any(client) and client.is_connected:
        await client.gateway.close()
    return result


async def a_invite(client, *, community_id, max_uses):
    return await client.invites.create(community_id, max_uses=max_uses)


async def a_new_community(client, *, text):
    return await client.create_community(text)


async def a_new_category(client, *, community_id, text):
    return await client.community_admin.create_channel_group(community_id, text)


async def a_new_channel(client, *, community_id, channel_group_id, name,
                        channel_type):
    return await client.community_admin.create_channel(
        community_id, channel_group_id, name, channel_type=channel_type
    )


async def a_new_role(client, *, community_id, text):
    return await client.community_admin.create_role(community_id, text)


async def a_friend(client, *, word):
    # friend_requests.send over friends.request: same RPC, but it strips a
    # leading "@", so pasting a mention works instead of failing to find a
    # user literally called "@name".
    return await client.friend_requests.send(word)


async def a_accept_friends(client):
    return await client.friend_requests.accept_all()


async def a_status(client, *, text):
    return await client.users.set_status(text)


async def a_presence(client, *, status):
    # Presence is two independent values, and the ceiling alone is invisible.
    # What everyone else sees is the lower of SetMaxOnlineStatus (the user's
    # choice) and the *device* status, which a real client announces on every
    # connect and reconnect. These accounts run gateway=False, so nothing ever
    # announced one, and this command only ever set the ceiling -- measured
    # against a second account, a gatewayless account left at ceiling ACTIVE
    # still read DISCONNECTED for as long as it was watched, and read ACTIVE
    # within a second of the device half being sent. It then held with no
    # socket open at all, which is what makes this worth doing here rather
    # than by bringing a gateway up.
    #
    # Ceiling first, then device: the other order flashes an account online
    # for a moment when the ceiling it is coming from is higher than the one
    # it is going to, which is exactly the case ,presence invisible cares
    # about. Ceiling first, min() covers the gap.
    resolved = await client.users.set_online_status(status)
    await announce_device_once(client)
    return resolved


async def a_bio(client, *, text):
    return await client.users.set_description(text)


async def a_nick(client, *, community_id, text):
    return await client.members.edit_nickname(community_id, client.user_id, text)


async def a_read(client, *, container_id, community_id):
    return await client.messages.set_view_time(
        container_id, community_id=community_id
    )


async def a_read_all(client):
    return await client.notifications.mark_all_viewed()


async def a_whoami(client):
    return await client.whoami(refresh=True)


# --------------------------------------------------------------------------
# how a result is worth printing
# --------------------------------------------------------------------------
def r_id(value) -> str:
    return str(_field(value, "id") or "")


def r_invite(value) -> str:
    return str(_field(value, "code") or _field(value, "id") or "")


def r_name_and_id(value) -> str:
    name, ident = _field(value, "name"), _field(value, "id")
    return f"{name} {ident}" if name else str(ident or "")


def r_username(value) -> str:
    return str(_field(value, "username") or _field(value, "id") or "")


def r_plain(value) -> str:
    return str(value or "")


def r_presence(value) -> str:
    # The protocol name, not the word that was typed: "invisible" and
    # "offline" are the same DISCONNECTED, and the line should say which.
    try:
        status = UserOnlineStatus.coerce(int(value))
    except (TypeError, ValueError):
        return ""
    return f"{status.name.lower()}({int(status)})"


@dataclasses.dataclass
class Command:
    name: str
    usage: str
    summary: str
    parse: Callable[[str], dict]
    action: Optional[Callable] = None
    render: Optional[Callable] = None
    aliases: tuple = ()
    local: Optional[Callable] = None


COMMANDS: list[Command] = [
    Command("send", "<community> <channel> <message>",
            "send a message to a channel",
            p_container_text, a_send, r_id, aliases=("s",)),
    Command("reply", "<community> <channel> <message-id> <message>",
            "reply to a message in a channel",
            p_container_message_text, a_reply, r_id),
    Command("react", "<community> <channel> <message-id> <emoji>",
            "add a reaction to a message",
            p_react, a_react),
    Command("dm", "<user> <message>",
            "send a direct message (by username or user id)",
            p_user_text, a_dm, r_id),
    Command("messageallchannels", "<community> <message>",
            "send one message to EVERY text channel of a community",
            p_community_text, a_message_all, r_plain,
            aliases=("mac",)),
    Command("read", "<community> <channel>",
            "mark a channel read up to now",
            p_container, a_read),
    Command("readall", "",
            "mark every notification viewed",
            p_none, a_read_all),

    Command("join", "<invite-code>",
            "join a community by invite code (asserts 18+)",
            p_word, a_join),
    Command("leave", "<community>",
            "leave a community",
            p_community, a_leave),
    Command("attach", "<community>",
            "hold a place in a community's member list",
            p_community, a_attach),
    Command("detach", "<community>",
            "stop holding a place in a community's member list",
            p_community, a_detach),
    Command("invite", "<community> [max-uses]",
            "create an invite code for a community",
            p_community_maybe_int, a_invite, r_invite),
    Command("memberinvite", "<username> <community>",
            "invite someone to a community",
            p_user_community, a_member_invite),

    Command("newcommunity", "<name>",
            "create a community",
            p_text, a_new_community, r_name_and_id),
    Command("newcategory", "<community> <name>",
            "create a channel category",
            p_community_text, a_new_category, r_name_and_id),
    Command("newchannel", "<community> [category] <name> [text|voice]",
            "create a channel (defaults to the first category, text type)",
            p_new_channel, a_new_channel, r_name_and_id),
    Command("newrole", "<community> <name>",
            "create a role",
            p_community_text, a_new_role, r_name_and_id),

    Command("friend", "<username>",
            "send a friend request",
            p_word, a_friend),
    Command("acceptfriends", "",
            "accept every pending friend request",
            p_none, a_accept_friends),

    Command("status", "<text>",
            "set the custom status",
            p_text, a_status),
    Command("presence", "<online|idle|invisible>",
            "set the presence (visible without a gateway)",
            p_presence, a_presence, r_presence),
    Command("bio", "<text>",
            "set the profile description",
            p_text, a_bio),
    Command("nick", "<community> <nickname>",
            "set this account's own nickname in a community",
            p_community_text, a_nick),
    Command("whoami", "",
            "ask the server who each account is",
            p_none, a_whoami, r_username),
]

BY_NAME: dict[str, Command] = {}
for _command in COMMANDS:
    for _name in (_command.name, *_command.aliases):
        BY_NAME[_name] = _command


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------
class Console:
    """Everything that reaches the terminal, in one place.

    Two dispatched commands finishing at once would otherwise interleave
    mid-line; a lock is enough because output is short and never blocks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.verbose = False

    def say(self, text: str = "") -> None:
        with self._lock:
            print(text, flush=True)


OUT = Console()


class AttachKeeper:
    """Holds community attaches open across reconnects, per account.

    An attach lives with the hub connection: drop the socket and the server
    forgets it, silently, and the account vanishes from the member list while
    still looking perfectly logged in. So a fan-out that wants to *hold* a
    place in a sidebar needs two things -- a socket for each account holding
    one, which ``,attach`` opens, and something to redo the attach every time
    one of those sockets comes back, which is this.

    Without a socket the attach is not merely lost on reconnect, it decays on
    its own, which is why ``,attach`` opens one. Both halves measured
    against a second account reading
    ``CommunityGetExtendedResponse.AttachedUserIds``: an attach made with no
    gateway at all was still there at +16s and gone by +31s, and one whose
    socket was killed under it survived +36s and was gone by +48s. With the
    keeper running, the same kill was noticed in under a second and the
    account was back in the member list 6s later.

    Held sets are per account, so ``,only alice ,detach X`` stops holding X
    for alice and leaves everyone else's alone. Watching ``is_connected`` is a
    local flag read, so an idle keeper costs nothing.
    """

    POLL = 1.0

    #: A socket that will not come back should not be retried every second.
    #: The first attempt is immediate; this bounds the ones after it.
    REVIVE_EVERY = 15.0

    #: How often to re-send the attach for an account that has no socket yet.
    #: A socketless attach decays on its own -- present at +16s, gone by +31s
    #: -- and a socket takes ~6s to get, throttled to ~17/s by the hub, so at
    #: any scale most accounts are waiting. Refreshing is affordable because
    #: Attach on its own runs at 87/s; it is only expensive when it queues
    #: behind handshakes, which is why HANDSHAKES is deliberately narrow.
    #: Each refresh is a COMMUNITY_MEMBER_ATTACH other members see, so it
    #: stops the moment the account has a socket of its own.
    REFRESH_EVERY = 15.0

    def __init__(self) -> None:
        self.host: Optional[MultiClientHost] = None
        self.held: dict[str, set] = {}
        self.task: Optional[asyncio.Task] = None
        #: name -> was it connected last time round. Seeded on first sight so
        #: startup is not mistaken for a reconnect.
        self._connected: dict[str, bool] = {}
        #: name -> when its socket was last revived, monotonic.
        self._revived: dict[str, float] = {}
        #: id(client) -> account name, so the lookup is not a scan.
        self._names: dict[int, str] = {}
        #: name -> when its socketless attach was last refreshed, monotonic.
        self._refreshed: dict[str, float] = {}
        #: accounts with work in flight, so the poll does not queue it twice.
        self._working: set = set()
        self._tasks: set = set()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, host: MultiClientHost) -> None:
        self.host = host
        if not self.running:
            self.task = asyncio.create_task(self._loop(), name="attach-keeper")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._working.clear()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):        # noqa: BLE001
                pass
            self.task = None

    def _name_for(self, client) -> Optional[str]:
        # Cached by object identity. This is called once per account per
        # ,attach, and a linear scan of the host would make that O(N^2) --
        # 1.7M comparisons at 1,300 accounts, for a lookup that never changes.
        key = id(client)
        name = self._names.get(key)
        if name is not None:
            return name
        for candidate, account in (
            self.host.accounts.items() if self.host else ()
        ):
            if account.client is client:
                self._names[key] = candidate
                return candidate
        return None

    def hold(self, client, community_id: str) -> None:
        name = self._name_for(client)
        if name is not None:
            self.held.setdefault(name, set()).add(community_id)

    def release(self, client, community_id: str) -> None:
        name = self._name_for(client)
        if name is not None:
            self.held.get(name, set()).discard(community_id)

    def holds_any(self, client) -> bool:
        name = self._name_for(client)
        return bool(name is not None and self.held.get(name))

    def summary(self) -> str:
        holding = {n: c for n, c in self.held.items() if c}
        if not holding:
            return "holding nothing"
        communities = sorted({c for ids in holding.values() for c in ids})
        return (f"{len(holding)} account(s) holding "
                f"{len(communities)} community(ies)")

    def _spawn(self, name: str, coro) -> None:
        """Run one account's upkeep off the poll loop.

        The poll used to ``await`` this inline, which meant one account at a
        time: a socket takes ~6s to open, so a thousand accounts would have
        taken over an hour to work through. Each account's work is its own
        task; ``hold_socket``'s own gate is what bounds them.
        """
        self._working.add(name)
        task = asyncio.create_task(coro, name=f"keeper:{name}")
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            self._working.discard(name)
            if not done.cancelled() and done.exception() is not None:
                OUT.say(f"  {name}: keeper: {done.exception()!r}")

        task.add_done_callback(finished)

    async def _attach_all(self, name: str, client, wanted) -> None:
        for community_id in wanted:
            try:
                await client.community.attach(community_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                           # noqa: BLE001
                OUT.say(f"  {name}: re-attach failed: "
                        f"{type(exc).__name__}: {exc}")

    async def _tend(self, name: str, client, wanted, *, revive: bool) -> None:
        if revive:
            try:
                await hold_socket(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                           # noqa: BLE001
                OUT.say(f"  {name}: could not connect: "
                        f"{type(exc).__name__}: {exc}")
                return
            self._connected[name] = bool(
                getattr(client, "is_connected", False)
            )
        # A fresh connection means a fresh subscription: the attach lives
        # with the socket, not with the account. A socketless refresh lands
        # here too -- same call, different reason.
        await self._attach_all(name, client, wanted)
        self._refreshed[name] = time.monotonic()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.POLL)
            if self.host is None:
                continue
            now = time.monotonic()
            for name, account in list(self.host.accounts.items()):
                if name in self._working:
                    continue
                client = account.client
                wanted = sorted(self.held.get(name, ()))
                if client is None or not wanted:
                    continue

                # ``is_connected`` is coarser than it looks: the hub's
                # ordinary close-1000-and-resume cycle does not clear it, so
                # a False here is not a blip -- the gateway task has ended,
                # or was never started because ,attach no longer waits for
                # one. Either way this account needs a socket.
                connected = bool(getattr(client, "is_connected", False))
                was = self._connected.get(name, connected)
                self._connected[name] = connected

                if connected:
                    if not was:                    # socket just came back
                        self._spawn(name, self._tend(
                            name, client, wanted, revive=False))
                    continue

                if (not ATTACHING
                        and now - self._revived.get(name, 0.0)
                        >= self.REVIVE_EVERY):
                    self._revived[name] = now
                    self._spawn(name, self._tend(
                        name, client, wanted, revive=True))
                elif now - self._refreshed.get(name, 0.0) >= self.REFRESH_EVERY:
                    # Still queued behind the hub's handshake ceiling. Keep
                    # the member-list place from decaying while it waits.
                    self._spawn(name, self._tend(
                        name, client, wanted, revive=False))


KEEPER = AttachKeeper()


class Session:
    def __init__(self, host: MultiClientHost, *, concurrency: int = 64) -> None:
        self.host = host
        # broadcast() used to default to a flat 8, which is fine for a handful
        # of accounts and badly wrong for a lot of them: at 1,089 accounts and
        # a 200ms round trip that is 28 seconds for every single command. It
        # now scales with the account count and caps at 64 -- these numbers
        # are why -- but this fan-out still passes its own value, because
        # --concurrency is a knob the operator is meant to turn. Measured
        # against the real broadcast with a simulated round trip:
        #   concurrency=8 -> 28.3s   32 -> 7.3s   64 -> 3.8s   128 -> 2.0s
        # Root's rate limits are per account and these are all different
        # accounts, so the ceiling is about being a good citizen on one IP
        # rather than about correctness.
        self.concurrency = max(1, int(concurrency))
        self.only: Optional[list[str]] = None
        self.counter = 0
        self.running: set[asyncio.Task] = set()
        self.stop = asyncio.Event()

    # -- targeting ----------------------------------------------------- #
    def target_names(self) -> list[str]:
        """Who the next command runs as.

        An explicit ``,only`` is taken literally, including a name that is
        currently down -- you asked for it, and the outcome will say so. The
        default excludes accounts that never came up, because otherwise a
        single dead token adds the same failure line to every command you
        type for the rest of the session.
        """
        if self.only:
            return list(self.only)
        return [
            name for name, account in self.host.accounts.items()
            if account.ready
        ] or list(self.host.accounts)

    def down(self) -> int:
        return sum(
            1 for account in self.host.accounts.values() if not account.ready
        )

    # -- dispatch ------------------------------------------------------ #
    def dispatch(self, command: Command, raw: str) -> None:
        try:
            kwargs = command.parse(raw)
        except (NotFound, Ambiguous) as exc:
            OUT.say(f"    {exc}")
            return
        except Exception as exc:                               # noqa: BLE001
            OUT.say(f"    {type(exc).__name__}: {exc}")
            return

        self.counter += 1
        number = self.counter
        # Resolve WHO now, not when the task first runs. create_task only
        # schedules; if two lines are already queued, handle() runs both
        # before the loop touches either task -- so a ,only typed straight
        # after a ,send would have retargeted the send that was already
        # entered. Whoever was selected when you pressed enter is who acts.
        names = self.target_names()
        asleep = self.down() if not self.only else 0
        target = INDEX.describe(kwargs)
        self.track(asyncio.create_task(
            self._run(number, command, kwargs, names, asleep, target)
        ))

    async def _run(self, number: int, command: Command, kwargs: dict,
                   names: list[str], asleep: int, target: str = "") -> None:
        try:
            if "_username" in kwargs:
                # One lookup for the whole fan-out, not one per account: the
                # answer is the same for everybody, and it has to happen here
                # because parsing is synchronous and this is a round trip.
                kwargs = dict(kwargs)
                wanted = kwargs.pop("_username")
                client = next(
                    (self.host.accounts[n].client for n in names
                     if n in self.host.accounts
                     and self.host.accounts[n].ready
                     and self.host.accounts[n].client is not None),
                    None,
                )
                if client is None:
                    OUT.say(f"[{number}] ,{command.name} -> no account is up")
                    return
                kwargs["user_id"] = await resolve_username(client, wanted)
                target = INDEX.describe(kwargs) or target
            await self._broadcast(number, command, kwargs, names, asleep,
                                  target)
        except (NotFound, Ambiguous) as exc:
            OUT.say(f"[{number}] ,{command.name} -> {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001
            # broadcast() is documented never to raise, but if anything here
            # does, the numbered command would otherwise vanish with no line
            # at all -- the done callback never calls task.exception().
            OUT.say(f"[{number}] ,{command.name} -> "
                    f"{type(exc).__name__}: {exc}")

    async def _broadcast(self, number: int, command: Command, kwargs: dict,
                         names: list[str], asleep: int,
                         target: str = "") -> None:
        started = time.monotonic()
        results = await self.host.broadcast(
            lambda client: command.action(client, **kwargs),
            only=names,
            concurrency=self.concurrency,
            timeout=45.0,
        )
        elapsed = time.monotonic() - started

        ok = [n for n, o in results.items() if o.ok]
        bad = [(n, o) for n, o in results.items() if not o.ok]

        lines = [
            f"[{number}] ,{command.name}"
            + (f" {target}" if target else "")
            + f" -> {len(ok)}/{len(names)} ok  ({elapsed:.2f}s)"
            + (f"   [{asleep} account(s) down, not sent]" if asleep else "")
        ]
        if command.render is not None:
            rendered: dict[str, list[str]] = {}
            for name in ok:
                try:
                    text = command.render(results[name].value)
                except Exception:                              # noqa: BLE001
                    text = ""
                if text:
                    rendered.setdefault(text, []).append(name)
            # One shared value (a channel id every account saw) is one line;
            # per-account values (invite codes) get a line each.
            if len(rendered) == 1 and len(next(iter(rendered.values()))) > 1:
                lines.append(f"[{number}]   {next(iter(rendered))}")
            else:
                for text, who in rendered.items():
                    lines.append(f"[{number}]   {', '.join(who)}: {text}")
        for name, outcome in bad:
            lines.append(
                f"[{number}]   {name}: {type(outcome.error).__name__}: "
                f"{outcome.error}"
            )
        OUT.say("\n".join(lines))

    # -- local commands ------------------------------------------------ #
    def show_help(self, raw: str) -> None:
        wanted = raw.strip().lstrip(PREFIX).casefold()
        if wanted and wanted in BY_NAME:
            command = BY_NAME[wanted]
            alias = (f"   (also ,{', ,'.join(command.aliases)})"
                     if command.aliases else "")
            OUT.say(f"\n  ,{command.name} {command.usage}{alias}\n"
                    f"    {command.summary}\n")
            return

        width = max(len(c.name) + len(c.usage) for c in COMMANDS) + 2
        OUT.say("\n  Every command below runs as all selected accounts at "
                "once.\n  Names work wherever an id does: ,send Root Chat hi\n")
        for command in COMMANDS:
            call = f",{command.name} {command.usage}".rstrip()
            OUT.say(f"    {call:<{width}}  {command.summary}")
        OUT.say("\n  Local, not broadcast:\n")
        for call, summary in LOCAL_HELP:
            OUT.say(f"    {call:<{width}}  {summary}")
        OUT.say("")

    def show_accounts(self, raw: str) -> None:
        selected = set(self.target_names())
        OUT.say("")
        for name, entry in self.host.status().items():
            mark = "*" if name in selected else " "
            state = "ready" if entry["ready"] else (entry["error"] or "down")
            who = entry["username"] or ""
            OUT.say(f"  {mark} {name:<16} {who:<20} {state}")
        OUT.say(f"\n  {len(selected)} of {len(self.host.accounts)} selected"
                f" (* acts on commands)\n")

    def show_stats(self, raw: str) -> None:
        """Where a slow command actually went, per endpoint.

        A fan-out that takes minutes has three possible culprits and they want
        opposite fixes, so guessing between them is expensive:

          roundtrip -- the server was slow. Concurrency is the lever.
          wait      -- we were rate limited and backed off. Fewer calls, or
                       spread them over more IPs (--proxy).
          overhead  -- time in neither: the event loop was starved. More
                       concurrency makes this *worse*, not better.

        The transport has recorded all three per endpoint since it was
        written; nothing ever printed them.
        """
        transport = getattr(self.host, "_shared", None)
        stats = getattr(transport, "stats", None)
        endpoints = dict(getattr(stats, "endpoints", {}) or {})
        if not endpoints:
            OUT.say("    nothing recorded yet -- run a command first")
            return
        OUT.say("")
        OUT.say(f"  {'endpoint':<38} {'calls':>6} {'429':>4} {'retry':>5}"
                f" {'rt med':>7} {'rt max':>7} {'wait':>8} {'overhead':>9}")
        for name, entry in sorted(endpoints.items()):
            row = entry.summary()
            OUT.say(
                f"  {name[:38]:<38} {row['calls']:>6} "
                f"{row['rate_limited']:>4} {row['retries']:>5} "
                f"{row['roundtrip_median_ms']:>6.0f}ms "
                f"{row['roundtrip_max_ms']:>6.0f}ms "
                f"{row['wait_ms'] / 1000:>7.1f}s "
                f"{row['overhead_ms'] / 1000:>8.1f}s"
            )
        OUT.say("")

    def show_servers(self, raw: str) -> None:
        if not INDEX.communities:
            OUT.say("    nothing indexed yet -- ,refresh")
            return
        OUT.say("")
        for entry in INDEX.communities:
            count = len(INDEX.channels.get(entry.id, []))
            OUT.say(f"  {entry.name:<32} {entry.id}  "
                    f"({count} channel{'' if count == 1 else 's'})")
        OUT.say("")

    def show_channels(self, raw: str) -> None:
        try:
            community_id = INDEX.community(raw.strip())
        except (NotFound, Ambiguous) as exc:
            OUT.say(f"    {exc}")
            return
        entries = INDEX.channels.get(community_id) or []
        if not entries:
            OUT.say("    no channels indexed for that community")
            return
        kinds = {v: k for k, v in CHANNEL_TYPES.items()}
        OUT.say("")
        for entry in entries:
            OUT.say(f"  {entry.name:<32} {entry.id}  "
                    f"{kinds.get(entry.kind, '')}")
        OUT.say("")

    def set_only(self, raw: str) -> None:
        wanted = [t for t, _ in tokenize(raw)]
        if not wanted:
            OUT.say("    ,only <account> [account...]   -- or ,all")
            return
        unknown = [w for w in wanted if w not in self.host.accounts]
        if unknown:
            OUT.say(f"    no such account: {', '.join(unknown)}")
            return
        # Deduplicate, keeping the order typed. broadcast() schedules one call
        # per entry of `only` but keys results by name, so ",only a a" would
        # send twice and report once -- a doubled message with nothing on
        # screen to show it happened.
        self.only = list(dict.fromkeys(wanted))
        OUT.say(f"    commands now run as {', '.join(self.only)}")

    def set_all(self, raw: str) -> None:
        self.only = None
        OUT.say(f"    commands now run as all {len(self.host.accounts)} accounts")

    def do_refresh(self, raw: str) -> None:
        # Claim the flag HERE, not inside Index.build. create_task only
        # schedules, so two ,refresh lines in one input burst both ran this
        # guard before either task started, and both builds went ahead.
        if INDEX.building:
            OUT.say("    already building")
            return
        INDEX.building = True
        self.track(asyncio.create_task(build_index(self.host)))

    def track(self, task: asyncio.Task) -> asyncio.Task:
        """Hold a reference and clean it up. A bare create_task is only
        weakly held by the loop, so it can be collected mid-flight -- and
        nothing untracked is awaited by the shutdown path."""
        self.running.add(task)
        task.add_done_callback(self.running.discard)
        return task

    def set_verbose(self, raw: str) -> None:
        OUT.verbose = not OUT.verbose
        logging.getLogger("rootpy").setLevel(
            logging.INFO if OUT.verbose else logging.WARNING
        )
        # Silenced to CRITICAL at startup because login failures are reported
        # here instead; ,verbose has to lift that too or it does half a job.
        logging.getLogger("rootpy.host").setLevel(
            logging.INFO if OUT.verbose else logging.CRITICAL
        )
        OUT.say(f"    verbose {'on' if OUT.verbose else 'off'}")

    def do_quit(self, raw: str) -> None:
        self.stop.set()

    # -- the loop ------------------------------------------------------ #
    def handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith(PREFIX):
            line = line[len(PREFIX):]
        # split(None, 1) rather than partition(" "): tokenize treats all
        # whitespace as a separator, so a tab-separated line has to find its
        # command too.
        parts = line.split(None, 1)
        name = parts[0].casefold()
        raw = parts[1] if len(parts) > 1 else ""

        local = LOCAL.get(name)
        if local is not None:
            # Local commands run inline rather than as a task, so an
            # exception here would unwind through read_lines and end the
            # session -- with everyone still logged in and work in flight.
            try:
                local(self, raw)
            except Exception as exc:                           # noqa: BLE001
                OUT.say(f"    ,{name}: {type(exc).__name__}: {exc}")
            return

        command = BY_NAME.get(name)
        if command is None:
            near = [c.name for c in COMMANDS if c.name.startswith(name)]
            hint = f" -- did you mean ,{near[0]}?" if near else ""
            OUT.say(f"    no command ,{name}{hint}   (,help)")
            return
        self.dispatch(command, raw)


LOCAL: dict[str, Callable] = {
    "help": Session.show_help,
    "?": Session.show_help,
    "accounts": Session.show_accounts,
    "who": Session.show_accounts,
    "servers": Session.show_servers,
    "stats": Session.show_stats,
    "channels": Session.show_channels,
    "only": Session.set_only,
    "all": Session.set_all,
    "refresh": Session.do_refresh,
    "verbose": Session.set_verbose,
    "quit": Session.do_quit,
    "exit": Session.do_quit,
}

LOCAL_HELP = (
    (",help [command]", "this, or detail on one command"),
    (",accounts", "which accounts are up and which are selected"),
    (",servers", "indexed communities"),
    (",stats", "where the time went: roundtrip vs rate-limit vs overhead"),
    (",channels <community>", "indexed channels in one community"),
    (",only <account>...", "run later commands as just these accounts"),
    (",all", "run later commands as every account"),
    (",refresh", "rebuild the community / channel / user index"),
    (",verbose", "show the SDK's own logging"),
    (",quit", "log everyone out and exit"),
)


# --------------------------------------------------------------------------
async def build_index(host: MultiClientHost, *, refresh: bool = True) -> None:
    started = time.monotonic()
    try:
        summary = await INDEX.build(host, refresh=refresh)
    except Exception as exc:                                   # noqa: BLE001
        OUT.say(f"    index failed: {type(exc).__name__}: {exc}")
        return
    OUT.say(f"    indexed {summary}  ({time.monotonic() - started:.1f}s)")


class LoginProgress:
    """A bar for the one part of startup that scales with account count.

    Redrawn in place on a terminal. When stdout is a pipe, ``\\r`` would just
    build one enormous line, so it prints instead -- and only when the *bar*
    moves, not the count. At 1,089 accounts the count moves on nearly every
    poll, which put 240 lines of progress into a piped run for a bar with 24
    positions in it.
    """

    WIDTH = 24

    #: Seconds between redraws when nothing has changed. The wait loop polls
    #: 20x a second; redrawing that often means thousands of write+flush calls
    #: over a long login, and a Windows console is slow enough at those to be
    #: worth not doing. The bar only needs to look alive, not be a stopwatch.
    MIN_INTERVAL = 0.2

    def __init__(self, total: int) -> None:
        self.total = max(1, total)
        self.tty = sys.stdout.isatty()
        self.shown = -1
        self.previous = 0
        self.drawn_at = -1.0
        self.last_bar = ""

    def draw(self, done: int, failed: int, elapsed: float, *,
             final: bool = False) -> None:
        if not final and done == self.shown \
                and elapsed - self.drawn_at < self.MIN_INTERVAL:
            return
        self.drawn_at = elapsed
        filled = round(self.WIDTH * done / self.total)
        bar = "#" * filled + "-" * (self.WIDTH - filled)

        eta = ""
        if done and done < self.total:
            # Logins overlap, so per-account cost falls as more land. Using
            # the running mean rather than the first sample stops the estimate
            # opening at a wild number and walking down.
            eta = f"   ~{(elapsed / done) * (self.total - done):.1f}s left"

        line = (f"  [{bar}] {done}/{self.total}"
                + (f"   {failed} failed" if failed else "")
                + f"   {elapsed:.1f}s{eta}")

        if self.tty:
            pad = " " * max(0, self.previous - len(line))
            self.previous = len(line)
            sys.stdout.write("\r" + line + pad)
            if final:
                sys.stdout.write("\n")
            sys.stdout.flush()
        elif final or bar != self.last_bar:
            print(line, flush=True)
        self.last_bar = bar
        self.shown = done


async def wait_with_progress(host: MultiClientHost, *,
                             timeout: float = 60.0) -> dict:
    """``host.wait_ready``, but it shows its working.

    Polls twice as often as the host's own loop as well, which is worth up to
    0.15s of the total: at these speeds the poll interval is a real share of
    the time being waited on.
    """
    total = len(host.accounts)
    bar = LoginProgress(total)
    started = time.monotonic()

    while True:
        ready = sum(1 for a in host.accounts.values() if a.ready)
        failed = sum(1 for a in host.accounts.values() if a.error is not None)
        done = ready + failed
        elapsed = time.monotonic() - started
        settled = done >= total or elapsed >= timeout
        bar.draw(done, failed, elapsed, final=settled)
        if settled:
            break
        await asyncio.sleep(0.05)

    return {name: a.ready for name, a in host.accounts.items()}


def _fancy_prompt():
    """``(session, patch_stdout)`` if this terminal can host one, else None.

    prompt_toolkit is worth reaching for because ``patch_stdout`` keeps a
    half-typed line intact when a command finishes and prints underneath it,
    which here happens constantly -- nothing waits for anything.

    Guarding the *import* is not enough, though. Constructing a
    ``PromptSession`` probes the real console, and on Windows that raises
    ``NoConsoleScreenBufferError`` whenever stdout is not a Win32 console --
    piped output, or any mintty terminal, Git Bash included. Measured: it
    killed the process after the accounts had already logged in. Anything
    that goes wrong here falls back to plain stdin, which always works.
    """
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        return PromptSession(), patch_stdout
    except Exception:                                          # noqa: BLE001
        return None


async def read_lines(session: Session) -> None:
    """Feed typed lines to the session without ever blocking the loop."""
    fancy = _fancy_prompt()
    if fancy is None:
        await _read_lines_thread(session)
        return

    prompt, patch_stdout = fancy
    with patch_stdout():
        while not session.stop.is_set():
            try:
                line = await prompt.prompt_async("> ")
            except (EOFError, KeyboardInterrupt):
                session.stop.set()
                return
            session.handle(line)


async def _read_lines_thread(session: Session) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def pump() -> None:
        # This thread cannot be woken out of a blocking stdin read, so after
        # ,quit closes the loop it is still sitting here; the next line typed
        # would raise "Event loop is closed" from a daemon thread on the way
        # out. Nothing left to deliver it to, so stop quietly.
        try:
            for line in sys.stdin:
                if session.stop.is_set():
                    return
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:
            pass

    threading.Thread(target=pump, daemon=True).start()
    while not session.stop.is_set():
        line = await queue.get()
        if line is None:
            session.stop.set()
            return
        session.handle(line)


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive every account in tokens.txt or provisioned.json "
                    "from one prompt."
    )
    parser.add_argument("--tokens", type=Path, default=None,
                        help="path to tokens.txt or provisioned.json "
                             "(default: the usual places)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation")
    parser.add_argument("--no-index", action="store_true",
                        help="skip the startup index; ,refresh builds it")
    parser.add_argument("--proxy", default=None,
                        help="route every account through a proxy, e.g. "
                             "socks5://127.0.0.1:1080 for a wireproxy tunnel")
    parser.add_argument("--stagger", type=float, default=0.0, metavar="SECONDS",
                        help="fixed gap between logins (default 0; prefer "
                             "--login-concurrency)")
    parser.add_argument("--login-concurrency", type=int, default=64,
                        metavar="N",
                        help="how many accounts log in at once (default 64; "
                             "0 for no limit)")
    parser.add_argument("--concurrency", type=int, default=64, metavar="N",
                        help="how many accounts act on a command at once "
                             "(default 64)")
    parser.add_argument("--verbose", action="store_true",
                        help="show the SDK's own logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    for noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio",
                  "rootpy"):
        logging.getLogger(noisy).setLevel(
            logging.INFO if args.verbose else logging.WARNING
        )
    # The host logs a failed login at ERROR, which this reports itself a few
    # lines later -- otherwise a dead token prints twice, once out of order.
    logging.getLogger("rootpy.host").setLevel(
        logging.INFO if args.verbose else logging.CRITICAL
    )
    OUT.verbose = args.verbose

    path = args.tokens or credentials_file()
    accounts = load_tokens(path)
    if not args.yes and not confirm(accounts, path):
        print("Nothing logged in.")
        return 1

    # A ceiling on logins in flight rather than a delay between them. The
    # host's 0.5s stagger guards against N simultaneous handshakes, which a
    # shared transport does not have -- but removing it entirely releases
    # every login at once, and at a few hundred accounts that is its own
    # problem. The gate bounds the burst and costs nothing for accounts that
    # are already up: 200 accounts stay at 24 in flight, with no fixed delay
    # to pay. --stagger is still there if Root ever rate-limits by IP.
    host = MultiClientHost(
        stagger=args.stagger,
        proxy=args.proxy,
        max_concurrent_logins=args.login_concurrency or None,
        # The pool has to be able to hold what the gate lets through *and*
        # what a command fans out to -- it used to be sized from the login
        # gate alone, which left it smaller than --concurrency. HTTP/2
        # multiplexes, so this is headroom rather than one socket per call:
        # measured, 48 and 200 give the same throughput on one account.
        max_connections=max(
            20, (args.login_concurrency or 32) * 2, args.concurrency
        ),
    )
    if args.proxy:
        print(f"routing through {args.proxy}")
    for label, token in accounts:
        # gateway=False: this sends, it never listens, and the websocket
        # handshake is the slowest part of bringing an account up. The one
        # thing that does need a socket -- holding a place in a member list --
        # opens its own, for the accounts it is asked about, when it is asked.
        # See ,attach and hold_socket.
        # defer_hub: login is two sequential hops, and the first of them --
        # GetNewHubserverEndpoint, ~680ms -- exists only to address a
        # websocket. Most accounts here never open one, and ,attach fetches
        # it for the ones that do.
        host.add(label, token, gateway=False, defer_hub=True)

    # The try starts HERE, not after login. Everything below can raise -- a
    # network error in start(), a timeout in wait_ready(), Ctrl+C while the
    # accounts come up -- and each of those used to leave the shared transport
    # and every logged-in client open.
    session = None
    try:
        print(f"\nlogging in {len(accounts)} account(s)...")
        began = time.monotonic()
        await host.start()
        ready = await wait_with_progress(host, timeout=60)
        elapsed = time.monotonic() - began

        status = host.status()
        live = sum(1 for up in ready.values() if up)
        # Every failure, always -- a dead token is why an account is missing.
        # Successes only for a small host: at 20+ accounts the listing is
        # noise, and ,accounts shows it on demand.
        for name, up in ready.items():
            if not up:
                print(f"  {name:<16} FAILED  {status[name]['error']}")
            elif len(accounts) <= 12:
                print(f"  {name:<16} {status[name]['username']}")

        if not live:
            print("\nNo account logged in.")
            return 1
        print(f"\n{live} of {len(accounts)} up in {elapsed:.1f}s"
              + ("" if live == len(accounts)
                 else "; commands run as the ones that are")
              + "  (,accounts to see, ,only to narrow)")

        session = Session(host, concurrency=args.concurrency)
        # Idle until something is actually held: the loop skips any account
        # with an empty set, so a run that never attaches never pays for it.
        KEEPER.start(host)
        if not args.no_index:
            INDEX.building = True
            # refresh=False: login already fetched each account's community
            # list, and asking again costs 185ms per token.
            session.track(
                asyncio.create_task(build_index(host, refresh=False))
            )

        print(f"\n,help for commands. {PREFIX}quit to leave.\n")
        await read_lines(session)
    finally:
        if session is not None and session.running:
            print(f"\nwaiting for {len(session.running)} command(s)...")
            await asyncio.gather(*session.running, return_exceptions=True)
        await KEEPER.stop()
        await host.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
