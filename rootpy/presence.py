"""Presence: the rule, and the two ways to watch it.

Root computes what other people see for an account from **two independent
values**, and the lower one wins::

    visible = min(ceiling, device)

* the **ceiling** is ``UserGrpcService/SetMaxOnlineStatus`` -- the most you
  are willing to appear as, and what a client's online/idle/invisible menu
  actually sets;
* the **device** is ``UserGrpcService/SetDeviceOnlineStatus`` -- what one
  connection claims it is doing. A connection must announce it when it
  opens, and again after every reconnect.

Both calls return success independently, so setting the ceiling to Active and
never announcing a device leaves the account **offline to everyone** with
nothing to suggest anything went wrong. That is the trap this module exists
to close: :func:`effective_presence` is the rule written down, and
``client.set_presence`` sets both halves.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Iterable, Optional

log = logging.getLogger(__name__)

#: Poll cadence used when a watch asks for the polling route without saying
#: how often. One request covers every user being watched, so this is cheap.
DEFAULT_POLL = 5.0


def effective_presence(ceiling, device):
    """What others see, given a ceiling and a device status.

    ``min`` of the two, because Root's states are ordered by their protocol
    values -- DISCONNECTED(1) < INACTIVE(4) < ACTIVE(16) -- so the numeric
    minimum is the visible one. A zero means "not known", which is not the
    same as offline: an unknown ceiling makes the answer unknown, while an
    unannounced device makes the account invisible, since no connection has
    claimed to be doing anything.
    """
    from .enums import UserOnlineStatus

    ceiling = int(ceiling or 0)
    device = int(device or 0)
    if not ceiling:
        return UserOnlineStatus.UNSPECIFIED
    if not device:
        return UserOnlineStatus.DISCONNECTED
    return UserOnlineStatus.coerce(min(ceiling, device))


class PresenceWatch:
    """A running watch on other people's presence. Stop it with :meth:`stop`.

    Returned by ``client.watch_presence``; there is no reason to build one
    directly.
    """

    def __init__(
        self,
        client,
        on_change,
        *,
        users: Optional[Iterable[str]] = None,
        poll: Optional[float] = None,
    ) -> None:
        self.client = client
        self.on_change = on_change
        self.users = {u for u in (users or ()) if u}
        self.poll = None if poll is None else max(0.5, float(poll))
        #: Last status seen per user, so a poll only reports what moved.
        self.last: dict[str, int] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._listening = False

    # -- lifecycle ------------------------------------------------------ #

    def start(self) -> "PresenceWatch":
        if not self._listening:
            self.client.add_listener("packet_user_set_status", self._on_packet)
            self._listening = True
        if self.poll is not None and self._task is None:
            self._task = asyncio.create_task(self._poll(), name="presence-watch")
        return self

    async def stop(self) -> None:
        self._stop.set()
        if self._listening:
            try:
                self.client.remove_listener(
                    "packet_user_set_status", self._on_packet
                )
            except Exception:                                 # noqa: BLE001
                pass
            self._listening = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):        # noqa: BLE001
                pass

    async def __aenter__(self) -> "PresenceWatch":
        return self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- the two routes ------------------------------------------------- #

    async def _emit(self, user_id: str, status: int) -> None:
        from .enums import UserOnlineStatus

        if self.users and user_id not in self.users:
            return
        if self.last.get(user_id) == int(status):
            return
        self.last[user_id] = int(status)
        try:
            result = self.on_change(user_id, UserOnlineStatus.coerce(int(status)))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:                              # noqa: BLE001
            log.warning("presence watch: handler raised: %r", exc)

    async def _on_packet(self, packet) -> None:
        """USER_SET_STATUS (191): user_id at field 3, online_status at 4.

        Push only arrives for communities this connection has **attached**.
        A watch that never attaches is not broken, it is simply quiet -- which
        is why :meth:`_poll` exists.
        """
        fields = (getattr(packet, "data", None) or {}).get("fields") or {}
        user_id = fields.get("user_id")
        status = fields.get("online_status")
        if user_id and status is not None:
            await self._emit(str(user_id), int(status))

    async def _poll(self) -> None:
        """Ask for the truth on a timer, for the users we were given.

        ``get_profiles`` batches: 200 users is one request, not 200. It needs
        no attach and no socket, which makes it the route that works when the
        push route has nothing to push.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll)
                return
            except asyncio.TimeoutError:
                pass
            if not self.users:
                continue
            try:
                profiles = await self.client.get_profiles(sorted(self.users))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001
                log.debug("presence watch: poll failed: %s", exc)
                continue
            for user_id, profile in profiles.items():
                status = getattr(profile, "online_status", None)
                if status is not None:
                    await self._emit(str(user_id), int(status))
