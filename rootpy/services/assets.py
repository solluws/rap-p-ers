from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

from ..transport import GrpcWebTransport


class AssetService:
    def __init__(
        self,
        transport: GrpcWebTransport,
        token_getter: Callable[[], str],
        web_api_url_getter: Callable[[], str],
    ) -> None:
        self.transport = transport
        self._token_getter = token_getter
        self._web_api_url_getter = web_api_url_getter

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
