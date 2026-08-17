"""
Diagnose why MessageGrpcService/List fails for this session.

Run this once and send the output. It isolates the variable in a single run
instead of one guess per round-trip:

  * GetExtended  -- known working; gives us a real channel + community id
  * PinList      -- takes the SAME ContainerId + CommunityId as List and
                    nothing else. If PinList works, our ids and their
                    encoding are provably fine, and the problem is specific
                    to List's other fields (direction / dateAt).
  * List         -- every candidate request shape, reported individually
  * SetViewTime  -- another call on the same container, as a second control

Usage:
    python diagnose.py
(fill in TOKEN below, or set ROOT_TOKEN in the environment)
"""

import asyncio
import logging
import os
import sys

from rootpy import RootClient
from rootpy.identifiers import encode_root_guid, normalize_root_guid
from rootpy.protocol import encode_varint, field_key, length_field
from rootpy.services.messages import MESSAGE_LIST

TOKEN = os.environ.get("ROOT_TOKEN", "PASTE-YOUR-TOKEN-HERE")

# The deciding test posts one short message via v2.MessageGrpcService/Create.
# It writes a real message to the chosen channel, so it's opt-in:
#     python diagnose.py --send
# (or set TEST_SEND = True here, or ROOT_TEST_SEND=1 in the environment)
TEST_SEND = (
    "--send" in sys.argv
    or os.environ.get("ROOT_TEST_SEND", "").strip() in ("1", "true", "True")
)

