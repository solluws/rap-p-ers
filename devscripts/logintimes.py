"""
Time every way to log in with a token.

Each mode is timed on the same account (and repeated), so the differences are
the modes themselves rather than account or network variance. Request counts
come from wrapping the transport, so you see exactly what each mode costs.


THE MODES
---------
  minimal      login_token(preload_caches=False)
                 -> hub lookup + GetSelf. Identity only; communities and DMs
                    load on demand.

  lazy         login_token()                      [the default]
                 -> hub lookup + (GetSelf || ListMine in parallel).
                    Community detail expands on first access and is cached.

  eager        login_token() with expand_communities=True
                 -> the above + GetExtended per community (concurrent).

  eager+dms    eager plus preload_direct_messages=True
                 -> the above + DirectMessageList.

  gateway      lazy + connect()
                 -> lazy, then opens the websocket. Only needed to RECEIVE
                    pushed events; every RPC works fine without it.

Note start_token() is lazy + connect() + wait_closed() -- it never returns by
design (it runs until the socket closes), so it isn't timed here.

SETUP
-----
tokens.txt next to this script:

    token=<a token>

    python logintimes.py
"""

import asyncio
import logging
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient

ROUNDS = 3
LOG_PATH = Path(__file__).with_name("log.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
for _noisy in (
    "httpx", "httpcore", "hpack", "h2", "websockets", "asyncio",
    "rootpy.gateway", "rootpy.direct_messages", "rootpy.messages",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("logintimes")


MODES = [
    ("minimal",   dict(preload=False, eager=False, dms=False, gateway=False)),
    ("lazy",      dict(preload=True,  eager=False, dms=False, gateway=False)),
    ("eager",     dict(preload=True,  eager=True,  dms=False, gateway=False)),
    ("eager+dms", dict(preload=True,  eager=True,  dms=True,  gateway=False)),
    ("gateway",   dict(preload=True,  eager=False, dms=False, gateway=True)),
]


def load_tokens() -> list[str]:
    path = Path(__file__).with_name("tokens.txt")
    if not path.exists():
        raise SystemExit("Create tokens.txt with:  token=<your token>")
    tokens = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _key, _, value = line.partition("=")
        value = value.strip()
        if value and value not in tokens:
            tokens.append(value)
    if not tokens:
        raise SystemExit("No tokens found in tokens.txt")
    return tokens


async def time_mode(token: str, config: dict) -> dict:
    client = RootClient(
        token=token,
        preload_caches=config["preload"],
        expand_communities=config["eager"],
        preload_direct_messages=config["dms"],
    )

    endpoints: Counter = Counter()
    transports = {
        getattr(service, "transport", None)
        for service in (
            client.users, client.messages, client.community_service,
            client.direct_messages, client.auth,
        )
    }
    transports.discard(None)

    originals = []
    for transport in transports:
        original = transport.unary

        def make(orig):
            async def counting(*, endpoint, **kwargs):
                endpoints[endpoint.rsplit("/", 1)[-1]] += 1
                return await orig(endpoint=endpoint, **kwargs)
            return counting

        originals.append((transport, original))
        transport.unary = make(original)

    started = time.perf_counter()
    error = None
    try:
        await client.login_token()
        if config["gateway"]:
            await client.connect()
        elapsed = time.perf_counter() - started
        username = getattr(client.user, "username", "?")
        communities = len(client.communities)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        username, communities = "?", 0
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for transport, original in originals:
            transport.unary = original
        await client.close()

    return {
        "seconds": elapsed,
        "requests": sum(endpoints.values()),
        "endpoints": endpoints,
        "username": username,
        "communities": communities,
        "error": error,
    }


async def main() -> None:
    tokens = load_tokens()

    for index, token in enumerate(tokens, 1):
        log.info("")
        log.info("=" * 74)
        results: dict[str, list] = {name: [] for name, _cfg in MODES}

        for round_number in range(ROUNDS):
            # Alternate direction each round so ordering can't bias results.
            order = MODES if round_number % 2 == 0 else list(reversed(MODES))
            for name, config in order:
                outcome = await time_mode(token, config)
                results[name].append(outcome)
                status = outcome["error"] or (
                    f"{outcome['seconds'] * 1000:6.0f} ms, "
                    f"{outcome['requests']} requests"
                )
                log.info("  round %d  %-10s %s", round_number + 1, name, status)
                await asyncio.sleep(0.8)

        good = {
            name: [r for r in runs if not r["error"]]
            for name, runs in results.items()
        }
        sample = next(
            (r for runs in good.values() for r in runs if r["username"] != "?"),
            None,
        )
        username = sample["username"] if sample else f"account {index}"
        communities = max(
            (r["communities"] for runs in good.values() for r in runs),
            default=0,
        )

        log.info("")
        log.info(
            "ACCOUNT %s -- %d communities, median of %d rounds",
            username, communities, ROUNDS,
        )
        log.info("")
        log.info(
            "  %-10s %10s %10s %9s   %s",
            "mode", "median ms", "best ms", "requests", "calls made",
        )
        log.info("  " + "-" * 70)

        baseline = None
        for name, _config in MODES:
            runs = good.get(name) or []
            if not runs:
                log.info("  %-10s %10s", name, "failed")
                continue
            median_ms = statistics.median(r["seconds"] for r in runs) * 1000
            best_ms = min(r["seconds"] for r in runs) * 1000
            request_count = statistics.median(r["requests"] for r in runs)
            merged: Counter = sum((r["endpoints"] for r in runs), Counter())
            calls = ", ".join(
                f"{endpoint}x{count // len(runs)}"
                for endpoint, count in merged.most_common(5)
            )
            if baseline is None:
                baseline = median_ms
            log.info(
                "  %-10s %10.0f %10.0f %9.0f   %s",
                name, median_ms, best_ms, request_count, calls,
            )

        log.info("")
        log.info(
            "  Every RPC (send, history, admin, presence) works in ALL modes.\n"
            "  The gateway is only needed to RECEIVE pushed events."
        )
        log.info("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
