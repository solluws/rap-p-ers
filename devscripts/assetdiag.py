"""
Work out what AssetGrpcService/Get actually wants.

Resolving a profile picture URI fails with INVALID_ARGUMENT, and the request
has only one field (repeated string uris), so it's the *values* Root objects
to -- not the shape. This tries the plausible variations against a real URI

and reports which one the server accepts.

    python assetdiag.py

Reads the token from tokens.txt, grabs a real avatar URI from your own
profile, and works from there.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy import RootClient
from rootpy.protocol import grpc_frame, iter_fields, string_field, unwrap_grpc_web

logging.basicConfig(level=logging.INFO, format="%(message)s")
for noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("assetdiag")

ASSET_GET = "https://api.rootapp.com/root.AssetGrpcService/Get"


def load_token() -> str:
    path = Path(__file__).with_name("tokens.txt")
    if not path.exists():
        raise SystemExit("Create tokens.txt with:  token=<your token>")
    for line in path.read_text(encoding="utf-8").splitlines():
        _key, _, value = line.strip().partition("=")
        if value.strip():
            return value.strip()
    raise SystemExit("No token found in tokens.txt")


def describe(exc: BaseException) -> str:
    parts = [type(exc).__name__]
    status = getattr(exc, "status", None)
    if status is not None:
        parts.append(getattr(status, "name", str(status)))
    detail = getattr(exc, "validation_errors", None)
    if detail:
        parts.append("; ".join(str(item) for item in detail))
    else:
        code = getattr(exc, "error_code", None)
        if code:
            parts.append(f"root-code={code}")
        parts.append("no structured detail")
    return " | ".join(parts)


def walk(data: bytes, depth: int = 0, path: str = "") -> None:
    """Print a protobuf message as a tree, so nothing can hide."""
    pad = "  " * (depth + 1)
    offset = 0
    while offset < len(data):
        tag = 0
        shift = 0
        while offset < len(data):
            byte = data[offset]
            offset += 1
            tag |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        field, wire = tag >> 3, tag & 7
        here = f"{path}.{field}" if path else str(field)

        if wire == 0:
            value = 0
            shift = 0
            while offset < len(data):
                byte = data[offset]
                offset += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            log.info("%sf%-3d varint  %s", pad, field, value)
        elif wire == 2:
            length = 0
            shift = 0
            while offset < len(data):
                byte = data[offset]
                offset += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            chunk = data[offset:offset + length]
            offset += length
            try:
                text = chunk.decode("utf-8")
                if text.isprintable() and len(text) >= 2:
                    marker = " <-- URL" if text.startswith("http") else ""
                    log.info("%sf%-3d str     %r%s", pad, field, text[:90], marker)
                    continue
            except UnicodeDecodeError:
                pass
            log.info("%sf%-3d msg[%d]", pad, field, length)
            if depth < 6 and length:
                walk(chunk, depth + 1, here)
        elif wire == 1:
            offset += 8
            log.info("%sf%-3d fixed64", pad, field)
        elif wire == 5:
            offset += 4
            log.info("%sf%-3d fixed32", pad, field)
        else:
            log.info("%sf%-3d wire=%d (stopping)", pad, field, wire)
            return


async def main() -> None:
    client = RootClient(token=load_token())
    await client.login_token()
    me = await client.whoami()
    log.info("logged in as %s\n", me.username)

    profile = await client.get_profile(me.id)
    uri = getattr(profile, "avatar_url", None) or getattr(profile, "banner_uri", None)
    log.info("asset uri: %s\n", uri)
    if not uri:
        log.info("No asset on your profile -- set an avatar first.")
        await client.close()
        return

    # 1) raw structure, so we can see exactly what those ~2 KB contain
    response = await client.assets.transport.unary(
        endpoint=ASSET_GET,
        body=string_field(1, uri),
        headers=client.assets._headers(),
        operation="AssetGet",
    )
    payload = unwrap_grpc_web(response.content)
    log.info("RAW RESPONSE STRUCTURE (%d bytes)", len(payload or b""))
    if payload:
        walk(payload)

    # 2) what the SDK's decoder makes of it
    log.info("")
    log.info("DECODED BY THE SDK")
    asset = await client.get_asset(uri)
    if asset is None:
        log.info("  decoder returned nothing")
    else:
        log.info("  url         : %s", asset.url)
        log.info("  best_url    : %s", asset.best_url)
        log.info("  links       : %d", len(asset.links))
        for link in asset.links:
            log.info("      %5dpx  %dx%d  %s",
                     link.max_dimension, link.width, link.height, link.url[:70])
        log.info("  expires_at  : %s", asset.expires_at)
        log.info("  animated    : %s", asset.is_animated)
        log.info("  mime        : %s", asset.mime_type)

    log.info("")
    if asset is not None and asset.best_url:
        log.info("RESULT: URLs are available -- client.asset_url(uri) works.")
    else:
        log.info(
            "RESULT: no URL in the response. Compare the tree above against\n"
            "  AssetInformation{1 url, 2 image{10 asset_links{10 url}}} to see\n"
            "  which field actually carries it."
        )

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
