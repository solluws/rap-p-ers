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


class MultiClientHost:
    """Runs many accounts on one event loop, in one process.

    Parameters
    ----------
    stagger:
        Seconds between account logins. Starting twenty accounts at the same
        instant means twenty simultaneous handshakes; a small gap spreads that
        out and is gentler on the API.
    max_connections_per_account:
        Connection-pool ceiling for each account. The default of 200 is sized
        for a single busy client; with many accounts a smaller number keeps the
        total socket count sane.
    shared_transport:
        Off by default, deliberately. Sharing one transport means sharing one
        connection pool *and* one rate-limit cooldown table -- so a 429 caused
        by one account would pause every other account's calls to that
        endpoint. Separate transports keep each account's limits its own.
    """

    def __init__(
        self,
        *,
        stagger: float = 0.5,
        max_connections_per_account: int = 20,
        shared_transport: bool = False,
    ) -> None:
        self.accounts: dict[str, HostedAccount] = {}
        self.stagger = max(0.0, float(stagger))
        self.max_connections_per_account = max(1, int(max_connections_per_account))
        self._shared = (
            GrpcWebTransport(max_connections=max_connections_per_account)
            if shared_transport
            else None
        )
        self._stopping = False

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
    async def _run_account(self, account: HostedAccount) -> None:
        transport = self._shared or GrpcWebTransport(
            max_connections=self.max_connections_per_account,
            max_keepalive_connections=max(
                1, self.max_connections_per_account // 2
            ),
        )
        client = RootClient(
            token=account.token, transport=transport, **account.options
        )
        account.client = client
        try:
            await client.login_token()
            if account.setup is not None:
                await account.setup(client)
            if account.gateway:
                await client.connect()
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
