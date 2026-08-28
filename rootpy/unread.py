"""Read everything the moment it goes unread, the way a person at the client does.

The model is someone sitting in front of Root: the sidebar shows which channels
have gone bold, they click each one, it clears, and they go back to what they
were doing. They do not walk every server every thirty seconds asking "anything
new?" -- and neither does this.

**Membership does not subscribe you.** Until a community is *attached* the hub
sends nothing for it: no message, no channel edit, nothing -- while DMs,
mentions and status changes arrive normally on the same socket. A bot can look
perfectly connected and never see a channel post. One call fixes it::

    await client.community.attach(community_id)

That is ``root.CommunityGrpcService/Attach``, which the desktop client makes
from its full-load path (``Community.attachAsync``) and undoes in
``FullyUnload``. Measured, same process, same channel, with the post before and
after in one run: **before attach, nothing at all; 0.4 s after attach, the
message arrives.** This reader attaches to every community you are in, so you
do not have to.

Attaching has one visible consequence: the server broadcasts
``COMMUNITY_MEMBER_ATTACH`` (5502), and clients show attached members as
present in the community. Pass ``attach=False`` to stay invisible and fall back
to polling, or ``detach_on_stop=True`` (the default) to leave as you found it.

What arrives, all measured with a positive control in the same process so a
silent socket could not be mistaken for a broken listener:

* **DMs and mentions** -- ``NOTIFICATION`` (case 180), the whole originating
  message embedded, at **+0.4 s**. These need no attach; they are user-scoped.
* **Channel messages, once attached** -- ``MESSAGE`` (case 170) carrying the
  channel as ``container_id`` and the community as ``community_id``, at
  **+0.26 s**. Without the attach these never arrive, however long you wait.

So there is no sweep in the steady state. The reader does one reconciliation
pass at startup to clear whatever went unread while it was not running, then
sits on the socket. Periodic sweeping is off by default (``interval=0``) and
available if you want a safety net; when it does sweep, one
``CommunityGetExtended`` returns the unread state of every channel in a
community at once, so the cost is per community, never per channel.

The unread test itself is Root's, taken from the client's ``Channel.HasActivity``:

    LastActivityAt > UserLastViewedAt, both non-null, and not a voice channel

The "both non-null" half matters and is easy to get wrong: a channel you have
never opened has no ``UserLastViewedAt`` and is **not** unread. Treating null as
unread makes every channel in every new community look unread forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger("rootpy.unread")

#: Voice channels are excluded by Root's own predicate -- they carry activity
#: that is not "unread messages".
_VOICE = 4


@dataclass
class UnreadChannel:
    """A channel that has gone bold, and by how much."""

    channel_id: str
    channel_name: str
    community_id: str
    community_name: str
    last_activity_at: object = None
    user_last_viewed_at: object = None

    def __str__(self) -> str:
        return f"#{self.channel_name} in {self.community_name}"


@dataclass
class ReaderStats:
    """What the reader has done, for judging cost rather than guessing at it."""

    sweeps: int = 0
    requests: int = 0
    channels_opened: int = 0
    messages_read: int = 0
    pushed_messages: int = 0
    attached: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def requests_per_minute(self) -> float:
        minutes = self.uptime / 60.0
        return self.requests / minutes if minutes > 0 else 0.0

    def __str__(self) -> str:
        return (
            f"{self.sweeps} sweep(s), {self.requests} request(s) "
            f"({self.requests_per_minute:.1f}/min), "
            f"{self.attached} community(ies) attached, "
            f"{self.channels_opened} channel(s) opened, "
            f"{self.messages_read} message(s) read, "
            f"{self.pushed_messages} pushed"
        )


def is_unread(channel) -> bool:
    """Root's own unread test, from the client's ``Channel.HasActivity``.

    Both timestamps must be present. A channel that has never been opened has
    no ``user_last_viewed_at``, and Root does not call that unread -- which is
    the difference between "three channels are bold" and "every channel in
    every community you have ever joined is bold".
    """
    last_activity = getattr(channel, "last_activity_at", None)
    last_viewed = getattr(channel, "user_last_viewed_at", None)
    if last_activity is None or last_viewed is None:
        return False
    if getattr(channel, "channel_type", None) == _VOICE:
        return False
    try:
        return last_activity > last_viewed
    except TypeError:            # unexpected shapes: not unread, not a crash
        return False


class UnreadReader:
    """Watch every community and read whatever goes unread, the instant it does.

        reader = UnreadReader(client, on_message=handle)
        async with reader:
            await asyncio.Event().wait()

    Starting it attaches to every community the account is in, so channel
    messages start arriving on the socket (see the module docstring -- without
    the attach they never do), then does one reconciliation sweep to clear the
    backlog from before it was running. After that it makes **no requests at
    all** until something happens.

    You can still drive a pass by hand, which is also how it is tested::

        found = await reader.sweep()

    ``interval`` is an optional safety-net poll for channel unread, **off by
    default** -- pushes cover it, and a sweep costs one request per community.
    Set ``mark_read=False`` to observe without clearing anything; note the same
    channel then stays unread and is reported again by any later sweep.
    ``attach=False`` keeps the account invisible in member lists at the cost of
    going back to polling, and needs an ``interval`` to see anything.
    """

    def __init__(
        self,
        client,
        *,
        interval: float = 0.0,
        idle_interval: Optional[float] = None,
        idle_after: int = 10,
        community_refresh: float = 60.0,
        on_message: Optional[Callable[[object], Awaitable[None]]] = None,
        on_unread: Optional[Callable[[UnreadChannel], Awaitable[None]]] = None,
        mark_read: bool = True,
        attach: bool = True,
        detach_on_stop: bool = True,
        concurrency: int = 8,
        history_limit: int = 10,
    ) -> None:
        self.client = client
        #: Safety-net poll period for channel unread. ``0`` -- the default --
        #: means no periodic sweep at all: attaching makes channel messages
        #: push, so there is nothing left for a timer to discover.
        self.interval = 0.0 if float(interval) <= 0 else max(0.25, float(interval))
        #: Optional backoff: poll this slowly once nothing has been unread for
        #: ``idle_after`` sweeps. **Off by default**, because backing off makes
        #: the *first* message after a quiet spell as slow as the backoff --
        #: which is the opposite of the point. Turn it on when a steady
        #: ``len(communities)`` requests per tick costs more than latency is
        #: worth; any find snaps straight back to ``interval``.
        self.idle_interval = (
            self.interval if idle_interval is None
            else max(self.interval, float(idle_interval))
        )
        self.idle_after = max(1, int(idle_after))
        #: How often to re-list communities. They change rarely, and refusing
        #: to re-list every tick removes one request per tick.
        self.community_refresh = max(0.0, float(community_refresh))
        self.on_message = on_message
        self.on_unread = on_unread
        self.mark_read = bool(mark_read)
        #: Subscribe to every community's live packets. Without this the hub
        #: sends no channel traffic at all -- see the module docstring.
        self.attach = bool(attach)
        #: Detach again on stop, so the account stops showing as present.
        self.detach_on_stop = bool(detach_on_stop)
        self.concurrency = max(1, int(concurrency))
        # MessageList's Limit must be 10-50 inclusive; 10 is the floor and
        # enough to catch up a channel that just went bold.
        self.history_limit = min(50, max(10, int(history_limit)))
        self.stats = ReaderStats()

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        #: channel id -> the last_activity_at we have already handled, so a
        #: channel is opened once per burst rather than once per sweep.
        self._handled: dict = {}
        self._listening = False
        self._quiet_sweeps = 0
        self._community_cache: tuple = ()
        self._community_cache_at: float = 0.0
        #: community id -> name, for the ids that arrive on pushed packets.
        self._names: dict = {}
        #: community ids currently attached, so stop() can undo exactly them.
        self._attached: set = set()
        self._watchdog: Optional[asyncio.Task] = None

    @property
    def current_interval(self) -> float:
        """The pace right now -- fast while busy, slow once quiet."""
        if self._quiet_sweeps >= self.idle_after:
            return self.idle_interval
        return self.interval

    # -- lifecycle -------------------------------------------------------
    async def __aenter__(self) -> "UnreadReader":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        """Subscribe, clear the backlog, then wait on the socket.

        Order matters. Listening comes first so nothing that lands mid-startup
        is missed, then the attach turns channel traffic on, then one sweep
        picks up whatever went unread while this was not running. Only after
        that is the reader idle -- and idle means zero requests.
        """
        self._listen()
        if self.attach:
            await self._attach_all()
        try:
            await self.sweep()
        except Exception as exc:
            log.warning("unread: startup reconciliation failed: %s", exc)
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="rootpy-unread")
        if self.attach and self._watchdog is None:
            self._watchdog = asyncio.create_task(
                self._watch_connection(), name="rootpy-unread-reattach"
            )

    async def stop(self) -> None:
        self._stop.set()
        for attribute in ("_task", "_watchdog"):
            task = getattr(self, attribute)
            setattr(self, attribute, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._listening:
            try:
                self.client.remove_listener("message", self._on_pushed)
            except Exception:
                pass
            self._listening = False
        if self.detach_on_stop and self._attached:
            attached, self._attached = tuple(self._attached), set()
            try:
                await self.client.community.detach_many(attached)
            except Exception as exc:
                log.debug("unread: detach on stop failed: %s", exc)

    # -- the subscription ------------------------------------------------
    async def _attach_all(self) -> None:
        """Attach to every community, so its channels actually push.

        One request per community, once per connection -- not per tick. A
        community that fails to attach is logged and skipped rather than
        taking the whole reader down with it; the sweep still covers it if an
        ``interval`` is set.
        """
        communities = await self._communities()
        limiter = asyncio.Semaphore(self.concurrency)

        async def attach(community):
            async with limiter:
                self.stats.requests += 1
                await self.client.community.attach(community.id)
                return community.id

        results = await asyncio.gather(
            *(attach(c) for c in communities), return_exceptions=True
        )
        for community, result in zip(communities, results):
            if isinstance(result, BaseException):
                log.warning(
                    "unread: could not attach to %s (%s): %s",
                    getattr(community, "name", "?"), community.id, result,
                )
                continue
            self._attached.add(result)
            self.stats.attached += 1

    async def _watch_connection(self) -> None:
        """Re-attach after a reconnect.

        The subscription lives with the hub connection, so a dropped socket
        loses it silently -- the reader would stay up, stay quiet, and stop
        seeing anything. Watching ``is_connected`` costs no requests, so this
        polls a local flag and only spends anything on the rising edge.
        """
        was_connected = bool(getattr(self.client, "is_connected", False))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                pass
            connected = bool(getattr(self.client, "is_connected", False))
            if connected and not was_connected:
                log.info("unread: gateway back; re-attaching")
                self._attached.clear()
                self._community_cache_at = 0.0
                try:
                    await self._attach_all()
                    await self.sweep()
                except Exception as exc:
                    log.warning("unread: re-attach failed: %s", exc)
            was_connected = connected

    # -- the pushed half, which is now nearly all of it -------------------
    def _listen(self) -> None:
        """Everything arrives here: DMs, mentions, and attached channels."""
        if self._listening:
            return
        try:
            self.client.add_listener("message", self._on_pushed)
            self._listening = True
        except Exception as exc:                       # no gateway yet
            log.debug("unread: could not attach message listener: %s", exc)

    async def _on_pushed(self, event) -> None:
        """A message landed on the socket: hand it over and clear the bold.

        This is the whole point of attaching. A channel post arrives here
        0.26 s after it is sent, carrying the channel as ``container_id``, so
        the channel is read and cleared without anything having to notice it
        went bold first.
        """
        message = getattr(event, "message", None)
        if message is None:
            return
        self.stats.pushed_messages += 1
        container = getattr(message, "container_id", None)
        community_id = getattr(message, "community_id", None)

        # Only channel posts carry a community; a DM does not, and a DM is not
        # a channel going unread.
        if container and community_id:
            self.stats.channels_opened += 1
            if self.on_unread is not None:
                await self.on_unread(self._describe(container, community_id))

        if self.on_message is not None:
            await self.on_message(message)
        if self.mark_read and container:
            await self._mark(container, community_id)

    def _describe(self, channel_id, community_id) -> UnreadChannel:
        """Name a pushed channel from cache -- without spending a request.

        A packet carries ids, not names. Anything already listed or fetched is
        free to look up; anything else keeps the id as its name rather than
        making the reader pay a round trip to pretty-print a log line.
        """
        community_name = self._names.get(community_id) or ""
        if not community_name:
            cached = getattr(self.client, "communities", {}) or {}
            community = cached.get(community_id)
            community_name = getattr(community, "name", "") or ""
            if community_name:
                self._names[community_id] = community_name
        channel_name = ""
        extended = getattr(self.client, "_community_extended", {}) or {}
        detail = extended.get(community_id)
        for channel in getattr(detail, "text_channels", ()) or ():
            if getattr(channel, "id", None) == channel_id:
                channel_name = getattr(channel, "name", "") or ""
                break
        return UnreadChannel(
            channel_id=channel_id,
            channel_name=channel_name or channel_id,
            community_id=community_id,
            community_name=community_name or community_id,
        )

    # -- the polled half -------------------------------------------------
    async def sweep(self) -> list:
        """One pass: find every unread channel, read it, clear it.

        Returns the :class:`UnreadChannel` list it acted on, so a caller (or a
        test) can see what a pass actually did.
        """
        self.stats.sweeps += 1
        communities = await self._communities()
        limiter = asyncio.Semaphore(self.concurrency)

        async def scan(community):
            async with limiter:
                return await self._scan_community(community)

        results = await asyncio.gather(
            *(scan(c) for c in communities), return_exceptions=True
        )
        found: list = []
        for result in results:
            if isinstance(result, list):
                found.extend(result)
            elif isinstance(result, BaseException):
                log.debug("unread: community scan failed: %s", result)

        for channel in found:
            if self.on_unread is not None:
                await self.on_unread(channel)

        # Pace from what we found: activity keeps it fast, quiet slows it down.
        self._quiet_sweeps = 0 if found else self._quiet_sweeps + 1
        return found

    async def _communities(self):
        """The community list, cached between refreshes.

        ``list_communities()`` honours the client's ``expand_communities``
        setting, so on the default lazy client this is a single ListMine
        rather than one GetExtended per community -- but it is still a request,
        and communities do not change every two seconds. Re-listed at most
        every ``community_refresh`` seconds; that is one request per tick saved
        on the steady state.
        """
        now = time.monotonic()
        fresh = now - self._community_cache_at < self.community_refresh
        if self._community_cache and fresh:
            return self._community_cache
        self.stats.requests += 1
        self._community_cache = tuple(
            await self.client.list_communities(refresh=True)
        )
        self._community_cache_at = now
        return self._community_cache

    async def _scan_community(self, community) -> list:
        """One GetExtended -> the unread state of every channel in it."""
        self.stats.requests += 1
        detail = await self.client.community_detail(community.id, refresh=True)
        community_name = getattr(community, "name", "") or ""

        acted: list = []
        for channel in getattr(detail, "text_channels", ()) or ():
            if not is_unread(channel):
                continue
            # Only act once per burst: the same last_activity_at seen twice is
            # the same bold state, not a new message.
            marker = getattr(channel, "last_activity_at", None)
            if self._handled.get(channel.id) == marker:
                continue
            self._handled[channel.id] = marker

            unread = UnreadChannel(
                channel_id=channel.id,
                channel_name=getattr(channel, "name", "") or "",
                community_id=community.id,
                community_name=community_name,
                last_activity_at=marker,
                user_last_viewed_at=getattr(channel, "user_last_viewed_at", None),
            )
            await self._open(unread)
            acted.append(unread)
        return acted

    async def _open(self, unread: UnreadChannel) -> None:
        """Click the channel: read what is new, then clear the bold."""
        self.stats.channels_opened += 1
        if self.on_message is not None:
            try:
                self.stats.requests += 1
                messages = await self.client.messages.list(
                    unread.channel_id,
                    community_id=unread.community_id,
                    direction="older",
                    limit=self.history_limit,
                )
            except Exception as exc:
                log.debug("unread: history for %s failed: %s", unread, exc)
                messages = []
            for message in messages:
                self.stats.messages_read += 1
                await self.on_message(message)

        if self.mark_read:
            await self._mark(unread.channel_id, unread.community_id)

    async def _mark(self, container_id, community_id) -> None:
        try:
            self.stats.requests += 1
            await self.client.messages.set_view_time(
                container_id, community_id=community_id
            )
        except Exception as exc:
            log.debug("unread: set_view_time %s failed: %s", container_id, exc)

    # -- the loop --------------------------------------------------------
    async def _loop(self) -> None:
        """The safety net, and only if one was asked for.

        With ``interval=0`` -- the default -- this waits for stop and does
        nothing else. The pushes are the mechanism; a timer here would only
        re-ask questions the socket has already answered.
        """
        if self.interval <= 0:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("unread sweep failed: %s", exc)
            # Pace from the *end* of the sweep, so a slow pass does not stack.
            elapsed = time.monotonic() - started
            wait = max(0.0, self.current_interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
