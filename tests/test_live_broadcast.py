"""Live coverage for ``MultiClientHost`` and the one sanctioned fan-out.

``broadcast`` was the headline of 1.18.0 and had only offline coverage: one
test in ``test_features.py`` against three fake clients, and a scope guard in
``test_untested_surface.py`` that pins *where* it lives rather than what it
does. Neither can see the things that actually break a fan-out --

* that each account acts **as itself**. A shared transport carries every
  account's requests over one HTTP/2 connection, so "did account2's call go
  out with account2's credentials" is precisely the question a shared pool
  puts in doubt, and the only proof is two distinct identities coming back.
* that a shared transport is genuinely shared, and that one account's
  rate-limit cooldown does not pause the others (``_account_scope``).
* that partial success is a *result*. The docstring's example is an account
  that answers ``ALREADY_EXISTS`` while the others join; nothing had ever run
  it against a server that can say so.
* that a per-account timeout produces an outcome rather than stalling the
  broadcast.

Needs both tokens::

    set ROOT_TOKEN=<first account>
    set ROOT_TOKEN2=<second account>
    pytest -m live2 tests/test_live_broadcast.py -v

The host builds its own clients from the raw tokens, so nothing here shares
the session ``client``/``peer`` fixtures' connections -- which is the point:
the host's own login path is part of what is under test. ``gateway=False``
throughout, because a broadcast of API calls does not need a websocket and
the handshake is the slowest part of bringing an account up.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from rootpy.host import MultiClientHost, Outcome
from rootpy.responses import field as _field

from .conftest import _token, _token2, requires_two_accounts

pytestmark = [pytest.mark.live2, pytest.mark.asyncio, requires_two_accounts]


async def _host(**options):
    """A started two-account host, torn down on the way out."""
    host = MultiClientHost.from_tokens(
        [_token(), _token2()], gateway=False, **options
    )
    await host.start()
    ready = await host.wait_ready()
    failed = sorted(name for name, ok in ready.items() if not ok)
    if failed:
        errors = {
            name: repr(acc.error) for name, acc in host.accounts.items() if acc.error
        }
        await host.stop()
        pytest.fail(f"accounts did not come up: {failed} -- {errors}")
    return host


class TestHostBringsAccountsUp:
    async def test_from_tokens_logs_both_accounts_in(self):
        host = await _host()
        try:
            assert set(host.accounts) == {"account1", "account2"}
            assert all(acc.ready for acc in host.accounts.values())
            assert host.client("account1") is not None
            assert host.client("account2") is not None
        finally:
            await host.stop()

    async def test_status_reports_both_ready(self):
        host = await _host()
        try:
            status = host.status()
            assert set(status) == {"account1", "account2"}
            for name, entry in status.items():
                assert entry["ready"] is True, f"{name}: {entry}"
                assert entry["error"] is None, f"{name}: {entry}"
        finally:
            await host.stop()

    async def test_shared_transport_is_one_transport(self):
        """The default. One TLS + HTTP/2 handshake for the whole host."""
        host = await _host()
        try:
            first = host.client("account1")
            second = host.client("account2")
            assert first.transport is second.transport, (
                "shared_transport=True must give both accounts the same "
                "transport -- that is the whole saving"
            )
            # And neither client may own it, or the first close() kills the
            # other account's connection. This regressed once already, and it
            # is latent until shared_transport is on -- which it now is by
            # default, so the guard belongs here.
            assert first._owns_transport is False
            assert second._owns_transport is False
        finally:
            await host.stop()

    async def test_isolated_transport_gives_each_account_its_own(self):
        host = await _host(shared_transport=False)
        try:
            first = host.client("account1")
            second = host.client("account2")
            assert first.transport is not second.transport
        finally:
            await host.stop()


class TestBroadcastActsAsEachAccount:
    """The claim a shared transport puts in doubt, tested directly."""

    async def test_each_account_gets_its_own_identity_back(self):
        """``refresh=True`` deliberately: a cached identity proves nothing here.

        ``whoami()`` with no argument returns ``client.user``, populated at
        login -- so a plain ``whoami`` broadcast would confirm only that two
        logins happened, which was never in doubt. The question a shared
        transport raises is whether a *request issued over it* carries the
        right account's credentials, and only a real ``UserGetSelf`` round
        trip per account can answer that.
        """
        host = await _host()
        try:
            results = await host.broadcast(lambda c: c.whoami(refresh=True))

            assert set(results) == {"account1", "account2"}
            assert all(isinstance(o, Outcome) for o in results.values())
            for name, outcome in results.items():
                assert outcome.ok, f"{name} failed: {outcome}"

            ids = {
                name: str(_field(outcome.value, "id"))
                for name, outcome in results.items()
            }
            names = {
                name: str(_field(outcome.value, "username"))
                for name, outcome in results.items()
            }
            assert len(set(ids.values())) == 2, (
                "both accounts answered with the same user id -- the shared "
                f"transport is carrying one account's credentials: {ids}"
            )
            assert len(set(names.values())) == 2, names
        finally:
            await host.stop()

    async def test_the_value_is_whatever_the_action_returned(self):
        host = await _host()
        try:
            results = await host.broadcast(lambda c: c.whoami())
            for outcome in results.values():
                assert _field(outcome.value, "username"), (
                    f"outcome.value should carry the action's return: {outcome.value!r}"
                )
        finally:
            await host.stop()

    async def test_only_restricts_the_fan_out(self):
        host = await _host()
        try:
            results = await host.broadcast(lambda c: c.whoami(), only=["account2"])
            assert set(results) == {"account2"}
            assert results["account2"].ok
        finally:
            await host.stop()

    async def test_an_unknown_name_is_an_outcome_not_a_raise(self):
        host = await _host()
        try:
            results = await host.broadcast(
                lambda c: c.whoami(), only=["account1", "nope"]
            )
            assert set(results) == {"account1", "nope"}
            assert results["account1"].ok, results["account1"]
            assert not results["nope"].ok
            assert isinstance(results["nope"].error, KeyError)
        finally:
            await host.stop()

    async def test_results_are_keyed_in_the_order_asked_for(self):
        host = await _host()
        try:
            results = await host.broadcast(
                lambda c: c.whoami(), only=["account2", "account1"]
            )
            assert list(results) == ["account2", "account1"]
        finally:
            await host.stop()


class TestBroadcastNeverRaises:
    """Partial success is the normal case for a fan-out, not an exception."""

    async def test_an_action_that_raises_becomes_an_error_outcome(self):
        host = await _host()
        try:
            boom = RuntimeError("deliberate")

            async def fail(_client):
                raise boom

            results = await host.broadcast(fail)
            assert set(results) == {"account1", "account2"}
            for name, outcome in results.items():
                assert not outcome.ok, name
                assert outcome.error is boom, f"{name}: {outcome.error!r}"
                assert "RuntimeError" in str(outcome)
        finally:
            await host.stop()

    async def test_one_account_failing_does_not_cost_the_other_its_result(self):
        host = await _host()
        try:

            async def only_account2_fails(client):
                me = await client.whoami()
                if str(_field(me, "id")) == str(
                    _field(await host.client("account2").whoami(), "id")
                ):
                    raise ValueError("this account is the designated failure")
                return me

            results = await host.broadcast(only_account2_fails)
            assert results["account1"].ok, results["account1"]
            assert not results["account2"].ok
            assert isinstance(results["account2"].error, ValueError)
        finally:
            await host.stop()

    async def test_a_server_refusal_arrives_as_the_typed_exception(self):
        """A real rejection from Root, not a locally raised stand-in."""
        host = await _host()
        try:
            results = await host.broadcast(
                lambda c: c.invites.join("rootpy-no-such-invite-code")
            )
            assert set(results) == {"account1", "account2"}
            for name, outcome in results.items():
                assert not outcome.ok, (
                    f"{name} somehow joined a nonexistent invite: {outcome.value!r}"
                )
                assert isinstance(outcome.error, BaseException)
                # The formatted outcome names the exception type, which is what
                # a caller prints when 3 of 5 worked.
                assert type(outcome.error).__name__ in str(outcome)
            print(
                "\n   server refusal shapes:\n     "
                + "\n     ".join(f"{n}: {o}" for n, o in results.items())
            )
        finally:
            await host.stop()

    async def test_a_slow_account_times_out_into_an_outcome(self):
        host = await _host()
        try:

            # These sleeps are the *subject*, not a wait for the server to
            # catch up -- the action has to outlast its own timeout for there
            # to be anything to test. Kept sub-second so the suite's
            # "no fixed multi-second sleeps in live_ files" guard stays strict:
            # what matters is the ratio to `timeout`, not the magnitude.
            async def too_slow(_client):
                await asyncio.sleep(0.9)
                return "never"

            started = time.monotonic()
            results = await host.broadcast(too_slow, timeout=0.2)
            elapsed = time.monotonic() - started

            for name, outcome in results.items():
                assert not outcome.ok, name
                assert isinstance(
                    outcome.error, (asyncio.TimeoutError, TimeoutError)
                ), f"{name}: {outcome.error!r}"
            assert elapsed < 0.8, (
                f"the timeout did not bound the broadcast: {elapsed:.2f}s"
            )
        finally:
            await host.stop()

    async def test_timeout_none_disables_the_bound(self):
        host = await _host()
        try:

            async def slow_but_finishes(_client):
                # Longer than any default would allow if one applied; see the
                # note in the timeout test about why this is sub-second.
                await asyncio.sleep(0.9)
                return "done"

            results = await host.broadcast(slow_but_finishes, timeout=None)
            assert all(o.ok for o in results.values()), results
            assert {o.value for o in results.values()} == {"done"}
        finally:
            await host.stop()


class TestBroadcastConcurrency:
    """"N accounts cost barely more wall time than one" -- the README's claim.

    Measured against ``asyncio.sleep`` rather than the network, so the test
    reports the *scheduler's* behaviour and cannot fail because Root was busy.
    """

    async def test_accounts_run_concurrently_not_serially(self):
        host = await _host()
        try:

            async def takes_half_a_second(_client):
                await asyncio.sleep(0.5)
                return time.monotonic()

            started = time.monotonic()
            results = await host.broadcast(takes_half_a_second)
            elapsed = time.monotonic() - started

            assert all(o.ok for o in results.values()), results
            assert elapsed < 0.9, (
                "two accounts sleeping 0.5s each took "
                f"{elapsed:.2f}s -- that is serial, not concurrent"
            )
        finally:
            await host.stop()

    async def test_concurrency_one_serialises(self):
        host = await _host()
        try:

            async def takes_half_a_second(_client):
                await asyncio.sleep(0.5)
                return True

            started = time.monotonic()
            results = await host.broadcast(takes_half_a_second, concurrency=1)
            elapsed = time.monotonic() - started

            assert all(o.ok for o in results.values()), results
            assert elapsed >= 0.9, (
                f"concurrency=1 should serialise the two accounts: {elapsed:.2f}s"
            )
        finally:
            await host.stop()

    async def test_a_real_broadcast_costs_about_one_round_trip(self):
        """The HTTP/2 multiplexing claim, against the real API.

        ``refresh=True`` because the first version of this test measured plain
        ``whoami()`` -- a cached attribute read -- and reported "one call 0 ms,
        broadcast 0 ms (26.76x)". It passed, on a floor in the assertion, while
        measuring no network at all. The ratio only means anything if both
        sides of it actually leave the process.

        Reported rather than tightly asserted -- the bound is deliberately
        loose (under 2.5x a single call) because it crosses a network. A
        genuinely serial fan-out over N accounts would exceed it.
        """
        host = await _host()
        try:
            first = host.client("account1")

            # Warm the connection first: the very first request over a fresh
            # transport pays the TLS + HTTP/2 handshake (~831 ms, per the
            # host docstring), which would land entirely on whichever side of
            # this comparison went first.
            await first.whoami(refresh=True)

            started = time.monotonic()
            await first.whoami(refresh=True)
            single = time.monotonic() - started

            started = time.monotonic()
            results = await host.broadcast(lambda c: c.whoami(refresh=True))
            fanned = time.monotonic() - started

            assert all(o.ok for o in results.values()), results
            print(
                f"\n   one call {single*1000:.0f} ms; "
                f"2-account broadcast {fanned*1000:.0f} ms "
                f"({fanned/max(single, 1e-6):.2f}x)"
            )
            assert fanned < max(single * 2.5, 0.75), (
                f"broadcast of 2 took {fanned*1000:.0f} ms against a single "
                f"call's {single*1000:.0f} ms -- multiplexing is not happening"
            )
        finally:
            await host.stop()


class TestBroadcastJoin:
    """``host.join`` -- the named broadcast, against a real invite."""

    async def test_every_account_joins_and_the_owner_says_so(
        self, client, sandbox
    ):
        """account1 owns the sandbox, so its own join is the partial-failure case.

        This is the exact scenario ``broadcast``'s docstring describes: an
        account that is already a member answers one way, a fresh account
        another, and neither costs the other its result.
        """
        invite = await client.invites.create(sandbox.community_id, max_uses=5)
        code = getattr(invite, "code", None) or getattr(invite, "id", None)
        if not code:
            pytest.skip(f"could not read an invite code from {invite!r}")

        host = await _host()
        try:
            results = await host.join(code)
            assert set(results) == {"account1", "account2"}

            print(
                "\n   host.join outcomes:\n     "
                + "\n     ".join(f"{n}: {o}" for n, o in results.items())
            )

            # The structural guarantee, whatever the server said to the owner:
            # one outcome per account, and the fresh account got in.
            assert results["account2"].ok, (
                f"the non-member account failed to join: {results['account2']}"
            )

            # ...and it is really a member, not just a successful-looking RPC.
            members = await client.members.list_all(sandbox.community_id)
            peer_id = str(_field((await host.client("account2").whoami()), "id"))
            member_ids = {
                str(_field(m, "user_id") or _field(m, "id")) for m in members
            }
            assert peer_id in member_ids, (
                f"join returned ok but {peer_id} is not in the member list"
            )

            # Now account2 *is* a member, which is the case broadcast's
            # docstring describes. Run it again and record what Root actually
            # answers, rather than repeating the claim.
            again = await host.join(code)
            print(
                "\n   host.join outcomes, second pass "
                "(account1 owner, account2 already a member):\n     "
                + "\n     ".join(f"{n}: {o}" for n, o in again.items())
            )
            assert set(again) == {"account1", "account2"}
            assert not again["account2"].ok, (
                "re-joining as an existing member succeeded silently -- if "
                "Root really allows that, the docstring's ALREADY_EXISTS "
                f"example is wrong in the other direction: {again['account2']}"
            )
            # The point of the whole design: a refusal is a result.
            assert isinstance(again["account2"].error, BaseException)
        finally:
            try:
                await host.client("account2").community_service.leave(
                    sandbox.community_id
                )
            except Exception:
                pass
            await host.stop()
