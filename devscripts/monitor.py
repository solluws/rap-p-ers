"""example.py -- a tour of the merged rootpy SDK.

Everything here drives a single account you own. It shows:

  * token-first login (or username/password fallback)
  * flexible startup with auto-reconnect settings
  * events, multiple message listeners, and prefix commands
  * auto-react (hearts under your own messages)
  * the high-level verbs: message / reply / react / DM / profile / voice
  * the normalized error taxonomy (get_error_info / format_root_error)

Set your token (recommended) and run:

    export ROOT_TOKEN="....."     # or paste it into TOKEN below
    python example.py
"""

from __future__ import annotations

import asyncio
import logging
import os

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient
from rootpy.exceptions import RootError, format_root_error, get_error_info

# --- credentials -----------------------------------------------------------
# Preferred: a Root *client token* (your own account's session token). Treat it
# like a password -- anyone with it can act as you. Paste it here or, better,
# set the ROOT_TOKEN environment variable.
TOKEN = os.environ.get("ROOT_TOKEN") or ""

# Fallback: username/password, used only if no token is provided.
USERNAME = os.environ.get("ROOT_USERNAME", "your_username")
PASSWORD = os.environ.get("ROOT_PASSWORD", "your_password")

# --- fill in with real values from your account (placeholders are skipped) --
CHANNEL_ID = "PASTE-A-CHANNEL-ID"
VOICE_CHANNEL_ID = "PASTE-A-VOICE-CHANNEL-ID"
FRIEND_USER_ID = "PASTE-A-USER-ID"
FRIEND_USERNAME = "some_username"
AUDIO_FILE = "clip.mp3"

# Watch EVERY channel the account can see (plus DMs) for new messages, using
# Root's per-channel unread state. Efficient: one GetExtended per server per
# sweep, then fetch only channels that changed. Set False to disable.
WATCH_ALL_CHANNELS = True

# Sweep cadence in seconds. Community sweeps run concurrently, so this is close
# to the real detection latency for plain channel messages. Mentions and DMs
# arrive instantly regardless (pushed by the hub). Lower = faster = more calls.
WATCH_INTERVAL = 3.0

HEARTS = ["❤️", "🧡", "💛", "💚", "💙", "💜"]

# Set to DEBUG for the full firehose (every packet's fields, every HTTP request,
# websocket frames). INFO gives one clean line per event.
LOG_LEVEL = logging.INFO

logging.basicConfig(level=LOG_LEVEL)

# The HTTP/websocket libraries log every request and frame. That's useful when
# debugging the wire, and pure noise otherwise -- so it only shows at DEBUG.
_TRANSPORT_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.http2",
    "httpcore.connection",
    "hpack",
    "hpack.hpack",
    "hpack.table",
    "h2",
    "websockets",
    "websockets.client",
    "asyncio",
)
for _name in _TRANSPORT_LOGGERS:
    logging.getLogger(_name).setLevel(
        logging.DEBUG if LOG_LEVEL <= logging.DEBUG else logging.WARNING
    )


def _set(value) -> bool:
    return bool(value) and not str(value).startswith(("PASTE-", "your_", "some_"))


async def authenticate(client: RootClient) -> None:
    """Log in with a token when available, else username/password.

    Uses the REST login (no gateway) -- good for one-shot batches of actions.
    """
    if TOKEN:
        await client.login_token(TOKEN)
    elif _set(USERNAME) and _set(PASSWORD):
        await client.login(USERNAME, PASSWORD)
    else:
        raise RuntimeError(
            "No credentials: set ROOT_TOKEN (recommended) or "
            "ROOT_USERNAME/ROOT_PASSWORD."
        )


# ===========================================================================
#  Live client: a human-readable event monitor + commands + auto-react
# ===========================================================================
import logging as _logging


def _sid(uuid) -> str:
    """Short id for concise lines."""
    return (uuid or "?")[:8] if isinstance(uuid, str) else "?"


