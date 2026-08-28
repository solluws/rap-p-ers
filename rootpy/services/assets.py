from __future__ import annotations

import asyncio
import logging
import base64
import hashlib
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..protocol import iter_fields, string_field, unwrap_grpc_web
from urllib.parse import urljoin

from ..transport import GrpcWebTransport


@dataclass(frozen=True)
class AssetLink:
    """One downloadable URL for an asset, at a particular size."""

    url: str
    max_dimension: int = 0          # largest_dimension_limit
    width: int = 0
    height: int = 0

    def __str__(self) -> str:
        return self.url


@dataclass(frozen=True)
class Asset:
    """A resolved asset: its URLs, sizes, and when they stop working.

    Root serves images through short-lived links rather than stable public
    paths, so ``expires_at`` matters -- store the bytes if you need to keep
    something, not the URL.
    """

    uri: str
    url: Optional[str] = None            # the direct/original URL
    links: tuple = ()                    # AssetLink, smallest first
    mime_type: Optional[str] = None
    is_animated: bool = False
    expires_at: Optional[float] = None   # epoch seconds
    width: int = 0
    height: int = 0

    @property
    def best_url(self) -> Optional[str]:
        """Highest-resolution URL available."""
        if self.links:
            return self.links[-1].url
        return self.url

    def url_for_size(self, pixels: int) -> Optional[str]:
        """Smallest link at least ``pixels`` across, else the largest."""
        for link in self.links:
            if link.max_dimension >= pixels:
                return link.url
        return self.best_url

    def __str__(self) -> str:
        return self.best_url or self.uri


def _wrap_image(image_bytes: bytes) -> bytes:
    """Present a legacy AssetImage as an AssetInformation (image at field 2)."""
    from ..protocol import length_field

    return length_field(2, image_bytes)


