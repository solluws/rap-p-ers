"""
Measure login/startup cost: eager community expansion vs lazy (the new default).

Methodology note: running one account eager and another lazy would confound
the *mode* with the *account* -- an account in 15 communities has far more to
expand than one in 2. So every account is run in BOTH modes and acts as its own

control, alternating which mode goes first to cancel out warm-up and network
drift, and repeating for ROUNDS to average out variance.

Measured per run:
  * wall-clock time for login (auth + whatever preloading the mode does)
  * number of HTTP requests issued
  * which endpoints those were

SETUP
-----
tokens.txt next to this script, one token per line:

    token=<first account token>
    token2=<second account token>

(``watcher=``/``sender=`` names from twotest.py also work.)

    python benchmark.py

Results print to the console and to log.txt.
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

ROUNDS = 3          # repetitions per account per mode
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

log = logging.getLogger("benchmark")


def load_tokens() -> list[str]:
    path = Path(__file__).with_name("tokens.txt")
    if not path.exists():
        raise SystemExit(
            "Create tokens.txt next to this script:\n"
            "    token=<first token>\n"
            "    token2=<second token>"
        )
    tokens: list[str] = []
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


async def timed_login(token: str, *, eager: bool) -> dict:
    """Log in once and report how long it took and what it requested."""
    client = RootClient(
        token=token,
        expand_communities=eager,
        preload_direct_messages=eager,
    )

    # Count requests by wrapping the transport, without changing behaviour.
    endpoints: Counter = Counter()
    shared = {client.users.transport}
    for service in (
        client.users, client.messages, client.community_service,
        client.direct_messages,
    ):
        shared.add(getattr(service, "transport", None))
    shared.discard(None)

    originals = []
    for transport in shared:
        original = transport.unary

        def make(orig):
            async def counting(*, endpoint, **kwargs):
                endpoints[endpoint.rsplit("/", 1)[-1]] += 1
                return await orig(endpoint=endpoint, **kwargs)
            return counting

        originals.append((transport, original))
        transport.unary = make(original)

    started = time.perf_counter()
    try:
        await client.login_token()
        elapsed = time.perf_counter() - started
        username = getattr(client.user, "username", "?")
        communities = len(client.communities)
        error = None
    except Exception as exc:
        elapsed = time.perf_counter() - started
        username, communities, error = "?", 0, f"{type(exc).__name__}: {exc}"
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
    log.info("benchmarking %d account(s), %d rounds each, both modes\n", len(tokens), ROUNDS)

    summary = []

    for index, token in enumerate(tokens, 1):
        results = {"lazy": [], "eager": []}

        for round_number in range(ROUNDS):
            # Alternate which mode runs first so ordering can't bias the result.
            modes = ["lazy", "eager"] if round_number % 2 == 0 else ["eager", "lazy"]
            for mode in modes:
                outcome = await timed_login(token, eager=(mode == "eager"))
                results[mode].append(outcome)
                if outcome["error"]:
                    log.info("  account %d %-5s round %d: FAILED %s",
                             index, mode, round_number + 1, outcome["error"])
                else:
                    log.info(
                        "  account %d (%s) %-5s round %d: %6.0f ms, %2d requests",
                        index, outcome["username"], mode, round_number + 1,
                        outcome["seconds"] * 1000, outcome["requests"],
                    )
                await asyncio.sleep(1.0)   # be polite between logins

        good = {
            mode: [r for r in runs if not r["error"]]
            for mode, runs in results.items()
        }
        if not good["lazy"] or not good["eager"]:
            log.info("  account %d: not enough successful runs to compare\n", index)
            continue

        username = good["lazy"][0]["username"]
        communities = good["lazy"][0]["communities"] or good["eager"][0]["communities"]
        stats = {}
        for mode, runs in good.items():
            stats[mode] = {
                "median_ms": statistics.median(r["seconds"] for r in runs) * 1000,
                "best_ms": min(r["seconds"] for r in runs) * 1000,
                "requests": statistics.median(r["requests"] for r in runs),
                "endpoints": sum((r["endpoints"] for r in runs), Counter()),
            }
        summary.append((index, username, communities, stats))

        log.info("")
        log.info("  account %d (%s) -- %d communities", index, username, communities)
        for mode in ("eager", "lazy"):
            entry = stats[mode]
            top = ", ".join(
                f"{name}x{count // len(good[mode])}"
                for name, count in entry["endpoints"].most_common(4)
            )
            log.info(
                "    %-5s median %6.0f ms (best %6.0f)  %4.1f requests   %s",
                mode, entry["median_ms"], entry["best_ms"], entry["requests"], top,
            )
        saved_ms = stats["eager"]["median_ms"] - stats["lazy"]["median_ms"]
        saved_req = stats["eager"]["requests"] - stats["lazy"]["requests"]
        if stats["eager"]["median_ms"]:
            pct = saved_ms / stats["eager"]["median_ms"] * 100
        else:
            pct = 0.0
        log.info(
            "    -> lazy saves %.0f ms (%.0f%%) and %.0f request(s)\n",
            saved_ms, pct, saved_req,
        )

    if summary:
        log.info("=" * 64)
        log.info("SUMMARY")
        log.info(
            "%-22s %10s %10s %9s %9s",
            "account", "eager ms", "lazy ms", "eager req", "lazy req",
        )
        for index, username, communities, stats in summary:
            log.info(
                "%-22s %10.0f %10.0f %9.0f %9.0f",
                f"{username} ({communities} comms)",
                stats["eager"]["median_ms"], stats["lazy"]["median_ms"],
                stats["eager"]["requests"], stats["lazy"]["requests"],
            )
        log.info("=" * 64)
        log.info(
            "Both modes end up with the same capabilities -- lazy just defers "
            "per-community\nexpansion until something actually needs it "
            "(client.community_detail caches it)."
        )


if __name__ == "__main__":
    asyncio.run(main())
