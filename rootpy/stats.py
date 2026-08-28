"""Where the time actually goes.

When a call feels slow it's useful to know *which* part was slow, and the
honest answer has three parts:

  waiting   -- time this client deliberately held the request: rate-limit
               cooldowns and retry backoff. Entirely our own doing.
  roundtrip -- from sending the bytes to having the response in hand. This is
               network plus server, and can't be split further from inside the
               process. On a connection's first request it also includes DNS
               and the TLS handshake, which is why one call can look far worse
               than the rest.
  overhead  -- our own work around the call: framing, header building,
               bookkeeping. Usually a rounding error; worth watching in case
               it isn't.

Decoding happens in the calling service rather than the transport, so it isn't
counted here -- ``client.timings()`` reports it separately by comparing total
call time against what the transport saw.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class EndpointStats:
    """Aggregated timings for one RPC endpoint."""

    calls: int = 0
    errors: int = 0
    retries: int = 0
    rate_limited: int = 0
    #: A *bounded* sample, kept only for the median. Never sum this -- see
    #: ``roundtrip_total``.
    roundtrip_ms: List[float] = field(default_factory=list)
    #: Exact running totals, unaffected by the sample's truncation. Anything
    #: that must stay correct past 1000 calls to one endpoint reads these.
    roundtrip_total: float = 0.0
    roundtrip_low: Optional[float] = None
    roundtrip_high: Optional[float] = None
    wait_ms: float = 0.0
    overhead_ms: float = 0.0

    def record(self, roundtrip: float, wait: float, overhead: float) -> None:
        self.calls += 1
        self.roundtrip_ms.append(roundtrip)
        # Accumulate exactly, *before* any truncation can drop the value.
        self.roundtrip_total += roundtrip
        if self.roundtrip_low is None or roundtrip < self.roundtrip_low:
            self.roundtrip_low = roundtrip
        if self.roundtrip_high is None or roundtrip > self.roundtrip_high:
            self.roundtrip_high = roundtrip
        self.wait_ms += wait
        self.overhead_ms += overhead
        # Keep the sample bounded -- we only need the distribution.
        if len(self.roundtrip_ms) > 1000:
            del self.roundtrip_ms[:500]

    @property
    def total_roundtrip_ms(self) -> float:
        """Every call's round trip, exactly.

        This used to be ``sum(self.roundtrip_ms)``, which silently summed the
        *bounded sample* rather than the calls. Past 1000 calls to one endpoint
        the truncation above starts discarding values, so the "total" converged
        toward ``1000 * mean`` no matter how many requests were actually made
        -- understating without bound. Because ``report()`` ranks endpoints by
        this number and ``summary()`` divides by it, a busy endpoint could be
        reported as *both* slower on average and lower-total than a quiet one
        it dominated.
        """
        return self.roundtrip_total

    def summary(self) -> dict:
        # Median is the one figure the bounded sample is genuinely for; total,
        # min and max come from the exact accumulators.
        samples = self.roundtrip_ms or [0.0]
        return {
            "calls": self.calls,
            "errors": self.errors,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "roundtrip_total_ms": round(self.roundtrip_total, 1),
            "roundtrip_median_ms": round(statistics.median(samples), 1),
            "roundtrip_min_ms": round(self.roundtrip_low or 0.0, 1),
            "roundtrip_max_ms": round(self.roundtrip_high or 0.0, 1),
            "wait_ms": round(self.wait_ms, 1),
            "overhead_ms": round(self.overhead_ms, 1),
        }


class TransportStats:
    """Per-endpoint request timings for one transport."""

    def __init__(self) -> None:
        self.endpoints: Dict[str, EndpointStats] = {}
        self.enabled = True

    @staticmethod
    def _key(endpoint: str) -> str:
        parts = [p for p in endpoint.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else endpoint

    def _entry(self, endpoint: str) -> EndpointStats:
        key = self._key(endpoint)
        entry = self.endpoints.get(key)
        if entry is None:
            entry = self.endpoints[key] = EndpointStats()
        return entry

    def record(self, endpoint: str, *, roundtrip_ms: float, wait_ms: float = 0.0,
               overhead_ms: float = 0.0) -> None:
        if self.enabled:
            self._entry(endpoint).record(roundtrip_ms, wait_ms, overhead_ms)

    def note_error(self, endpoint: str) -> None:
        if self.enabled:
            self._entry(endpoint).errors += 1

    def note_retry(self, endpoint: str) -> None:
        if self.enabled:
            self._entry(endpoint).retries += 1

    def note_rate_limited(self, endpoint: str) -> None:
        if self.enabled:
            self._entry(endpoint).rate_limited += 1

    # ------------------------------------------------------------------ #
    def totals(self) -> dict:
        """Everything added up across endpoints."""
        calls = sum(e.calls for e in self.endpoints.values())
        roundtrip = sum(e.total_roundtrip_ms for e in self.endpoints.values())
        wait = sum(e.wait_ms for e in self.endpoints.values())
        overhead = sum(e.overhead_ms for e in self.endpoints.values())
        return {
            "calls": calls,
            "errors": sum(e.errors for e in self.endpoints.values()),
            "retries": sum(e.retries for e in self.endpoints.values()),
            "rate_limited": sum(e.rate_limited for e in self.endpoints.values()),
            "roundtrip_ms": round(roundtrip, 1),
            "waiting_ms": round(wait, 1),
            "overhead_ms": round(overhead, 1),
            "avg_roundtrip_ms": round(roundtrip / calls, 1) if calls else 0.0,
        }

    def summary(self) -> dict:
        """Totals plus a per-endpoint breakdown."""
        return {
            "totals": self.totals(),
            "endpoints": {
                key: entry.summary()
                for key, entry in sorted(
                    self.endpoints.items(),
                    key=lambda item: item[1].total_roundtrip_ms,
                    reverse=True,
                )
            },
        }

    def report(self) -> str:
        """A readable table -- print this when something feels slow."""
        totals = self.totals()
        if not totals["calls"]:
            # A client that was only ever throttled has real activity to
            # report even though no request completed. Saying "no requests
            # recorded" there hides exactly the situation you opened the
            # report to investigate.
            noted = totals["rate_limited"] + totals["errors"] + totals["retries"]
            if noted:
                return (
                    "no completed requests: "
                    f"{totals['rate_limited']} rate-limited, "
                    f"{totals['errors']} error(s), "
                    f"{totals['retries']} retry(ies)"
                )
            return "no requests recorded"

        lines = [
            f"{totals['calls']} request(s): "
            f"{totals['roundtrip_ms']:.0f}ms round trip, "
            f"{totals['waiting_ms']:.0f}ms waiting (rate limits/retries), "
            f"{totals['overhead_ms']:.0f}ms client overhead",
        ]
        if totals["errors"] or totals["retries"] or totals["rate_limited"]:
            lines.append(
                f"  {totals['errors']} error(s), {totals['retries']} retry(ies), "
                f"{totals['rate_limited']} rate-limited"
            )
        lines.append(
            f"  {'endpoint':<44} {'calls':>5} {'median':>8} {'total':>9}"
        )
        for key, entry in sorted(
            self.endpoints.items(),
            key=lambda item: item[1].total_roundtrip_ms, reverse=True,
        ):
            data = entry.summary()
            lines.append(
                f"  {key[:44]:<44} {data['calls']:>5} "
                f"{data['roundtrip_median_ms']:>7.0f}m {data['roundtrip_total_ms']:>8.0f}m"
            )
        return "\n".join(lines)

    def reset(self) -> None:
        self.endpoints.clear()