def _deep_strings(raw, _depth: int = 0) -> list[str]:
    """Recursively pull printable UTF-8 strings out of protobuf bytes.

    Message text sometimes lives in a nested ``payload`` (system messages,
    embeds, notification wrappers) rather than the top-level
    ``message_content``. This walks the wire format and collects any
    length-delimited field that decodes as clean text, so nothing a packet
    carries stays invisible.
    """
    out: list[str] = []
    if not isinstance(raw, (bytes, bytearray)) or _depth > 4:
        return out
    try:
        from rootpy.protocol import iter_fields
    except Exception:
        return out
    for _n, wire_type, value in iter_fields(raw):
        if wire_type != 2 or not isinstance(value, (bytes, bytearray)):
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            out.extend(_deep_strings(value, _depth + 1))
            continue
        if text and text.isprintable() and len(text) >= 2:
            out.append(text)
        else:
            out.extend(_deep_strings(value, _depth + 1))
    return out


def _content(pkt) -> str:
    """Best available text for a message-ish packet.

    Prefers the top-level ``message_content``; otherwise digs strings out of
    the nested ``payload`` (notifications wrap a whole message in there),
    dropping ``root://`` URIs and duplicates so only human text remains.
    """
    f = pkt.fields
    direct = f.get("message_content")
    if direct:
        return direct.replace("\n", " ")
    found: list[str] = []
    for key in ("payload", "message_uris", "reference_maps"):
        found.extend(_deep_strings(f.get(key)))
    if not found:
        data = pkt.data if isinstance(pkt.data, dict) else {}
        import binascii
        hexed = data.get("packet_raw_hex")
        if hexed:
            try:
                found = _deep_strings(binascii.unhexlify(hexed))
            except Exception:
                found = []
    # Keep human text: drop root:// URIs and de-dupe, preserving order.
    seen = set()
    cleaned = []
    for s in found:
        if s.startswith("root://") or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    return " | ".join(cleaned) if cleaned else ""


def _embeds_message(pkt) -> bool:
    """True if a NOTIFICATION carries a full message inside its payload.

    Those are surfaced as real [MSG] lines by the message listener, so the
    monitor stays quiet about them instead of printing the same event twice.
    """
    payload = (pkt.fields or {}).get("payload")
    if not isinstance(payload, (bytes, bytearray)):
        return False
    try:
        from rootpy.protocol import iter_fields
    except Exception:
        return False

    def _sub(data, number):
        for n, wire_type, value in iter_fields(data):
            if n == number and wire_type == 2 and isinstance(value, (bytes, bytearray)):
                return bytes(value)
        return None

    outer = _sub(payload, 6)
    inner = _sub(outer, 4) if outer else None
    if not inner:
        return False
    for n, wire_type, value in iter_fields(inner):
        if n == 10 and wire_type == 2 and value:
            return True
    return False


def _summarize(pkt) -> str | None:
    """One concise line for an event, or None to stay silent (e.g. pings)."""
    t = pkt.type.name
    f = pkt.fields

    # --- messages -----------------------------------------------------------
    if t == "MESSAGE":
        who = _sid(f.get("user_id"))
        where = _sid(f.get("container_id"))
        scope = "community" if f.get("community_id") else "dm"
        body = _content(pkt) or "(no text / attachment or embed)"
        tagm = "MSG edit" if f.get("edited_at") else "MSG"
        return f"[{tagm}] {who} in {where} ({scope}): {body[:100]}"
    if t == "MESSAGE_DELETED":
        return f"[MSG del] {_sid(f.get('id'))} in {_sid(f.get('container_id'))}"
    if t == "MESSAGE_REACTION":
        return (f"[REACT] {_sid(f.get('user_id'))} {f.get('shortcode')} "
                f"on {_sid(f.get('message_id'))}")
    if t == "MESSAGE_PIN":
        return f"[PIN] {_sid(f.get('message_id'))} in {_sid(f.get('container_id'))}"

    # --- direct messages ----------------------------------------------------
    if t == "DIRECT_MESSAGE_CREATED":
        members = f.get("member_user_ids")
        members = members if isinstance(members, list) else [members]
        return (f"[DM new] by {_sid(f.get('creator_user_id'))} "
                f"members={[_sid(m) for m in members if m]}")
    if t == "DIRECT_MESSAGE_RING":
        return f"[CALL] incoming from {_sid(f.get('caller_user_id'))}"
    if t == "DIRECT_MESSAGE_RING_DECLINED":
        return f"[CALL] declined ({_sid(f.get('id'))})"

    # --- friends & blocks ---------------------------------------------------
    if t == "FRIENDSHIP_CREATED":
        return f"[FRIEND +] {_sid(f.get('friend_user_id'))}"
    if t == "FRIENDSHIP_DELETED":
        return f"[FRIEND -] {_sid(f.get('friend_user_id'))}"
    if t == "USER_BLOCK_CREATED":
        return f"[BLOCK +] {_sid(f.get('block_user_id'))}"
    if t == "USER_BLOCK_DELETED":
        return f"[BLOCK -] {_sid(f.get('block_user_id'))}"

    # --- notifications & pings ---------------------------------------------
    if t == "NOTIFICATION":
        # If it carries a message (mention/DM), the [MSG] line covers it.
        if _embeds_message(pkt):
            return None
        preview = _content(pkt)
        where = _sid(f.get("container_id"))
        if preview:
            return f"[NOTIF] in {where}: {preview[:100]}"
        viewed = "viewed" if f.get("is_viewed") else "unviewed"
        return f"[NOTIF] {_sid(f.get('id'))} ({viewed})"
    if t == "NOTIFICATION_DELETED":
        return f"[NOTIF del] {_sid(f.get('id'))}"
    if t in ("NOTIFICATION_VIEWED", "NOTIFICATION_VIEWED_ALL"):
        return "[NOTIF] marked viewed"
    if t == "PING":
        return None  # heartbeat -- silent at INFO, shown verbosely at DEBUG

    # --- everything else ----------------------------------------------------
    strings = _content(pkt)
    ids = {k: _sid(v) for k, v in f.items() if isinstance(v, str)}
    extra = f" :: {strings[:60]}" if strings else ""
    return f"[{t}] seq={pkt.sequence} {ids}{extra}"