# Keep the transport noise out of the way -- we only want our own lines.
logging.basicConfig(level=logging.INFO, format="%(message)s")
for noisy in ("httpcore", "hpack", "httpx", "websockets", "websockets.client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _err(exc: BaseException) -> str:
    """One-line error description, including Root's structured detail if any."""
    parts = [f"{type(exc).__name__}"]
    for attr in ("status_code", "grpc_message"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={value}")
    info = getattr(exc, "root_exception", None)
    if info is not None:
        parts.append(f"root_detail={info.summary()!r}")
    else:
        parts.append("root_detail=<none: unhandled server exception>")
    text = str(exc)
    if text and text not in parts:
        parts.append(text[:120])
    return " | ".join(parts)


async def main() -> None:
    client = RootClient(token=TOKEN)
    await client.login_token()
    me = await client.whoami()
    print(f"logged in as {me.username} ({me.id})\n")

    communities = await client.list_communities()
    print(f"communities: {len(communities)}")
    if not communities:
        print("no communities to test against")
        await client.close()
        return

    # Inventory every channel, with its type -- message RPCs only work on
    # text-capable containers (TEXT=1, THREADED_TEXT=2; VOICE=4, APP=8 have no
    # message container and crash the handler).
    TYPE_NAMES = {0: "unspecified", 1: "TEXT", 2: "THREADED_TEXT", 4: "VOICE", 8: "APP"}
    inventory = []
    for community in communities:
        try:
            ext = await client.fetch_community(community.id)
        except Exception as exc:
            print(f"  GetExtended {community.id[:8]} FAILED: {_err(exc)}")
            continue
        for group in ext.channel_groups:
            for channel in group.channels:
                inventory.append((community, channel))

    print(f"channels visible: {len(inventory)}")
    for community, channel in inventory:
        ctype = int(getattr(channel, "channel_type", 0) or 0)
        print(
            f"  [{TYPE_NAMES.get(ctype, ctype):<13}] #{(channel.name or '?')[:28]:<28} "
            f"{channel.id[:8]} activity={channel.last_activity_at}"
        )

    text_channels = [
        (c, ch)
        for c, ch in inventory
        if int(getattr(ch, "channel_type", 0) or 0) in (1, 2)
    ]
    print(f"\ntext-capable channels: {len(text_channels)}")

    # PinList against EVERY text channel -- cheapest control (ids only).
    print("\nCONTROL PinList across text channels (grpc-web):")
    ok_channels = []
    for community, channel in text_channels[:8]:
        try:
            await client.messages.pin_list(channel.id, community_id=community.id)
            print(f"  OK      #{channel.name}")
            ok_channels.append((community, channel))
        except Exception as exc:
            short = str(exc).split("[Root:")[0][:60]
            print(f"  FAILED  #{channel.name} -> {short}")

    if not text_channels:
        print("\nNo text channels visible to this account -- nothing to test.")
        await client.close()
        return

    # Prefer a channel that passed the control; otherwise the first text one.
    community, channel = (ok_channels or text_channels)[0]
    ids_ok = bool(ok_channels)

    print(f"\ntesting List against:")
    print(f"  community : {community.id}")
    print(f"  channel   : {channel.id}  (#{channel.name})")
    print(f"  type      : {TYPE_NAMES.get(int(getattr(channel,'channel_type',0) or 0))}")
    print(f"  activity  : {channel.last_activity_at}")
    print(f"  viewed    : {channel.user_last_viewed_at}\n")

    # --- CONTROL 2: SetViewTime (same container, write path) --------------- #
    print("CONTROL SetViewTime (same container):")
    try:
        await client.messages.set_view_time(
            channel.id, community_id=community.id
        )
        print("  OK -- container is addressable.\n")
    except Exception as exc:
        print(f"  FAILED: {_err(exc)}\n")

    # --- THE TEST: List, every shape, reported individually ---------------- #
    import time

    now = time.time()
    viewed = channel.user_last_viewed_at
    viewed_ts = viewed.timestamp() if viewed else now

    def build(direction, date_at, limit):
        body = bytearray()
        body += length_field(10, encode_root_guid(normalize_root_guid(channel.id)))
        body += length_field(11, encode_root_guid(normalize_root_guid(community.id)))
        if direction is not None:
            body += field_key(12, 0) + encode_varint(direction)
        if date_at is not None:
            ts = field_key(1, 0) + encode_varint(int(date_at))
            body += length_field(13, bytes(ts))
        if limit is not None:
            body += length_field(15, field_key(1, 0) + encode_varint(int(limit)))
        return bytes(body)

    shapes = [
        ("OLDER + now",              (2, now, None)),
        ("OLDER + user_last_viewed", (2, viewed_ts, None)),
        ("NEWER + user_last_viewed", (1, viewed_ts, None)),
        ("BOTH  + user_last_viewed", (3, viewed_ts, None)),
        ("BOTH  + now",              (3, now, None)),
        ("OLDER + now + limit 20",   (2, now, 20)),
        ("direction only (no date)", (2, None, None)),
        ("date only (no direction)", (None, now, None)),
        ("bare (ids only)",          (None, None, None)),
    ]

    print("MessageList shapes:")
    worked = []

    # The reference client is grpc-dotnet, which speaks NATIVE gRPC
    # (application/grpc) over HTTP/2. This SDK speaks grpc-web. Every service
    # that works for us is a v1 service; root.v2.MessageGrpcService is the only
    # v2 one and the only one failing -- so it may simply not be behind the
    # grpc-web translation layer. Try each shape under both content types.
    base_headers = client.messages._headers()
    native_headers = dict(base_headers)
    native_headers["content-type"] = "application/grpc"
    native_headers["accept"] = "application/grpc"

    for ct_label, hdrs in (
        ("grpc-web", base_headers),
        ("native  ", native_headers),
    ):
        print(f"\n  --- content-type: {hdrs['content-type']} ---")
        for label, shape in shapes:
            body = build(*shape)
            try:
                response = await client.messages.transport.unary(
                    endpoint=MESSAGE_LIST,
                    body=body,
                    headers=hdrs,
                    operation="MessageList",
                )
                msgs = client.messages._parse_message_list(response.content)
                print(f"    OK      {label:<28} -> {len(msgs)} message(s)")
                worked.append((f"{ct_label} {label}", shape, hdrs["content-type"]))
            except Exception as exc:
                short = str(exc).split("[Root:")[0][:60]
                print(f"    FAILED  {label:<28} -> {short}")

    # --- THE DECIDING TEST: can this account use v2.MessageGrpcService at
    # all? Every read method (List/PinList/SetViewTime) fails. If Create also
    # fails, the whole service is unavailable to this account. If Create
    # succeeds, the service is reachable and only reads are blocked.
    if TEST_SEND:
        print("\nDECIDING TEST -- MessageCreate (sends a real message):")
        try:
            result = await client.messages.send(
                channel.id,
                "rootpy diagnostic test",
                community_id=community.id,
            )
            print(f"  OK -- sent. v2.MessageGrpcService IS reachable: {result}")
            print("  => reads are blocked, writes are not.")
        except Exception as exc:
            print(f"  FAILED: {_err(exc)}")
            print("  => the ENTIRE v2 message service is unavailable to this account.")
    else:
        print(
            "\nDECIDING TEST skipped (TEST_SEND = False).\n"
            "  Set TEST_SEND = True to have the script post one short message\n"
            "  to the channel above. That single call distinguishes 'reads are\n"
            "  blocked' from 'the whole message service is unavailable'."
        )

    # --- HEADER VARIATIONS ------------------------------------------------- #
    # Every v2 method fails for every account while v1 services work, so the
    # difference is likely in what we send *around* the request. The real
    # client's channel carries a ClientIdentity (UserId + DeviceId +
    # CommunityId); this SDK sends x-root-Device-Id on the websocket but not
    # on gRPC calls. Try the identity headers here.
    print("\nHEADER VARIATIONS (bare ids-only request, grpc-web):")
    device_id = getattr(client, "device_id", None) or getattr(
        getattr(client, "gateway", None), "device_id", ""
    )
    bare_body = build(None, None, None)
    variations = [
        ("baseline", {}),
        ("+ x-root-Device-Id", {"x-root-Device-Id": str(device_id)}),
        ("+ Root user-agent", {"user-agent": "Root/0.9.126"}),
        (
            "+ device + Root UA",
            {"x-root-Device-Id": str(device_id), "user-agent": "Root/0.9.126"},
        ),
        (
            "+ device + user + community",
            {
                "x-root-Device-Id": str(device_id),
                "x-root-User-Id": str(getattr(client, "user_id", "") or ""),
                "x-root-Community-Id": str(community.id),
            },
        ),
    ]
    for label, extra in variations:
        hdrs = dict(base_headers)
        hdrs.update(extra)
        try:
            response = await client.messages.transport.unary(
                endpoint=MESSAGE_LIST,
                body=bare_body,
                headers=hdrs,
                operation="MessageList",
            )
            msgs = client.messages._parse_message_list(response.content)
            print(f"  OK      {label:<28} -> {len(msgs)} message(s)")
            worked.append((f"headers {label}", (None, None, None), "grpc-web"))
        except Exception as exc:
            short = str(exc).split("[Root:")[0][:60]
            print(f"  FAILED  {label:<28} -> {short}")

    print()
    if worked:
        print("RESULT: List WORKS. Winning combination(s):")
        for label, shape, content_type in worked:
            print(
                f"  content-type={content_type}  {label}\n"
                f"      (direction, date_at, limit) = {shape}"
            )
    elif ids_ok:
        print(
            "RESULT: PinList SUCCEEDED on this channel but every List shape failed.\n"
            "The ids and encoding are correct -- List specifically is refused."
        )
    else:
        print(
            "RESULT: even PinList (ids only) fails on every text channel.\n"
            "Message RPCs as a whole are unavailable to this account, which is a\n"
            "server-side decision -- not a request-format problem."
        )

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
