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
accounts.txt next to this script, one per line:

    alice = <alice's token>
    bob   = <bob's token>

    python multihost.py
"""

import asyncio
import logging
import sys
from pathlib import Path

from rootpy import MultiClientHost

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("multihost")


def load_accounts() -> list[tuple[str, str]]:
    path = Path(__file__).with_name("accounts.txt")
    if not path.exists():
        raise SystemExit(
            "Create accounts.txt next to this script:\n"
            "    alice = <token>\n"
            "    bob   = <token>"
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
    host = MultiClientHost(stagger=0.5, max_connections_per_account=20)

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