def build_client() -> RootClient:
    client = RootClient(
        token=TOKEN or None,
        command_prefix=">",
        auto_reconnect=True,
    )

    @client.event
    async def on_ready(_event) -> None:
        me = await client.whoami()
        print(f"== connected as {me.username} ({me.id}) ==")

    # One monitor for EVERY packet. The gateway fires "packet" for each one
    # with typed fields, so this sees messages, DMs, friends, blocks,
    # notifications, reactions, pings -- everything.
    @client.event
    async def on_packet(pkt) -> None:
        # Heartbeats are pure noise -- never print them (the websockets DEBUG
        # log still shows raw frames if you truly want them).
        if pkt.type.name == "PING":
            return
        debug = _logging.getLogger().isEnabledFor(_logging.DEBUG)
        if debug:
            # Verbose but READABLE: show scalar fields, and decode any nested
            # bytes payloads into their text instead of dumping raw b'...'.
            parts = []
            for k, v in pkt.fields.items():
                if isinstance(v, (bytes, bytearray)):
                    strings = _deep_strings(v)
                    v = strings if strings else f"<{len(v)} bytes>"
                parts.append(f"{k}={v!r}")
            summary = _summarize(pkt)
            print(f"[{pkt.type.name}] seq={pkt.sequence} case={pkt.case} "
                  f"{{{', '.join(parts)}}}")
            if summary:
                print(f"    -> {summary}")
        else:
            line = _summarize(pkt)
            if line is not None:
                print(line)

    # Keep a functional reply so you can test round-trips (not for logging).
    @client.event
    async def on_message(event) -> None:
        msg = event.message
        if msg.user_id != client.user_id and msg.content.strip() == "!ping":
            await client.reply(msg, "pong")

    # THE single place every detected message lands, no matter how it arrived:
    # pushed by the hub (mentions, DMs) or found by the unread sweep (any
    # channel in any server). Duplicates are already filtered by the client.
    async def print_message(msg) -> None:
        debug = _logging.getLogger().isEnabledFor(_logging.DEBUG)
        where = "DM" if not msg.community_id else "CHAN"
        if debug:
            fields = ", ".join(
                f"{k}={getattr(msg, k)!r}"
                for k in ("id", "user_id", "container_id", "community_id", "content")
            )
            print(f"[MSG/{where}] {fields}")
        else:
            print(
                f"[MSG] {msg.content[:120]}"
                f"  (id={(msg.id or '?')[:8]}"
                f" user={(msg.user_id or '?')[:8]}"
                f" channel={(msg.container_id or '?')[:8]}"
                f" server={(msg.community_id or 'dm')[:8]})"
            )

    client.add_message_listener(print_message)

    # This session can't fetch channel message text (the server rejects
    # MessageGrpcService/List for it), but the unread signal is still real --
    # so you still learn which channel got new messages, and when.
    @client.event
    async def on_channel_activity(activity) -> None:
        name = activity.channel_name or (activity.channel_id or "?")[:8]
        server = (activity.community_id or "?")[:8]
        print(f"[ACTIVITY] new messages in #{name}  (server={server})")

    # --- typed events: friendly objects, not raw packets ------------------ #
    # Each carries resolved names where known, plus .received_at and .raw.
    @client.event
    async def on_friend_request(event) -> None:
        print(f"[FRIEND REQ] {event.username or event.user_id} "
              f"at {event.received_at:%H:%M:%S}")

    @client.event
    async def on_member_join(event) -> None:
        print(f"[JOIN] {event.username or event.user_id} -> "
              f"{event.community_name or event.community_id}")

    @client.event
    async def on_member_leave(event) -> None:
        print(f"[LEAVE] {event.username or event.user_id} left "
              f"{event.community_name or event.community_id}")

    @client.event
    async def on_role_add(event) -> None:
        print(f"[ROLE +] {event.role_name or event.role_id} -> "
              f"{', '.join(event.user_ids)}")

    @client.event
    async def on_role_remove(event) -> None:
        print(f"[ROLE -] {event.role_name or event.role_id} from "
              f"{', '.join(event.user_ids)}")

    @client.event
    async def on_channel_create(event) -> None:
        category = f" in {event.category}" if event.category else ""
        print(f"[CHANNEL +] #{event.name}{category} "
              f"({event.community_name or event.community_id})")

    @client.event
    async def on_channel_delete(event) -> None:
        print(f"[CHANNEL -] {event.channel_id[:8]} "
              f"({event.community_name or event.community_id})")

    @client.command(name="hello")
    async def hello(ctx) -> None:
        await ctx.reply("hi!")

    # hearts under your own messages
    client.enable_auto_react(HEARTS)

    # WATCH EVERYTHING the account can see. Community sweeps run concurrently
    # (one GetExtended per server, 8 at a time), so detection stays fast even
    # with many servers; only channels whose activity advanced get fetched.
    # Mentions and DMs also arrive instantly via the hub push -- both paths
    # feed print_message above, deduplicated.
    if WATCH_ALL_CHANNELS:
        client.watch_unread(
            interval=WATCH_INTERVAL,
            include_dms=True,
            concurrency=8,
        )
        print(
            f"watching all channels + DMs (sweep every {WATCH_INTERVAL}s, "
            "mentions/DMs instant)"
        )

    return client


