"""Run several accounts in one process.

One Python process costs ~30 MB before you do anything -- interpreter, imports,
the SDK itself. Running one process per account pays that every time:

    50 accounts, one process each   ->  ~1,490 MB
    50 accounts, one process        ->     ~30 MB

Everything expensive (module code, the generated registries, the event loop) is
shared, and each account adds well under a megabyte. So the answer to hosting
several accounts is a single process running several clients on one event loop.

    from rootpy import MultiClientHost

    async def setup_alice(client):
        @client.event
        async def on_message(event):
            ...                       # alice's own handlers

    host = MultiClientHost()
    host.add("alice", ALICE_TOKEN, setup=setup_alice)
    host.add("bob", BOB_TOKEN, setup=setup_bob)
    await host.run()

Each account is independent: its own client, its own handlers, its own
credentials, its own rate-limit budget, and its own failures. One account
erroring or being logged out doesn't disturb the others.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .client import RootClient
from .transport import GrpcWebTransport

log = logging.getLogger("rootpy.host")

SetupHook = Callable[[RootClient], Awaitable[None]]

#: What one IP gets out of Root in practice, on one shared HTTP/2
#: transport. These are ceilings, not targets: pushing concurrency past them
#: buys nothing and just moves the queue into the client, where it is harder
#: to see.
#:
#: The important shape is that they are *not* all the same. Sizing a fan-out
#: from the plain-RPC number makes attach look mysteriously slow, and sizing
#: socket work from it is off by nearly an order of magnitude, which is why
#: socket handshakes need their own, much narrower gate than plain RPCs.
MEASURED_CEILINGS = {
    #: Ordinary unary calls -- messages, profiles, settings. 103-140/s
    #: depending on the endpoint; treat 120 as the working number.
    "rpc": 120.0,
    #: Login is two RPCs (GetSelf and ListMine, gathered), so it caps at half
    #: the plain-RPC rate. ``RootClient(defer_hub=True)`` removes a third
    #: hop and is what takes it from ~39/s to ~61/s.
    "login": 60.0,
    #: CommunityAttach on its own, with nothing else in flight.
    "attach": 87.0,
    #: Websocket handshakes. The odd one out by a wide margin, and the reason
    #: socket work needs its own gate rather than sharing the RPC one.
    "connect": 17.0,
}

#: The most accounts :meth:`MultiClientHost.broadcast` runs at once when it
#: picks the number itself. Throughput keeps improving up to about here;
#: past it the gain is small while the load one IP puts on Root doubles, and
#: the plain-RPC ceiling is ~120/s either way. 64 is the point where more
#: concurrency stops being worth it.
DEFAULT_MAX_CONCURRENCY = 64

#: The floor, so a small host behaves exactly as it did before. Any host with
#: 8 accounts or fewer has more permits than accounts, so nothing changes.
DEFAULT_MIN_CONCURRENCY = 8


def resolve_concurrency(requested: Optional[int], account_count: int) -> int:
    """How many accounts to run at once, when the caller did not say.

    A flat default cannot be right for both sizes of host. 8 is sensible for
    five accounts and very wrong for a large one: with a 200 ms round trip it
    makes every command take tens of seconds. Scaling with the account count
    means the caller does not have to read a docstring to find that out.
    """
    if requested is not None:
        return max(1, int(requested))
    return min(
        DEFAULT_MAX_CONCURRENCY,
        max(DEFAULT_MIN_CONCURRENCY, int(account_count)),
    )


@dataclass
class HostedAccount:
    """One account inside the host."""

    name: str
    token: str
    setup: Optional[SetupHook] = None
    gateway: bool = True
    client: Optional[RootClient] = None
    task: Optional[asyncio.Task] = None
    error: Optional[BaseException] = None
    ready: bool = False
    options: dict = field(default_factory=dict)

    def __str__(self) -> str:
        state = "ready" if self.ready else ("failed" if self.error else "starting")
        return f"{self.name} ({state})"


@dataclass
class Outcome:
    """What one account's part of a :meth:`MultiClientHost.broadcast` did.

    "3 of 5 worked" is the normal case for a fan-out, not the exception --
    an account that cannot join answers with a refusal, one that is banned
    answers ``PERMISSION_DENIED``, and neither should cost you the other
    results. So a broadcast never raises; it returns one of these per account
    and you decide what matters.
    """

    name: str
    value: object = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __str__(self) -> str:
        if self.ok:
            return f"{self.name}: ok"
        return f"{self.name}: {type(self.error).__name__}: {self.error}"


class MultiClientHost:
    """Runs many accounts on one event loop, in one process.

    Parameters
    ----------
    stagger:
        Seconds between account logins. Starting twenty accounts at the same
        instant means twenty simultaneous handshakes; a small gap spreads that
        out and is gentler on the API.

        Prefer ``max_concurrent_logins`` for a large host. A stagger is a
        fixed *delay*, so it costs ``stagger x (N-1)`` before the last account
        has even started -- at the 0.5 default that is 9.5s for 20 accounts
        and 8 minutes for 1000, whether or not the API was ever the
        bottleneck. Setting it to 0 removes the delay but then releases every
        login at once, which is the other bad end. A concurrency ceiling
        bounds the burst without paying for accounts that are already done.
    max_concurrent_logins:
        How many accounts may be logging in at any one moment. ``None`` (the
        default) keeps the old behaviour: no ceiling, the stagger alone
        decides the shape.

        The gate is held only across ``login_token`` / ``setup`` / ``connect``
        and released as soon as an account is ready, so a slow account never
        blocks the queue behind it for longer than its own login.
    max_connections:
        Ceiling on the **HTTP** connection pool the accounts use. What it
        counts depends on ``shared_transport``, because there is only ever one
        pool per transport:

        * shared (the default) -- one pool for the whole host, however many
          accounts are on it.
        * ``shared_transport=False`` -- one pool per account, so the host's
          total is this times the number of accounts.

        It does **not** bound the host's sockets. Each account with
        ``gateway=True`` (the default for :meth:`add`) opens its own websocket
        through the ``websockets`` package, outside httpx entirely, where no
        pool limit can see it. A 50-account host at ``max_connections=20``
        holds 50 websockets plus up to 20 pooled HTTP connections.
        :meth:`from_tokens` defaults ``gateway=False`` for exactly this
        reason: a broadcast of API calls has no use for a websocket.

        20 is deliberately far below :class:`~rootpy.transport.GrpcWebTransport`'s
        own default of 200, because HTTP/2 multiplexes many concurrent
        requests down one connection -- 10 concurrent calls cost barely more
        than 1. Raise it for a large host doing genuinely heavy concurrent
        work.

        Accepted as ``max_connections_per_account`` too, which is what this
        was called when it only ever meant the isolated case. That spelling
        still works and still means the same number; it is just no longer an
        accurate description of the default.
    proxy:
        Outbound proxy for every account, e.g.
        ``"socks5://127.0.0.1:1080"`` for a wireproxy tunnel. Set here rather
        than per account: the proxy lives on the transport, and with a shared
        transport there is one of those for the whole host.

        Passing ``proxy=`` to :meth:`add` instead is refused, and that is
        deliberate. :class:`~rootpy.client.RootClient` ignores ``proxy`` when
        it is handed a transport, so a per-account proxy would tunnel the
        gateway websocket -- which reads ``client.proxy`` separately -- while
        the account's API calls went out direct. Half-proxied is worse than
        either. Set the proxy on the host instead.
    shared_transport:
        **On by default.** One transport means one TLS + HTTP/2 handshake for
        the whole host instead of one per account -- measured at ~831 ms per
        connection, which for a three-account host is the difference between
        ~1.1 s and ~3 s to first usefulness. HTTP/2 multiplexes the accounts'
        requests over it (10 concurrent calls cost barely more than 1).

        Cooldowns are keyed per account (see
        ``GrpcWebTransport._account_scope``), so one account's 429 does not
        pause the others even though they share the pool. Pass
        ``shared_transport=False`` if you want hard socket isolation anyway.
    """

    def __init__(
        self,
        *,
        stagger: float = 0.5,
        max_connections: Optional[int] = None,
        shared_transport: bool = True,
        proxy: Optional[str] = None,
        max_concurrent_logins: Optional[int] = None,
        max_connections_per_account: Optional[int] = None,
    ) -> None:
        if max_connections is not None and max_connections_per_account is not None:
            raise TypeError(
                "pass max_connections or max_connections_per_account, not both "
                "-- they are the same setting under two names"
            )
        limit = max_connections
        if limit is None:
            limit = max_connections_per_account
        if limit is None:
            limit = 20

        self.accounts: dict[str, HostedAccount] = {}
        self.stagger = max(0.0, float(stagger))
        self.max_connections = max(1, int(limit))
        self.proxy = proxy
        self.max_concurrent_logins = (
            None if max_concurrent_logins is None
            else max(1, int(max_concurrent_logins))
        )
        # Built here rather than in start(): asyncio.Semaphore has not bound
        # itself to a loop at construction since 3.10, and the host is
        # routinely constructed before the loop is running.
        self._login_gate = (
            asyncio.Semaphore(self.max_concurrent_logins)
            if self.max_concurrent_logins else None
        )
        self._shared = (
            self._new_transport() if shared_transport else None
        )
        self._stopping = False

    @property
    def max_connections_per_account(self) -> int:
        """The old name for :attr:`max_connections`, kept working.

        Only ever accurate with ``shared_transport=False``; see the class
        docstring. Reading it is harmless, so this is not deprecated -- it
        just does not describe the default any more.
        """
        return self.max_connections

    def _new_transport(self) -> GrpcWebTransport:
        """A pool at this host's ceiling -- shared or per-account, same shape.

        Both call sites used to build this differently: the shared one passed
        the *unclamped* argument and no keepalive ceiling, the per-account one
        passed the clamped attribute and half of it. Nothing intended that;
        one branch was simply written later than the other.
        """
        return GrpcWebTransport(
            max_connections=self.max_connections,
            max_keepalive_connections=max(1, self.max_connections // 2),
            proxy=self.proxy,
        )

    @classmethod
    def from_tokens(
        cls,
        tokens,
        *,
        gateway: bool = False,
        stagger: float = 0.0,
        **host_options,
    ) -> "MultiClientHost":
        """A host for a handful of bare tokens, named ``account1``... in order.

        The lightweight path: no setup hooks, no gateway unless asked for
        (a broadcast of API calls does not need a websocket), no stagger by
        default since two or three logins do not need spreading out. For
        anything long-lived or with handlers, construct the host yourself
        and use :meth:`add`.

            host = MultiClientHost.from_tokens([token_a, token_b])
            async with host:
                results = await host.broadcast(lambda c: c.invites.join(code))
        """
        if isinstance(tokens, (str, bytes)):
            raise TypeError(
                "from_tokens takes a sequence of tokens, not a single string "
                "-- wrap it in a list"
            )
        host = cls(stagger=stagger, **host_options)
        for index, token in enumerate(tokens, start=1):
            host.add(f"account{index}", token, gateway=gateway)
        return host

    async def __aenter__(self) -> "MultiClientHost":
        await self.start()
        ready = await self.wait_ready()
        failed = sorted(name for name, ok in ready.items() if not ok)
        if failed:
            log.warning("accounts not ready at __aenter__: %s", failed)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------ #
    def add(
        self,
        name: str,
        token: str,
        *,
        setup: Optional[SetupHook] = None,
        gateway: bool = True,
        **client_options,
    ) -> HostedAccount:
        """Register an account.

        name:    a label for logs -- anything you like
        token:   that account's token
        setup:   ``async def setup(client)`` where you register that account's
                 handlers. Runs after login, before the gateway starts.
        gateway: open a websocket for this account. Turn it off for accounts
                 that only make API calls -- it saves a connection and the
                 keepalive traffic.
        """
        if name in self.accounts:
            raise ValueError(f"account {name!r} is already registered")
        if "proxy" in client_options:
            # The host always hands RootClient a transport, and RootClient
            # ignores `proxy` when it gets one -- so a per-account proxy
            # would leave the API calls going out direct while the gateway,
            # which reads client.proxy separately, went through the tunnel.
            raise TypeError(
                "pass proxy= to MultiClientHost(...), not to add(): the proxy "
                "lives on the transport, which the host owns. Setting it per "
                "account would proxy that account's websocket but not its API "
                "calls."
            )
        if "transport" in client_options:
            raise TypeError(
                "the host supplies each account's transport -- use "
                "MultiClientHost(shared_transport=False) for isolated pools"
            )
        account = HostedAccount(
            name=name, token=token, setup=setup, gateway=gateway,
            options=client_options,
        )
        self.accounts[name] = account
        return account

    def remove(self, name: str) -> None:
        account = self.accounts.pop(name, None)
        if account and account.task:
            account.task.cancel()

    # ------------------------------------------------------------------ #
    async def _bring_up(self, account: HostedAccount, client: RootClient) -> None:
        """Log one account in and run its setup. The part worth rate-limiting."""
        await client.login_token()
        if account.setup is not None:
            await account.setup(client)
        if account.gateway:
            await client.connect()

    async def _run_account(self, account: HostedAccount) -> None:
        transport = self._shared or self._new_transport()
        # A client owns its transport only when the transport is its own.
        # With the shared pool, ``client.close()`` used to close it for every
        # other account -- the first account to shut down (or fail during
        # login, whose ``finally`` also closes) silently killed the rest.
        # Latent while shared_transport defaulted to off; fatal once it
        # became the default. The host closes the shared pool in stop().
        client = RootClient(
            token=account.token,
            transport=transport,
            owns_transport=transport is not self._shared,
            **account.options,
        )
        account.client = client
        try:
            # Held across the login only, then released -- an account that is
            # up costs nothing, so the ceiling applies to work in flight
            # rather than to how many accounts exist.
            if self._login_gate is not None:
                async with self._login_gate:
                    await self._bring_up(account, client)
            else:
                await self._bring_up(account, client)
            account.ready = True
            me = getattr(client.user, "username", account.name)
            log.info("account %s ready (%s)", account.name, me)

            # Stay alive until stopped; the client's own tasks do the work.
            while not self._stopping:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            account.error = exc
            log.error(
                "account %s failed: %s: %s",
                account.name, type(exc).__name__, exc,
            )
        finally:
            account.ready = False
            try:
                await client.close()
            except Exception:
                pass

    async def start(self) -> None:
        """Start every account, staggered. Returns once all are launched."""
        self._stopping = False
        for index, account in enumerate(self.accounts.values()):
            if account.task is not None and not account.task.done():
                continue
            account.task = asyncio.create_task(
                self._run_account(account), name=f"account:{account.name}"
            )
            if self.stagger and index < len(self.accounts) - 1:
                await asyncio.sleep(self.stagger)

    async def wait_ready(self, timeout: float = 30.0) -> dict[str, bool]:
        """Wait for accounts to finish logging in. Returns name -> ready."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            settled = all(
                account.ready or account.error is not None
                for account in self.accounts.values()
            )
            if settled:
                break
            await asyncio.sleep(0.2)
        return {name: acc.ready for name, acc in self.accounts.items()}

    async def run(self) -> None:
        """Start everything and run until cancelled or :meth:`stop` is called."""
        await self.start()
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shut every account down cleanly."""
        self._stopping = True
        tasks = [a.task for a in self.accounts.values() if a.task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._shared is not None:
            try:
                await self._shared.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    async def broadcast(
        self,
        action: Callable[[RootClient], Awaitable],
        *,
        only: Optional[list] = None,
        concurrency: Optional[int] = None,
        timeout: Optional[float] = 60.0,
    ) -> dict[str, Outcome]:
        """Run one action as every account, concurrently. Never raises.

            results = await host.broadcast(lambda c: c.invites.join(code))
            for outcome in results.values():
                print(outcome)      # "alice: ok" / "bob: GrpcUnauthenticated: ..."

        ``action`` is called once per ready account with that account's
        client and may return anything; the return value lands on the
        outcome's ``value``. Concurrent across accounts (HTTP/2 multiplexes,
        so N accounts cost barely more wall time than one), sequential within
        each -- an action that makes several calls behaves normally.

        Per-account failure is a *result*, not an exception: partial success
        is the normal case for a fan-out, and one account's refusal must not
        cost you the others' outcomes. An account that is not ready gets an
        outcome saying so rather than being silently skipped.

        only:        run as just these account names.
        concurrency: how many accounts act at once. ``None`` (the default)
                     scales with the number of accounts, capped at
                     :data:`DEFAULT_MAX_CONCURRENCY`. A flat 8 suits a
                     handful of accounts and is badly wrong for a lot of
                     them, so do not hard-code one. Pass a number to
                     override it.
                     See :data:`MEASURED_CEILINGS` for what Root will take.
        timeout:     per-account; a slow account becomes a TimeoutError
                     outcome instead of stalling the whole broadcast.
                     ``None`` disables it.
        """
        names = list(only) if only is not None else list(self.accounts)
        limiter = asyncio.Semaphore(
            resolve_concurrency(concurrency, len(names))
        )
        results: dict[str, Outcome] = {}

        async def run_one(name: str) -> None:
            account = self.accounts.get(name)
            if account is None:
                results[name] = Outcome(
                    name, error=KeyError(f"no account named {name!r}")
                )
                return
            if not account.ready or account.client is None:
                reason = account.error or RuntimeError(
                    "account is not ready -- did start()/wait_ready() run?"
                )
                results[name] = Outcome(name, error=reason)
                return
            async with limiter:
                try:
                    coro = action(account.client)
                    if timeout is not None:
                        coro = asyncio.wait_for(coro, timeout)
                    results[name] = Outcome(name, value=await coro)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 -- the point
                    results[name] = Outcome(name, error=exc)

        await asyncio.gather(*(run_one(name) for name in names))
        return {name: results[name] for name in names}

    async def join(self, code: str, **broadcast_options) -> dict[str, Outcome]:
        """Every account joins a community by invite code.

        The most common broadcast, named.

        An account that cannot newly join comes back as a *failed* outcome,
        which callers usually want to treat as success -- ``outcome.ok``
        deliberately does not, so that the distinction stays visible.

        It is not what you would guess: Root answers
        ``UNAUTHENTICATED (16)`` for an account that is **already a member**,
        and the same for the community's own **owner**. Not ``ALREADY_EXISTS``,
        which is what you would expect. A
        genuinely bad code answers ``NOT_FOUND (5)``, so "already in" and "no
        such invite" *are* distinguishable -- just not by the code you would
        expect. Match on the exception type rather than assuming.
        """
        return await self.broadcast(
            lambda client: client.invites.join(code), **broadcast_options
        )

    # ------------------------------------------------------------------ #
    def client(self, name: str) -> Optional[RootClient]:
        """The live client for an account, once it's ready."""
        account = self.accounts.get(name)
        return account.client if account else None

    def status(self) -> dict:
        """A snapshot of every account -- handy for a health endpoint."""
        return {
            name: {
                "ready": acc.ready,
                "gateway": acc.gateway,
                "error": (
                    f"{type(acc.error).__name__}: {acc.error}"
                    if acc.error else None
                ),
                "username": getattr(getattr(acc.client, "user", None), "username", None),
            }
            for name, acc in self.accounts.items()
        }
