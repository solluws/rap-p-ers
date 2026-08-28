"""
Two-account receive test.

One account (the WATCHER) connects and watches everything; the other (the
SENDER) posts uniquely-tagged messages into a channel they share. The harness
then reports, per message, whether the watcher saw it -- and by which path

(gateway push vs. unread sweep) and how long it took.

Everything is written to log.txt.

SETUP
-----
Create a file next to this script called tokens.txt with two lines:

    watcher=<token of the account that observes>
    sender=<token of the account that posts>

(or set ROOT_WATCHER_TOKEN / ROOT_SENDER_TOKEN in the environment)

Both accounts must be members of TARGET_COMMUNITY and able to see
TARGET_CHANNEL.

    python twotest.py

Then send me log.txt.
"""

import asyncio
import logging
import os
import sys
import time
import uuid
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient

# --- what to test against ------------------------------------------------- #
TARGET_COMMUNITY = "0030735e-ddbf-8d02-99de-255bc3fc5dc5"
TARGET_CHANNEL = "00308801-dfec-8404-bb84-bf060b2169ba"

MESSAGE_COUNT = 3          # how many test messages to send
SEND_GAP = 4.0             # seconds between sends
SETTLE_SECONDS = 12.0      # how long to keep watching after the last send
BASELINE_SECONDS = 8.0     # let the watcher establish its baseline first

LOG_PATH = Path(__file__).with_name("log.txt")


# --- logging: everything to log.txt, quiet the transport noise ------------- #
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Transport chatter (every HTTP request, every websocket frame) is only useful
# when debugging the wire itself -- keep it out of the harness log.
for noisy in (
    "httpcore", "httpcore.http2", "hpack", "hpack.hpack", "hpack.table",
    "httpx", "h2", "websockets", "websockets.client", "asyncio",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("twotest")


def load_tokens() -> tuple[str, str]:
    watcher = os.environ.get("ROOT_WATCHER_TOKEN", "")
    sender = os.environ.get("ROOT_SENDER_TOKEN", "")
    path = Path(__file__).with_name("tokens.txt")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key == "watcher" and not watcher:
                watcher = value
            elif key == "sender" and not sender:
                sender = value
    if not watcher or not sender:
        raise SystemExit(
            "Need both tokens. Create tokens.txt next to this script:\n"
            "    watcher=<token>\n"
            "    sender=<token>"
        )
    return watcher, sender


async def main() -> None:
    watcher_token, sender_token = load_tokens()
    run_id = uuid.uuid4().hex[:6]

    # markers -> {sent_at, seen_at, path}
    expected: dict[str, dict] = {}

    # ---------------- watcher ---------------- #
    watcher = RootClient(token=watcher_token, auto_reconnect=True)

    async def record(message, path: str) -> None:
        content = (message.content or "").strip()
        for marker, info in expected.items():
            if marker in content and info["seen_at"] is None:
                info["seen_at"] = time.time()
                info["path"] = path
                info["message_id"] = message.id
                delay = info["seen_at"] - info["sent_at"]
                log.info(
                    "RECEIVED %s via %s after %.2fs (id=%s channel=%s)",
                    marker, path, delay, message.id[:8],
                    (message.container_id or "?")[:8],
                )
                return
        log.debug(
            "other message via %s: %r (channel=%s)",
            path, content[:60], (message.container_id or "?")[:8],
        )

    async def on_any_message(message) -> None:
        # Both the gateway push path and the sweep dispatch through here; the
        # gateway attaches no marker, so tag by which one is currently active.
        await record(message, "message-listener")

    watcher.add_message_listener(on_any_message)

    @watcher.event
    async def on_packet(pkt) -> None:
        if pkt.type.name != "PING":
            log.debug("packet seq=%s type=%s", pkt.sequence, pkt.type.name)

    @watcher.event
    async def on_channel_activity(activity) -> None:
        log.info(
            "ACTIVITY in #%s (%s) -- no text available",
            activity.channel_name, (activity.channel_id or "?")[:8],
        )

    log.info("=== starting watcher ===")
    # NOTE: start_token() ends with gateway.wait_closed(), which blocks
    # forever -- it never returns, so the sender would never run. Use the
    # non-blocking pair instead: authenticate, then bring the gateway up.
    await watcher.login_token()
    await watcher.connect()
    me = await watcher.whoami()
    log.info("watcher logged in as %s (%s)", me.username, me.id)

    watcher.watch_unread(interval=3.0, include_dms=True)
    log.info("watcher sweeping; letting it baseline for %.0fs", BASELINE_SECONDS)
    await asyncio.sleep(BASELINE_SECONDS)

    # ---------------- sender ---------------- #
    # The sender only needs to authenticate -- sending is a plain API call, no
    # gateway required. (Using login_token here, not start_token, for the same
    # blocking reason as above.)
    sender = RootClient(token=sender_token)
    await sender.login_token()
    sender_me = await sender.whoami()
    log.info(
        "sender logged in as %s (%s) -- user_id set: %s",
        sender_me.username, sender_me.id, sender.user_id is not None,
    )

    log.info("=== sending %d test message(s) ===", MESSAGE_COUNT)
    for index in range(1, MESSAGE_COUNT + 1):
        marker = f"rootpy-{run_id}-{index}"
        expected[marker] = {
            "sent_at": time.time(),
            "seen_at": None,
            "path": None,
            "message_id": None,
            "sent_ok": False,
            "error": None,
        }
        try:
            result = await sender.messages.send(
                TARGET_CHANNEL,
                f"{marker} (rootpy two-account test)",
                community_id=TARGET_COMMUNITY,
            )
            expected[marker]["sent_ok"] = True
            log.info("SENT %s -> %s", marker, result)
        except Exception as exc:
            expected[marker]["error"] = f"{type(exc).__name__}: {exc}"
            log.error("SEND FAILED %s -> %s", marker, exc)
        if index < MESSAGE_COUNT:
            await asyncio.sleep(SEND_GAP)

    log.info("=== watching for %.0fs ===", SETTLE_SECONDS)
    await asyncio.sleep(SETTLE_SECONDS)

    # ---------------- report ---------------- #
    log.info("")
    log.info("================ RESULT ================")
    sent_ok = sum(1 for i in expected.values() if i["sent_ok"])
    seen = sum(1 for i in expected.values() if i["seen_at"] is not None)
    log.info("sent OK : %d/%d", sent_ok, len(expected))
    log.info("received: %d/%d", seen, len(expected))
    for marker, info in expected.items():
        if info["error"]:
            log.info("  %s  SEND FAILED: %s", marker, info["error"])
        elif info["seen_at"] is None:
            log.info("  %s  sent but NEVER received", marker)
        else:
            log.info(
                "  %s  received in %.2fs via %s",
                marker, info["seen_at"] - info["sent_at"], info["path"],
            )

    if sent_ok == 0:
        log.info("VERDICT: sending is broken -- MessageCreate failed.")
    elif seen == 0:
        log.info(
            "VERDICT: messages send fine but the watcher never sees them. "
            "Receive path is broken."
        )
    elif seen < sent_ok:
        log.info("VERDICT: partial receive -- some messages missed.")
    else:
        log.info("VERDICT: ALL messages sent and received. Pipeline works.")
    log.info("========================================")

    await sender.close()
    await watcher.close()


if __name__ == "__main__":
    asyncio.run(main())