def _extension_for(data: bytes) -> str:
    for magic, ext in (
        (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
        (b"GIF8", ".gif"), (b"RIFF", ".webp"), (b"BM", ".bmp"),
    ):
        if data.startswith(magic):
            return ".webp" if magic == b"RIFF" and data[8:12] == b"WEBP" else ext
    return ".bin"


def _timestamp_seconds(data: bytes) -> Optional[float]:
    seconds = nanos = 0
    for number, wire, value in iter_fields(data):
        if wire != 0:
            continue
        if number == 1:
            seconds = int(value)
        elif number == 2:
            nanos = int(value)
    if not seconds and not nanos:
        return None
    return seconds + nanos / 1_000_000_000


def _decode_asset(uri: str, data: bytes) -> Asset:
    """AssetInformation -> Asset.

    Field numbers straight from AssetInformationReflection:
      AssetInformation: 1 url, 2 image, 5 file, 10 link_expires_at
      AssetImage:       10 asset_links (repeated), 12 aspect_ratio, 13 is_animated
      AssetImageLink:   10 url, 11 largest_dimension_limit, 12 width, 13 height
      AssetFile:        1 url, 2 mime_type
    """
    url = None
    links: list = []
    mime = None
    animated = False
    expires = None
    width = height = 0

    for number, wire, value in iter_fields(data):
        if number == 1 and wire == 2:
            url = bytes(value).decode("utf-8", errors="replace")
        elif number == 2 and wire == 2:                      # image
            for sub, sub_wire, sub_value in iter_fields(bytes(value)):
                if sub == 10 and sub_wire == 2:              # asset_links
                    link_url = None
                    limit = link_w = link_h = 0
                    for ln, lw, lv in iter_fields(bytes(sub_value)):
                        if ln == 10 and lw == 2:
                            link_url = bytes(lv).decode("utf-8", errors="replace")
                        elif ln == 11 and lw == 0:
                            limit = int(lv)
                        elif ln == 12 and lw == 0:
                            link_w = int(lv)
                        elif ln == 13 and lw == 0:
                            link_h = int(lv)
                    if link_url:
                        links.append(
                            AssetLink(link_url, limit, link_w, link_h)
                        )
                elif sub == 13 and sub_wire == 0:            # is_animated
                    animated = bool(sub_value)
        elif number == 5 and wire == 2:                      # file
            for fn, fw, fv in iter_fields(bytes(value)):
                if fn == 1 and fw == 2 and url is None:
                    url = bytes(fv).decode("utf-8", errors="replace")
                elif fn == 2 and fw == 2:
                    mime = bytes(fv).decode("utf-8", errors="replace")
        elif number == 10 and wire == 2:                     # link_expires_at
            expires = _timestamp_seconds(bytes(value))

    links.sort(key=lambda link: (link.max_dimension, link.width))
    if links:
        width, height = links[-1].width, links[-1].height
    return Asset(
        uri=uri, url=url, links=tuple(links), mime_type=mime,
        is_animated=animated, expires_at=expires, width=width, height=height,
    )


ASSET_GET = "https://api.rootapp.com/root.AssetGrpcService/Get"

log = logging.getLogger("rootpy.assets")


class AssetService:
    """Uploading files, and resolving Root's asset URIs to real URLs."""

    def __init__(
        self,
        transport,
        token_getter,
        web_api_url_getter,
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self._web_api_url_getter = web_api_url_getter

    def _headers(self) -> dict:
        return {
            "user-agent": (
                "grpc-dotnet/2.83.0 (.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "te": "trailers",
            "grpc-accept-encoding": "identity,gzip,deflate",
            "authorization": f"Bearer {self._token_getter()}",
            "content-type": "application/grpc-web",
        }

    async def get(self, uris, *, chunk_size: int = 5,
                  concurrency: int = 16) -> dict:
        """Resolve asset URIs to :class:`Asset` objects -> ``{uri: Asset}``.

            assets = await client.assets.get([profile.banner_uri])
            print(assets[profile.banner_uri].best_url)

        Requests are chunked and run ``concurrency`` at a time; a chunk Root
        refuses is retried one URI at a time so a single bad value does not
        lose the rest.

        **Small chunks, wide concurrency -- the opposite of the usual advice.**
        Everywhere else in this SDK, bigger batches win, because a round trip
        costs ~190 ms almost regardless of payload (see
        ``get_members_detailed``, 100 -> 500 per request). AssetGet inverts
        that, for a reason worth knowing: **one URI Root dislikes rejects the
        entire request**, and about **6% of the asset URIs on real profiles
        are rejected** -- stale references that individually answer
        ``Uris: The specified condition was not met for 'Uris'``. Measured on
        120 real URIs: 113 returned data, 7 were refused, and a batch of
        9 good + 1 refused was refused whole.

        So the bigger the chunk, the likelier it contains a poison URI, and
        the whole chunk then degrades to one-request-per-URI. Measured over
        200 real URIs, 3 rounds each, zero rate limiting:

            chunk=100 conc=16   18.56 s     chunk=25  conc=8   5.06 s
            chunk=50  conc=8     9.55 s     chunk=10  conc=8   2.62 s  (old)
            chunk=5   conc=8     2.26 s     chunk=5   conc=16  1.63 s  (now)

        1.6x faster than the previous default. Concurrency past 16 flattens
        out -- 24, 32 and 48 were indistinguishable (1.55 / 1.44 / 1.53 s) --
        so the extra rate-limit exposure buys nothing.

        Pass a larger ``chunk_size`` only if you know every URI is live.
        """
        if isinstance(uris, str):
            uris = [uris]
        wanted = [
            u for u in dict.fromkeys(uris)
            if isinstance(u, str) and u.startswith("root://")
        ]
        if not wanted:
            return {}

        chunks = [
            wanted[i:i + max(1, chunk_size)]
            for i in range(0, len(wanted), max(1, chunk_size))
        ]
        limiter = asyncio.Semaphore(max(1, concurrency))

        async def fetch(chunk) -> dict:
            async with limiter:
                try:
                    return await self._get_chunk(chunk)
                except Exception as exc:
                    detail = getattr(exc, "validation_errors", None)
                    log.debug(
                        "AssetGet failed for %d uri(s): %s",
                        len(chunk),
                        "; ".join(str(d) for d in detail) if detail else exc,
                    )
            if len(chunk) == 1:
                return {}
            recovered: dict = {}
            for uri in chunk:
                async with limiter:
                    try:
                        recovered.update(await self._get_chunk([uri]))
                    except Exception:
                        continue
            return recovered

        out: dict = {}
        for result in await asyncio.gather(
            *(fetch(chunk) for chunk in chunks), return_exceptions=True
        ):
            if isinstance(result, dict):
                out.update(result)
        return out

    async def _get_chunk(self, uris) -> dict:
        """One AssetGet call. Response is map<uri, AssetInformation>."""
        body = bytearray()
        for uri in uris:
            body += string_field(1, uri)

        response = await self.transport.unary(
            endpoint=ASSET_GET,
            body=bytes(body),
            headers=self._headers(),
            operation="AssetGet",
        )

        found: dict = {}
        payload = unwrap_grpc_web(response.content)
        if payload is None:
            return found

        # AssetGetResponse: 1 = legacy map<string, AssetImage>,
        #                   2 = map<string, AssetInformation>.
        # Map entries are key(1) / value(2).
        for number, wire, value in iter_fields(payload):
            if wire != 2 or number not in (1, 2):
                continue
            key = None
            info = b""
            for sub, sub_wire, sub_value in iter_fields(bytes(value)):
                if sub == 1 and sub_wire == 2:
                    key = bytes(sub_value).decode("utf-8", errors="replace")
                elif sub == 2 and sub_wire == 2:
                    info = bytes(sub_value)
            if not key:
                continue
            if number == 1:
                # legacy map holds an AssetImage directly -- wrap it so the
                # same decoder handles both shapes.
                info = _wrap_image(info)
            found[key] = _decode_asset(key, info)
        return found

    # These must track :meth:`get`'s defaults. They are spelled out rather
    # than left to the callee because this is a keyword forwarder: a stale
    # default here silently overrides the tuned one below, which is the exact
    # shape `devscripts/defaultcheck.py` exists to catch.
    async def resolve(self, uris, *, chunk_size: int = 5, strict: bool = False,
                      size: Optional[int] = None, concurrency: int = 16) -> dict:
        """URLs for asset URIs -> ``{uri: url}``.

        A convenience wrapper over :meth:`get` when all you want is a link.
        Non-``root://`` values pass through unchanged, so a mixed list is fine.

        size: pick the smallest variant at least this many pixels across
            (e.g. ``size=128`` for thumbnails). Default is the largest.

        The URLs are signed and time-limited -- see ``Asset.expires_at``. If
        you need something permanent, download the bytes.
        """
        if isinstance(uris, str):
            uris = [uris]
        passthrough = {
            u: u for u in uris
            if isinstance(u, str) and u and not u.startswith("root://")
        }
        assets = await self.get(
            uris, chunk_size=chunk_size, concurrency=concurrency
        )
        resolved = {}
        for uri, asset in assets.items():
            url = asset.url_for_size(size) if size else asset.best_url
            if url:
                resolved[uri] = url
        if strict:
            missing = [
                u for u in uris
                if isinstance(u, str) and u.startswith("root://")
                and u not in resolved
            ]
            if missing:
                raise RuntimeError(f"could not resolve {len(missing)} asset(s)")
        return {**passthrough, **resolved}

    @staticmethod
    def uri_for_id(asset_id, kind: str = "image") -> str:
        """Build a ``root://asset/...`` URI from a bare asset id.

        Several records carry an ``asset_id`` rather than a URI -- a community
        file's, for one -- and every method here takes URIs, so there was no
        way to get from one to the other without knowing the encoding.

        It is a bare 16-byte GUID followed by a protobuf-framed *kind* tag::

            root://asset/<base64url( guid_bytes + 0x0A <len> kind )>

        Verified by reconstructing two known-good URIs exactly (a profile
        picture and a banner) from the ids in the same records.

        ``kind`` is not cosmetic: it selects which derivative Root resolves,
        and the derivatives live on different backends.

        * ``"image"`` resolves to a signed ``imagedelivery.net`` URL carrying
          ``exp`` and ``sig``, and downloads.
        * ``"file"`` -- the only tag a community file's asset resolves under --
          answers a ``static.rootapp.com`` URL with **no signature**, which is
          then refused with HTTP 403. See
          :meth:`~rootpy.domain_managers.CommunityFileManager.download`.

        So a URI this returns is not a promise the bytes are reachable.
        """
        digits = str(asset_id).replace("-", "")
        try:
            raw = bytes.fromhex(digits)
        except ValueError:
            # fromhex's own message names a character position in a string the
            # caller never wrote, which is not something you can act on.
            raise ValueError(
                f"asset id should be a 16-byte GUID in hex, got {asset_id!r}"
            ) from None
        if len(raw) != 16:
            raise ValueError(
                f"asset id should be a 16-byte GUID, got {len(raw)} bytes "
                f"from {asset_id!r}"
            )
        tag = kind.encode("utf-8")
        payload = raw + b"\x0a" + bytes([len(tag)]) + tag
        return "root://asset/" + (
            base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        )

    async def url_for(self, uri) -> Optional[str]:
        """The best URL for one asset URI, or None."""
        if not uri:
            return None
        return (await self.resolve([uri])).get(uri)

    async def download(self, uri) -> Optional[bytes]:
        """Fetch an asset's bytes over its (short-lived) URL."""
        url = await self.url_for(uri)
        if not url:
            return None
        # Root's CDN is still Root: fetching an asset direct while the API
        # call that produced its URL went through a proxy puts the real
        # address on the same account, seconds apart.
        async with self.transport.open_plain_client() as http:
            response = await http.get(url, timeout=30.0)
            response.raise_for_status()
            return response.content

    async def save(self, uri, path) -> Optional[str]:
        """Download an asset to disk; returns the path written, or None."""
        data = await self.download(uri)
        if not data:
            return None
        target = Path(path).expanduser()
        if not target.suffix:
            target = target.with_suffix(_extension_for(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        return str(target)

    async def upload_file(self, source) -> str:
        """Upload a file from disk and return its asset token URI."""
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(str(path))

        data = await asyncio.to_thread(path.read_bytes)
        stat = await asyncio.to_thread(path.stat)
        modified = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%SZ")
        return await self.upload_bytes(
            data, filename=path.name, modified=modified
        )

    async def upload_bytes(
        self,
        data: bytes,
        *,
        filename: str = "upload.png",
        modified: Optional[str] = None,
    ) -> str:
        """Upload raw bytes and return the asset token URI.

        Useful when the image never touches disk -- generated art, something
        just downloaded, an in-memory crop.
        """
        if not data:
            raise ValueError("no data to upload")

        md5 = hashlib.md5(data).digest()
        sha256 = hashlib.sha256(data).digest()
        if modified is None:
            modified = datetime.now(tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%SZ"
            )
        headers = {
            "user-agent": (
                "grpc-dotnet/2.83.0 "
                "(.NET 10.0.10; CLR 10.0.10; "
                "net10.0; windows; x64)"
            ),
            "authorization": f"Bearer {self._token_getter()}",
            "content-md5": base64.b64encode(md5).decode("ascii"),
            "content-length": str(len(data)),
            "x-amz-meta-root-content-sha256": (
                base64.b64encode(sha256).decode("ascii")
            ),
            "x-amz-meta-root-file-name-base64": (
                base64.b64encode(
                    filename.encode("utf-8")
                ).decode("ascii")
            ),
            "x-amz-meta-root-file-modification": modified,
        }

        try:
            filename.encode("ascii")
        except UnicodeEncodeError:
            pass
        else:
            headers["x-amz-meta-root-file-name"] = filename

        base_url = self._web_api_url_getter()
        endpoint = urljoin(
            base_url.rstrip("/") + "/",
            "asset/upload",
        )

        client = self.transport._get_client()
        response = await client.post(
            endpoint,
            headers=headers,
            content=data,
            timeout=1800.0,
        )
        response.raise_for_status()

        token_uri = response.json()
        if not isinstance(token_uri, str) or not token_uri:
            raise RuntimeError(
                "Asset upload returned an invalid token URI"
            )
        if not token_uri.startswith("root://"):
            raise RuntimeError(
                "Asset upload returned a non-Root URI: "
                f"{token_uri!r}"
            )

        return token_uri
