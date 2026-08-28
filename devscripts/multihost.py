"""
Host several accounts in one process.

One process per account wastes ~30 MB each on interpreter and imports. This
runs them all on a single event loop instead:

    50 accounts, separate processes  ->  ~1,490 MB
    50 accounts, this               ->     ~30 MB

Each account is fully independent -- its own client, handlers, credentials,
rate-limit budget, and failures.

SETUP
-----
Either file, next to this script. accounts.txt, one per line:

    alice = <alice's token>
    bob   = <bob's token>

or provisioned.json, exactly as provisioner/provision.py writes it -- copy it
over and change nothing:

    {"accounts": [{"username": "...", "token": "...", ...}]}

    python multihost.py

provisioned.json wins if both exist.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import MultiClientHost

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("multihost")


def load_accounts() -> list[tuple[str, str]]:
    """Read provisioned.json, or accounts.txt.

    Two formats because accounts arrive two ways, and nothing converts between
    them -- retyping fifty tokens into a second file is how one of them ends
    up wrong.
    """
    ledger = Path(__file__).with_name("provisioned.json")
    if ledger.is_file():
        return _from_ledger(ledger)

    path = Path(__file__).with_name("accounts.txt")
    if not path.exists():
        raise SystemExit(
            "Put one of these next to this script:\n"
            "    accounts.txt      alice = <token>\n"
            "    provisioned.json  the ledger provision.py writes"
        )
    accounts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, token = line.partition("=")
        if token.strip():
            accounts.append((name.strip(), token.strip()))
    if not accounts:
        raise SystemExit("No accounts found in accounts.txt")
    return accounts


def _from_ledger(path: Path) -> list[tuple[str, str]]:
    """provision.py's ledger: {"accounts": [{"username", "token", ...}]}."""
    rows = json.loads(path.read_text(encoding="utf-8")).get("accounts", [])

    accounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    skipped = 0
    for row in rows:
        token = (row.get("token") or "").strip()
        if not token:
            # The ledger records a row even when a step after signup failed,
            # so a tokenless row is a half-made account, not a corrupt file.
            skipped += 1
            continue
        name = (row.get("username") or "").strip() or (row.get("user_id") or "")[:8]
        # The ledger appends across runs and add() raises on a repeat, so one
        # duplicated username would otherwise take the whole host down.
        base, n = name or "account", 2
        while name in seen or not name:
            name, n = f"{base}#{n}", n + 1
        seen.add(name)
        accounts.append((name, token))

    if skipped:
        log.info("%s: skipped %d row(s) with no token", path.name, skipped)
    if not accounts:
        raise SystemExit(f"No usable accounts in {path.name}")

    # Each row remembers the tunnel its account was created behind. The host
    # takes one proxy for all of them, so that detail cannot survive the trip
    # and it is better said than silently dropped.
    proxies = {row.get("proxy") for row in rows if row.get("proxy")}
    if len(proxies) > 1:
        log.warning(
            "these %d accounts were created behind %d different proxies; "
            "MultiClientHost takes a single proxy= for all of them, so as "
            "written they will all go out this machine's address",
            len(accounts), len(proxies))
    elif proxies:
        log.info("created behind %s", next(iter(proxies)))
    return accounts


def make_setup(name: str):
    """Build the handlers for one account.

    Each account gets its own -- this is where that account's behaviour lives.
    """
    async def setup(client) -> None:
        @client.event
        async def on_message(event) -> None:
            message = event.message
            if message.user_id == client.user_id:
                return
            log.info("[%s] %s: %s", name, (message.user_id or "?")[:8],
                     message.content[:80])

        @client.event
        async def on_friend_request(event) -> None:
            log.info("[%s] friend request from %s", name,
                     event.username or event.user_id)

    return setup


async def main() -> None:
    host = MultiClientHost(stagger=0.5, max_connections=20)

    for name, token in load_accounts():
        host.add(name, token, setup=make_setup(name))

    log.info("starting %d account(s)...", len(host.accounts))
    await host.start()
    ready = await host.wait_ready(timeout=60)

    for name, ok in ready.items():
        detail = host.status()[name]
        log.info("  %-12s %s%s", name, "ready" if ok else "FAILED",
                 f" -- {detail['error']}" if detail["error"] else
                 f" ({detail['username']})")

    log.info("running -- Ctrl+C to stop")
    try:
        while True:
            await asyncio.sleep(60)
            alive = sum(1 for s in host.status().values() if s["ready"])
            log.info("heartbeat: %d/%d accounts up", alive, len(host.accounts))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await host.stop()
        log.info("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
