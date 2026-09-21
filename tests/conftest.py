"""Shared helpers for the offline test suite.

Every test in this directory runs without a token and without the network.
Nothing here talks to Root; the helpers below exist so that tests which poll
an in-process condition do not have to hand-roll the same loop.

    pip install -e ".[dev]"
    pytest
"""

from __future__ import annotations

import asyncio
import time

# Per-test timing in milliseconds, correlated with the SDK's transport
# counters. Off unless --timing is passed.
pytest_plugins = ["tests.timing_plugin"]


async def eventually(
    check,
    *,
    timeout: float = 20.0,
    interval: float = 0.4,
    describe: str = "condition",
):
    """Poll ``check`` until it returns something truthy, or give up.

    Root is eventually consistent for reads-after-writes, so a test that
    asserts one straight away is a coin flip. A fixed ``asyncio.sleep(2)`` is
    the usual answer and it is the wrong one twice over: it pays the full two
    seconds even when the value landed in 200 ms, and it still fails when the
    server happens to take longer.

    ``check`` may be sync or async; the truthy value it returns is passed back.
    """
    import inspect as _inspect

    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = check()
        if _inspect.isawaitable(last):
            last = await last
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{describe} did not become true within {timeout:.0f}s "
                f"(last value: {last!r})"
            )
        await asyncio.sleep(interval)

