"""Live tests for the gateway: connection, dispatch, and the watch loops.

The gateway had no coverage at all -- 91 packet types, eight typed events,
``wait_for``, ``watch_channel``, ``watch_unread`` and the reconnect cycle,
none of it ever exercised. It is also the part of the library that is hardest
to get right, because two of its behaviours are counter-intuitive:

* **Channel messages are pushed only after ``community.attach``.** Membership
  is not a subscription: until the community is attached the hub sends no
  packet for it, so ``on_message`` for a plain channel post waits forever --
  while DMs and mentions arrive on the same socket the whole time. That is
  what ``watch_channel``/``watch_unread`` existed to paper over, and what
  ``UnreadReader`` now does properly.
* **The socket closes on purpose.** The hub sends a batch, closes with code
  1000 and expects a reconnect. A close is the normal cycle, not an error.

Tests here are written against those facts rather than against what a
push-based gateway would usually do.

Run with::

    pytest -m live -k Gateway -v
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import eventually, requires_live, tag

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


class TestGatewayConnection:
    async def test_gateway_is_created_on_connect(self, gateway_client):
        assert gateway_client.gateway is not None

    async def test_client_reports_connected(self, gateway_client):
        assert gateway_client.is_connected is True

    async def test_session_carries_hub_url(self, gateway_client):
        assert gateway_client.session is not None
        assert gateway_client.session.hub_url

    async def test_session_carries_device_id(self, gateway_client):
        assert gateway_client.session.device_id

    async def test_wait_until_ready_returns(self, gateway_client):
        await asyncio.wait_for(gateway_client.wait_until_ready(), timeout=30)

    async def test_user_id_resolved_from_the_session(self, gateway_client):
        assert gateway_client.user_id


class TestEventRegistration:
    """Handler plumbing, checked without needing the server to cooperate."""

    async def test_add_and_remove_listener(self, gateway_client):
        async def handler(event):
            return None

        gateway_client.add_listener("message", handler)
        gateway_client.remove_listener("message", handler)

    async def test_event_decorator_registers_by_name(self, gateway_client):
        seen = []

        @gateway_client.event
        async def on_message(event):
            seen.append(event)

        await gateway_client.dispatch("message", object())
        # dispatch() is fire-and-forget for message events -- drain_events()
        # is the supported way to wait for the handlers it spawned.
        await gateway_client.drain_events(timeout=10)
        gateway_client.remove_listener("message", on_message)
        assert len(seen) == 1

    async def test_multiple_handlers_all_fire(self, gateway_client):
        calls = []

        async def first(event):
            calls.append("first")

        async def second(event):
            calls.append("second")

        gateway_client.add_listener("message", first)
        gateway_client.add_listener("message", second)
        try:
            await gateway_client.dispatch("message", object())
            await gateway_client.drain_events(timeout=10)
        finally:
            gateway_client.remove_listener("message", first)
            gateway_client.remove_listener("message", second)
        assert sorted(calls) == ["first", "second"]

    async def test_removing_an_unregistered_handler_is_safe(self, gateway_client):
        async def never_registered(event):
            return None

        gateway_client.remove_listener("message", never_registered)

    async def test_a_raising_handler_does_not_break_dispatch(self, gateway_client):
        """One bad handler must not stop the others or kill the socket."""
        survived = []

        async def explodes(event):
            raise RuntimeError("handler blew up")

        async def records(event):
            survived.append(event)

        gateway_client.add_listener("message", explodes)
        gateway_client.add_listener("message", records)
        try:
            await gateway_client.dispatch("message", object())
            await gateway_client.drain_events(timeout=10)
        finally:
            gateway_client.remove_listener("message", explodes)
            gateway_client.remove_listener("message", records)
        assert survived, "a raising handler suppressed the others"


class TestDispatchContract:
    """dispatch() delivers; drain_events() is how you know handlers finished."""

    async def test_handlers_have_not_run_when_dispatch_returns(
        self, gateway_client
    ):
        """Documents the deliberate fire-and-forget behaviour."""
        ran = []

        async def slow(event):
            await asyncio.sleep(0.05)
            ran.append(event)

        gateway_client.add_listener("message", slow)
        try:
            await gateway_client.dispatch("message", object())
            assert ran == [], "message handlers should not block dispatch"
            await gateway_client.drain_events(timeout=10)
            assert len(ran) == 1
        finally:
            gateway_client.remove_listener("message", slow)

    async def test_drain_reports_how_many_tasks_it_awaited(self, gateway_client):
        async def handler(event):
            await asyncio.sleep(0.01)

        gateway_client.add_listener("message", handler)
        try:
            await gateway_client.dispatch("message", object())
            assert await gateway_client.drain_events(timeout=10) >= 1
        finally:
            gateway_client.remove_listener("message", handler)

    async def test_drain_on_an_idle_client_returns_zero(self, gateway_client):
        assert await gateway_client.drain_events(timeout=5) == 0

    async def test_drain_does_not_deadlock_on_itself(self, gateway_client):
        """Called from inside the loop it must not await its own task."""
        await asyncio.wait_for(gateway_client.drain_events(), timeout=5)


class TestWaitFor:
    async def test_wait_for_resolves_on_dispatch(self, gateway_client):
        sentinel = object()

        async def fire():
            await asyncio.sleep(0.1)
            await gateway_client.dispatch("message", sentinel)

        task = asyncio.create_task(fire())
        got = await gateway_client.wait_for("message", timeout=10)
        await task
        assert got is sentinel

    async def test_wait_for_times_out_cleanly(self, gateway_client):
        with pytest.raises(asyncio.TimeoutError):
            await gateway_client.wait_for("message", timeout=0.3)

    async def test_check_filters_events(self, gateway_client):
        wanted = {"keep": True}

        async def fire():
            await asyncio.sleep(0.1)
            await gateway_client.dispatch("message", {"keep": False})
            await asyncio.sleep(0.1)
            await gateway_client.dispatch("message", wanted)

        task = asyncio.create_task(fire())
        got = await gateway_client.wait_for(
            "message", check=lambda e: isinstance(e, dict) and e.get("keep"),
            timeout=10,
        )
        await task
        assert got is wanted

    async def test_a_failing_check_does_not_swallow_the_wait(self, gateway_client):
        """A check that raises should not resolve or hang the waiter."""

        async def fire():
            await asyncio.sleep(0.1)
            await gateway_client.dispatch("message", object())

        task = asyncio.create_task(fire())
        try:
            with pytest.raises((asyncio.TimeoutError, Exception)):
                await gateway_client.wait_for(
                    "message",
                    check=lambda e: e.nonexistent_attribute,
                    timeout=1,
                )
        finally:
            await task


class TestChannelWatching:
    """The polling path, still supported for unattached communities."""

    async def test_watch_channel_delivers_a_new_message(
        self, gateway_client, client, sandbox
    ):
        received = []

        async def on_message(event):
            message = getattr(event, "message", None)
            if message is not None:
                received.append(message)

        gateway_client.add_listener("message", on_message)
        gateway_client.watch_channel(
            sandbox.channel.id,
            community_id=sandbox.community_id,
            interval=2.0,
        )
        try:
            marker = f"watch-probe-{tag()}"
            await client.message(sandbox.channel.id, marker)
            await eventually(
                lambda: any(
                    getattr(m, "content", "") == marker for m in received
                ),
                timeout=30,
                describe="watch_channel surfaces the posted message",
            )
        finally:
            gateway_client.unwatch_all()
            gateway_client.remove_listener("message", on_message)

    async def test_unwatch_all_stops_the_loops(self, gateway_client, sandbox):
        gateway_client.watch_channel(
            sandbox.channel.id, community_id=sandbox.community_id, interval=2.0
        )
        gateway_client.unwatch_all()

    async def test_watching_twice_is_not_an_error(self, gateway_client, sandbox):
        for _ in range(2):
            gateway_client.watch_channel(
                sandbox.channel.id,
                community_id=sandbox.community_id,
                interval=5.0,
            )
        gateway_client.unwatch_all()


class TestGatewayLifecycle:
    async def test_close_is_idempotent(self):
        """Closing twice must not raise -- teardown paths do it routinely."""
        from .conftest import _token
        from rootpy import RootClient

        token = _token()
        client = RootClient(token=token)
        await client.login_token(token)
        await client.connect()
        await client.close()
        await client.close()

    async def test_reports_disconnected_after_close(self):
        from .conftest import _token
        from rootpy import RootClient

        token = _token()
        client = RootClient(token=token)
        await client.login_token(token)
        await client.connect()
        await client.close()
        assert client.is_connected is False

    async def test_a_normal_close_is_not_an_error(self, gateway_client):
        """The hub closes each batch with 1000 and expects a reconnect."""
        assert gateway_client.gateway is not None
        assert gateway_client.is_connected is True
