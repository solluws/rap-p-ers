from __future__ import annotations

import asyncio
import random
import logging
import time

import httpx
from typing import Optional
from urllib.parse import unquote

from .exceptions import (
    GrpcWebError,
    TurnstileRequired,
    make_grpc_error,
    make_http_error,
)
log = logging.getLogger("rootpy.transport")


def _new_stats():
    from .stats import TransportStats

    return TransportStats()


class _StatsProxy:
    """No-op stand-in when a transport was built without __init__."""

    def record(self, *a, **k): pass
    def note_error(self, *a, **k): pass
    def note_retry(self, *a, **k): pass
    def note_rate_limited(self, *a, **k): pass
    def totals(self): return {"calls": 0, "roundtrip_ms": 0.0, "waiting_ms": 0.0,
                              "overhead_ms": 0.0, "errors": 0, "retries": 0,
                              "rate_limited": 0, "avg_roundtrip_ms": 0.0}
    def summary(self): return {"totals": self.totals(), "endpoints": {}}
    def report(self): return "no requests recorded"
    def reset(self): pass

from .protocol import grpc_frame, is_grpc_framed, iter_grpc_web_frames


class GrpcWebTransport:
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_base: float = 0.35,
        max_connections: int = 200,
        max_keepalive_connections: int = 100,
    ) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        # endpoint -> loop time until which that endpoint is cooling down
        self._cooldowns: dict[str, float] = {}
        # Where time goes: waiting on ourselves vs the round trip.
        self.stats = _new_stats()
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_base = max(0.0, float(retry_base))
        self.max_connections = max(1, int(max_connections))
        self.max_keepalive_connections = max(
            0,
            min(
                int(max_keepalive_connections),
                self.max_connections,
            ),
        )

    async def __aenter__(self):
        self._get_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
            )
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=self.timeout,
                follow_redirects=False,
                limits=limits,
            )
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def _endpoint_key(endpoint: str) -> str:
        """service/Method -- so cooldowns are per RPC, not per whole host."""
        parts = [p for p in endpoint.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else endpoint

    async def _respect_rate_limit(self, endpoint: str) -> None:
        """Wait out any cooldown this endpoint is under.

        The server tells us to back off via 429 + Retry-After; rather than
        just retrying the one call, we remember the cooldown per endpoint so
        *every* caller waits instead of piling on more 429s. This matters once
        a sweep is fanning out across many communities.
        """
        cooldowns = getattr(self, "_cooldowns", None)
        if not cooldowns:
            return
        key = self._endpoint_key(endpoint)
        while True:
            until = cooldowns.get(key)
            if not until:
                return
            remaining = until - asyncio.get_running_loop().time()
            if remaining <= 0:
                cooldowns.pop(key, None)
                return
            log.debug(
                "rate limit: holding %s for %.2fs", key, remaining,
            )
            await asyncio.sleep(min(remaining, 5.0))

    def _note_rate_limit(self, endpoint: str, retry_after: float) -> None:
        if getattr(self, "_cooldowns", None) is None:
            self._cooldowns = {}
        key = self._endpoint_key(endpoint)
        self._cooldowns[key] = (
            asyncio.get_running_loop().time() + max(0.0, retry_after)
        )
        log.warning(
            "rate limited on %s -- backing off %.2fs", key, retry_after,
        )

    @property
    def stats(self):
        value = self.__dict__.get("_stats")
        if value is None:
            value = self.__dict__["_stats"] = _StatsProxy()
        return value

    @stats.setter
    def stats(self, value):
        self.__dict__["_stats"] = value

    async def unary(self, *, endpoint: str, body: bytes, headers: dict[str, str], operation: str) -> httpx.Response:
        # Every gRPC-web request body must be framed (1-byte flag + 4-byte
        # big-endian length). Doing it here means no call site can forget --
        # an unframed body reaches the server as garbage and the handler
        # throws a bare UNKNOWN(2) "Exception was thrown by handler", which is
        # extremely hard to diagnose from the outside. Framing is idempotent:
        # already-framed bodies (from older call sites) pass through untouched.
        started = time.perf_counter()
        if not is_grpc_framed(body):
            body = grpc_frame(body)

        await self._respect_rate_limit(endpoint)
        # Everything up to here is us: framing plus any cooldown we imposed.
        waited = time.perf_counter() - started
        roundtrip = 0.0
        attempt = 0
        while True:
            attempt += 1
            sent_at = time.perf_counter()
            try:
                response = await self._get_client().post(endpoint, headers=headers, content=body)
                roundtrip += time.perf_counter() - sent_at
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                roundtrip += time.perf_counter() - sent_at
                if attempt <= self.max_retries:
                    self.stats.note_retry(endpoint)
                    backoff = self.retry_base * (2 ** (attempt - 1)) + random.random() * 0.1
                    await asyncio.sleep(backoff)
                    waited += backoff
                    continue
                self.stats.note_error(endpoint)
                self.stats.record(endpoint, roundtrip_ms=roundtrip * 1000,
                                  wait_ms=waited * 1000)
                raise

            if response.status_code != 200:
                error = make_http_error(operation, response.status_code, response.text[:200], response_headers=dict(response.headers))
                retryable = response.status_code in {429, 502, 503, 504}
                if retryable and attempt <= self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    try: delay = float(retry_after)
                    except (TypeError, ValueError): delay = self.retry_base * (2 ** (attempt - 1)) + random.random() * 0.1
                    if response.status_code == 429:
                        self._note_rate_limit(endpoint, delay)
                        self.stats.note_rate_limited(endpoint)
                    self.stats.note_retry(endpoint)
                    await asyncio.sleep(max(0.0, delay))
                    waited += max(0.0, delay)
                    continue
                self.stats.note_error(endpoint)
                self.stats.record(endpoint, roundtrip_ms=roundtrip * 1000,
                                  wait_ms=waited * 1000)
                raise error

            grpc_status = response.headers.get("grpc-status")
            grpc_message = response.headers.get("grpc-message", "")
            challenge_url = response.headers.get("turnstile-challenge-url")
            for flag, frame in iter_grpc_web_frames(response.content):
                if not (flag & 0x80): continue
                trailer_text = frame.decode("utf-8", errors="replace")
                for line in trailer_text.replace("\r\n", "\n").split("\n"):
                    if not line or ":" not in line: continue
                    name, value = line.split(":", 1); name=name.strip().casefold(); value=value.strip()
                    if name == "grpc-status": grpc_status=value
                    elif name == "grpc-message": grpc_message=unquote(value)
                    elif name == "turnstile-challenge-url": challenge_url=value
            if grpc_status is not None and grpc_status != "0":
                if challenge_url: raise TurnstileRequired(challenge_url)
                error = make_grpc_error(operation, grpc_status, grpc_message, response_headers=dict(response.headers))
                retryable = str(grpc_status) in {"4", "8", "10", "13", "14"}
                if retryable and attempt <= self.max_retries:
                    self.stats.note_retry(endpoint)
                    backoff = self.retry_base * (2 ** (attempt - 1)) + random.random()*0.1
                    await asyncio.sleep(backoff)
                    waited += backoff
                    continue
                self.stats.note_error(endpoint)
                self.stats.record(endpoint, roundtrip_ms=roundtrip * 1000,
                                  wait_ms=waited * 1000)
                raise error

            # Anything in this call that wasn't waiting or the round trip is
            # our own overhead (framing, trailer parsing, bookkeeping).
            total = time.perf_counter() - started
            overhead = max(0.0, total - roundtrip - waited)
            self.stats.record(
                endpoint,
                roundtrip_ms=roundtrip * 1000,
                wait_ms=waited * 1000,
                overhead_ms=overhead * 1000,
            )
            return response
