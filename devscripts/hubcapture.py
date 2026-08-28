"""SUPERSEDED -- the question this answered is settled, and it has a real cost.

It existed to find what the desktop client's hub connection did that rootpy's
did not. The answer turned out not to be on the wire at all: it is a gRPC call,
``root.CommunityGrpcService/Attach``, found by reading the decompiled client.
See HANDOFF, "Membership is not a subscription".

**Do not run this without a reason.** It installs nothing itself, but it needs
mitmproxy's CA in the Windows user Root store -- trusted for all TLS on the
machine -- and it restarts the Root client behind a proxy. On the machine this
was written on, that combination (with a VPN also active) left the client
unable to reach the network at all until a reboot.

Original notes follow.

mitmproxy addon: capture the Root desktop client's hub websocket, redacted.

Answers one question: what does the real client's hub connection do that ours
does not? No community-scoped packet (channel-created 30, channel-edited 31,
community-role 90, message 170) ever reaches a rootpy gateway connection, while
user-scoped ones (DM 120, notification 180, user-status 191) arrive fine. Every
comparable detail already matches the decompiled client, so the difference has
to be visible on the wire.

**This is not a rootpy script** -- it runs inside mitmproxy, not Python
directly:

    pip install mitmproxy
    mitmdump -s devscripts/hubcapture.py -w hub.flows

Then, on Windows:

  1. Start the command above and leave it running.
  2. Browse to http://mitm.it through the proxy and install the certificate
     into **Trusted Root Certification Authorities** (the Root client uses the
     Windows store and does not pin -- checked in
     `RootHttpHandlerUtility.Create`, which sets only EnabledSslProtocols).
  3. Settings -> Network & Internet -> Proxy -> Manual, 127.0.0.1 : 8080.
  4. Start the Root desktop client and sign in.
  5. From a *second* account, post a message in a channel the first account
     can see. Wait ~15 s.
  6. Stop mitmdump. Send `hub-capture.txt`.
  7. **Turn the Windows proxy back off.**

Everything is written to `hub-capture.txt` in the working directory.

## What it redacts

Tokens are account access. This never writes one:

  * `authorization`, `cookie`, `set-cookie`, `x-root-device-id` and anything
    with "token" in the name become `<redacted:N chars>`;
  * a `sequence_number` query value is kept (it is the thing under
    investigation), everything else in the query is kept as-is -- check the
    file before sending if the URL might carry anything private;
  * frame *payloads* are written as hex, and message text inside them is not
    decoded here. Hex of a chat message is still readable by someone who
    wants to, so do not capture in a channel you would not paste.

## What it keeps

  * the websocket upgrade: full URL, every header name and value (redacted
    ones excepted), and the response status;
  * every frame: direction, opcode, length, and the first 160 bytes as hex --
    enough to read the oneof case number without carrying whole messages.
"""

from __future__ import annotations

import time

OUT = "hub-capture.txt"
HUB_HINTS = ("hub", "socket", "ws")
REDACT = ("authorization", "cookie", "set-cookie", "x-root-device-id",
          "sec-websocket-key", "sec-websocket-accept")
MAX_HEX = 160


def _redact(name: str, value: str) -> str:
    lowered = name.lower()
    if lowered in REDACT or "token" in lowered or "secret" in lowered:
        return f"<redacted:{len(value)} chars>"
    return value


def _write(line: str = "") -> None:
    with open(OUT, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _is_hub(flow) -> bool:
    url = flow.request.pretty_url.lower()
    return flow.request.scheme in ("ws", "wss", "http", "https") and (
        any(hint in url for hint in HUB_HINTS)
    )


def websocket_start(flow):
    if not _is_hub(flow):
        return
    _write("=" * 72)
    _write(f"WEBSOCKET OPEN  {time.strftime('%H:%M:%S')}")
    _write(f"  url    {flow.request.pretty_url}")
    _write(f"  method {flow.request.method}")
    _write("  request headers:")
    for name, value in flow.request.headers.items():
        _write(f"    {name}: {_redact(name, value)}")
    if flow.response is not None:
        _write(f"  response status {flow.response.status_code}")
        _write("  response headers:")
        for name, value in flow.response.headers.items():
            _write(f"    {name}: {_redact(name, value)}")
    _write("-" * 72)


def websocket_message(flow):
    if not _is_hub(flow):
        return
    message = flow.websocket.messages[-1]
    direction = "CLIENT->HUB" if message.from_client else "HUB->CLIENT"
    payload = bytes(message.content)
    head = payload[:MAX_HEX].hex()
    more = "" if len(payload) <= MAX_HEX else f" ...(+{len(payload)-MAX_HEX}B)"
    kind = "binary" if message.is_text is False else "text"
    _write(f"[{time.strftime('%H:%M:%S')}] {direction} {kind} "
           f"{len(payload)}B  {head}{more}")


def websocket_end(flow):
    if not _is_hub(flow):
        return
    close = getattr(flow.websocket, "close_code", None)
    reason = getattr(flow.websocket, "close_reason", "")
    _write(f"WEBSOCKET CLOSED code={close} reason={reason!r}")
    _write("=" * 72)
    _write()


def running():
    _write()
    _write("#" * 72)
    _write(f"# capture started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _write("#" * 72)
