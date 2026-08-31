"""Keeping a community attach alive.

``CommunityService.attach`` is a single request, and on its own that is a
trap: the subscription lives with the hub connection, not with the account,
and this hub is a resync-on-connect design that closes each batch normally
and expects a fresh socket for the next one. So an attach made once is gone
within a minute, silently -- the client stays logged in, the member list
quietly drops the account, and nothing raises.

Measured against a second account reading
``CommunityGetExtendedResponse.AttachedUserIds``:

* an attach made with **no gateway at all** was still there at +16s and gone
  by +31s;
* one whose socket was **killed under it** survived +36s and was gone by +48s.

:class:`AttachHold` is the piece that makes "stay attached" mean what it
says. It is per client; a fan-out over many accounts wants one of these each,
or its own host-wide scheduler (``devscripts/fanout.py`` has one, because at
a thousand accounts the socket handshakes have to be rationed across accounts
in a way a single client cannot see).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

log = logging.getLogger(__name__)

#: How often to look at ``client.is_connected``. It is a local flag read, so
#: an idle hold costs nothing at all -- no requests, no wakeups the event
#: loop would not otherwise have.
DEFAULT_POLL = 1.0


class AttachHold:
    """Hold a set of communities attached for one client.

    Typical use is the context manager on the community API rather than this
    class directly::

        async with client.community.held(community_id):
            ...                      # packets arrive; you are in the sidebar

    What it actually guarantees, and what it does not:

    * **Guaranteed:** while the hold is open, every time the socket comes
      back the attach is re-sent. That is the case that silently breaks
      without it.
    * **Not guaranteed:** that you are attached *right now*. A reconnect
      leaves a gap of up to ``poll`` seconds plus one round trip, and nothing
      can remove that gap -- the server genuinely does not know about you in
      between.

    ``refresh_interval`` covers the other decay: an attach with no socket at
    all. Leave it ``None`` (the default) and a hold keeps a socket open, which
    is both cheaper and less visible. Set it only if you are running more
    accounts than you can give sockets to -- each refresh re-broadcasts
    ``COMMUNITY_MEMBER_ATTACH`` to every member of the community, so it is
    noise other people can see.
    """

    def __init__(
        self,
        client,
        *,
        poll: float = DEFAULT_POLL,
        refresh_interval: Optional[float] = None,
        connect: bool = True,
    ) -> None:
        self.client = client
        self.poll = max(0.05, float(poll))
        self.refresh_interval = (
            None if refresh_interval is None else max(1.0, float(refresh_interval))
        )
        #: Open a socket when there isn't one. An attach without a socket
        #: decays on its own, so a hold that never connects is a hold in
        #: name only -- but a caller who is managing sockets elsewhere
        #: (a fan-out rationing handshakes) can turn this off.
        self.connect = bool(connect)

        self.communities: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._refreshed = 0.0

    # -- lifecycle ------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def add(self, community_id: str) -> None:
        """Attach to ``community_id`` and keep it attached."""
        community_id = self._normalize(community_id)
        self.communities.add(community_id)
        await self._ensure_connection()
        await self._attach(community_id)
        self._start()

    async def remove(self, community_id: str, *, detach: bool = True) -> None:
        """Stop holding ``community_id``, and by default detach from it."""
        community_id = self._normalize(community_id)
        self.communities.discard(community_id)
        if detach:
            try:
                await self.client.community_service.detach(community_id)
            except Exception as exc:                          # noqa: BLE001
                log.debug("attach hold: detach %s failed: %s", community_id, exc)
        if not self.communities:
            await self.stop()

    async def stop(self, *, detach: bool = False) -> None:
        """Stop watching. Held communities are left attached unless asked."""
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):        # noqa: BLE001
                pass
        if detach and self.communities:
            holding, self.communities = tuple(self.communities), set()
            try:
                await self.client.community_service.detach_many(holding)
            except Exception as exc:                          # noqa: BLE001
                log.debug("attach hold: detach on stop failed: %s", exc)

    # -- internals ------------------------------------------------------ #

    @staticmethod
    def _normalize(community_id: str) -> str:
        from .identifiers import normalize_root_guid

        return normalize_root_guid(community_id)

    def _start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._watch(), name="attach-hold")

    async def _ensure_connection(self) -> None:
        if not self.connect:
            return
        if not getattr(self.client, "is_connected", False):
            await self.client.connect()

    async def _attach(self, community_id: str) -> None:
        await self.client.community_service.attach(community_id)

    async def _attach_all(self) -> None:
        """Re-send every held attach. One request each; failures are logged.

        A community that fails must not take the others down with it -- the
        usual reason is that this account was removed from it, which is not a
        reason to stop holding the rest.
        """
        for community_id in tuple(self.communities):
            try:
                await self._attach(community_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001
                log.warning(
                    "attach hold: could not re-attach %s: %s", community_id, exc
                )
        self._refreshed = asyncio.get_running_loop().time()

    async def _watch(self) -> None:
        """Re-attach on the rising edge of ``is_connected``.

        ``is_connected`` is coarser than it looks: the hub's ordinary
        close-1000-and-resume cycle does not clear it, because the gateway
        task is still running. A ``False`` here means the gateway has actually
        stopped, so it is worth a reconnect rather than a wait.
        """
        was_connected = bool(getattr(self.client, "is_connected", False))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll)
                return
            except asyncio.TimeoutError:
                pass

            if not self.communities:
                continue

            connected = bool(getattr(self.client, "is_connected", False))
            try:
                if connected and not was_connected:
                    # A fresh socket is a fresh subscription: the attach did
                    # not survive, whatever the client's own state suggests.
                    await self._attach_all()
                elif not connected and self.connect:
                    await self._ensure_connection()
                    if getattr(self.client, "is_connected", False):
                        await self._attach_all()
                        connected = True
                elif self.refresh_interval is not None:
                    now = asyncio.get_running_loop().time()
                    if now - self._refreshed >= self.refresh_interval:
                        await self._attach_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001
                log.debug("attach hold: upkeep failed: %s", exc)

            was_connected = connected


class _HeldCommunities:
    """The object ``client.community.held(...)`` returns.

    Awaitable *and* an async context manager, so both of these work::

        await client.community.held(cid)          # hold until you release
        async with client.community.held(cid):    # hold for the block
            ...
    """

    def __init__(self, api, community_ids: Iterable[str]) -> None:
        self._api = api
        self._ids = tuple(community_ids)

    def __await__(self):
        return self._enter().__await__()

    async def _enter(self) -> "AttachHold":
        hold = self._api._attach_hold()
        for community_id in self._ids:
            await hold.add(community_id)
        return hold

    async def __aenter__(self) -> "AttachHold":
        return await self._enter()

    async def __aexit__(self, *exc) -> None:
        hold = self._api._attach_hold()
        for community_id in self._ids:
            await hold.remove(community_id)
