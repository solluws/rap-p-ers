"""
Send a single message as fast as possible -- one HTTP request, no login.

    python sendone.py "your message here"

Needs tokens.txt (token=...) plus the channel and community below.
"""

import asyncio
import sys
import time
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient


CHANNEL = "00308801-dfec-8404-bb84-bf060b2169ba"
COMMUNITY = "0030735e-ddbf-8d02-99de-255bc3fc5dc5"


def load_token() -> str:
    path = Path(__file__).with_name("tokens.txt")
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if value.strip():
            return value.strip()
    raise SystemExit("Put token=<your token> in tokens.txt")


async def main() -> None:
    content = " ".join(sys.argv[1:]) or "hello from rootpy"
    started = time.perf_counter()
    result = await RootClient.send_once(
        load_token(), CHANNEL, content, community_id=COMMUNITY
    )
    print(f"sent in {(time.perf_counter() - started) * 1000:.0f} ms -> {result}")


if __name__ == "__main__":
    asyncio.run(main())
