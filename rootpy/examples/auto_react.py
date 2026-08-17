"""auto_react -- put a burst of hearts under your own messages.

Root renders each distinct reaction separately, so a list of heart variants
gives a little wall of hearts under everything you post. This reacts only to
your own messages (the default), from your single account.

    python -m rootpy.examples.auto_react
"""

from __future__ import annotations

import asyncio
import os

from rootpy import RootClient

USERNAME = os.environ.get("ROOT_USERNAME", "your_username")
PASSWORD = os.environ.get("ROOT_PASSWORD", "your_password")

HEARTS = ["❤️", "🧡", "💛", "💚", "💙", "💜"]


async def main() -> None:
    client = RootClient()

    @client.event
    async def on_ready(_event) -> None:
        me = await client.whoami()
        print(f"auto-react running as {me.username}")

    # Enable the effect. only_self=True is the default, so this reacts to
    # messages you send and nobody else's. Pass containers=[...] to scope it
    # to specific channels, or stop_on_error=True to surface failures.
    client.enable_auto_react(HEARTS)

    # ...to turn it off later: client.disable_auto_react()

    # start() logs in, connects, and blocks until you Ctrl-C.
    await client.start(USERNAME, PASSWORD)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