async def run_live() -> None:
    client = build_client()
    # start_token()/start() log in, connect, and block (through reconnects)
    # until the client is closed.
    if TOKEN:
        await client.start_token()          # reuses token=... from the client
    else:
        await client.start(USERNAME, PASSWORD)


# ===========================================================================
#  One-shot: log in, run a batch of account actions, log out
# ===========================================================================
async def run_one_shot() -> None:
    client = RootClient(token=TOKEN or None)
    try:
        await authenticate(client)

        me = await client.whoami()
        print(f"signed in as {me.username}")

        await client.update_profile(
            status="exploring rootpy",
            description="automating my own account",
        )

        if _set(CHANNEL_ID):
            sent = await client.message(CHANNEL_ID, "hello from rootpy")
            print("sent:", sent.id)

        if _set(FRIEND_USER_ID):
            await client.direct_message(client.get_user(FRIEND_USER_ID), "hey!")

        if _set(FRIEND_USERNAME):
            await client.add_friend(FRIEND_USERNAME)
        print("friends:", len(await client.list_friends()))

        print("communities:", len(await client.list_communities()))
        print("unread:", await client.unread_count())

        if _set(VOICE_CHANNEL_ID):
            await client.join_voice(VOICE_CHANNEL_ID)
            await client.unmute()
            if os.path.exists(AUDIO_FILE):
                await client.play(AUDIO_FILE)
                await asyncio.sleep(5)
                await client.stop_playing()
            await client.leave_voice()

    except RootError as exc:
        info = get_error_info(exc)
        print("operation failed:", info.summary, "| hint:", info.hint)
        print(format_root_error(exc, verbose=True))
    finally:
        await client.close()


if __name__ == "__main__":
    # Watch it live (recommended -- you'll see messages, commands, auto-react):
    # asyncio.run(run_live())

    # ...or run a one-shot batch of account actions:
    asyncio.run(run_one_shot())
