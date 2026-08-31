"""Two-account tests: the things one account cannot prove on its own.

Everything here needs a counterparty. A single account can send a DM to itself
and see it in history, but that shows the write succeeded -- not that anything
was *delivered*. Push, DMs, calls, friend requests and blocks are all
send-and-receive, and half of each was untestable until now.

Needs both tokens::

    set ROOT_TOKEN=<first account>
    set ROOT_TOKEN2=<second account>
    pytest -m live2 -v

Kept under its own marker so ``pytest -m live`` remains exactly the
single-account suite -- that stays a stable regression baseline rather than
growing a new dependency.

Three behaviours shape almost every test here:

* **DMs are pushed; channel messages need ``community.attach`` first.** A
  plain channel post never arrives as a ``message`` event on an unattached
  community, so ``wait_for("message")`` on one waits forever. The tests here
  verify delivery through a DM because that path needs no subscription;
  ``TestAttachedChannelPush`` covers the attached case.
* **The mention half of that is not true, or at least not reproducible.**
  Posting to a channel *does* ping every member's socket with a
  ``notification`` event -- but the same event fires for a message containing
  no mention at all, byte for byte, and no mention spelling produces a
  notification-list entry. ``TestMentionFormat`` records the evidence.
* **Handlers are fire-and-forget.** ``dispatch()`` returns before they run, so
  assertions either use ``wait_for`` (which resolves inline) or
  ``drain_events()`` first.

And one trap that is not about push at all: **blocking someone deletes the
friendship, and unblocking does not restore it.** ``TestBlocking`` puts it
back, because DMs and calls need the friendship and would otherwise start
failing several classes later for no visible reason.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from rootpy.enums import UserOnlineStatus
from rootpy.models import build_user_mention
from rootpy.responses import field as _field, items as _items

from .conftest import (
    _ids_match,
    eventually,
    never,
    requires_two_accounts,
    tag,
)

pytestmark = [pytest.mark.live2, pytest.mark.asyncio, requires_two_accounts]

#: Push and delivery are not instant; every wait uses this rather than a
#: sprinkling of magic numbers.
DELIVERY_TIMEOUT = 30


class TestTwoAccountSetup:
    async def test_both_accounts_authenticate(self, me, peer_identity):
        assert me.id and peer_identity.id

    async def test_they_are_different_accounts(self, distinct_accounts):
        first, second = distinct_accounts
        assert first.id != second.id

    async def test_peer_gateway_is_connected(self, peer):
        assert peer.is_connected is True

    async def test_peer_can_read_its_own_identity(self, peer, peer_identity):
        assert peer_identity.username


class TestDirectMessages:
    """DMs are pushed, so this exercises the real delivery path.

    Every test here depends on ``friendship``. Root's default privacy setting
    only accepts DMs from friends, so a fresh pair of burner accounts gets
    PERMISSION_DENIED until a request has been sent *and* accepted. The SDK
    diagnoses that precisely already; the ordering is the prerequisite.
    """

    async def test_open_a_conversation(self, friendship, client, peer_identity):
        conversation = await client.open_dm(peer_identity.id)
        assert conversation.id

    async def test_opening_twice_returns_the_same_conversation(
        self, friendship, client, peer_identity
    ):
        first = await client.open_dm(peer_identity.id)
        second = await client.open_dm(peer_identity.id)
        assert first.id == second.id

    async def test_send_returns_a_message(self, friendship, client, peer_identity):
        result = await client.direct_message(peer_identity.id, f"dm {tag()}")
        sent = getattr(result, "message", result)
        assert sent.id

    async def test_the_peer_actually_receives_it(
        self, friendship, client, peer, peer_identity
    ):
        """The half that a single account can never prove."""
        marker = f"dm-delivery-{tag()}"

        async def is_ours(event):
            message = getattr(event, "message", None)
            return getattr(message, "content", None) == marker

        waiter = asyncio.create_task(
            peer.wait_for("message", check=lambda e: _content(e) == marker,
                          timeout=DELIVERY_TIMEOUT)
        )
        await asyncio.sleep(0.5)
        await client.direct_message(peer_identity.id, marker)
        event = await waiter
        assert _content(event) == marker

    async def test_the_peer_can_reply(
        self, friendship, gateway_client, peer, me
    ):
        """Receiving needs a socket, so this waits on ``gateway_client``.

        The ``client`` fixture is deliberately token-only -- enough for every
        RPC, but it has no gateway, so nothing ever dispatches to it and
        ``wait_for`` can only time out. ``gateway_client`` is the same account
        with the socket connected.
        """
        marker = f"dm-reply-{tag()}"
        waiter = asyncio.create_task(
            gateway_client.wait_for(
                "message",
                check=lambda e: _content(e) == marker,
                timeout=DELIVERY_TIMEOUT,
            )
        )
        await asyncio.sleep(0.5)
        await peer.direct_message(me.id, marker)
        event = await waiter
        assert _content(event) == marker

    async def test_conversation_appears_in_both_lists(
        self, friendship, client, peer, peer_identity, me
    ):
        await client.direct_message(peer_identity.id, f"listing {tag()}")

        async def mine():
            return await client.direct_messages.list()

        async def theirs():
            return await peer.direct_messages.list()

        await eventually(mine, describe="the conversation appears for the sender")
        await eventually(theirs, describe="the conversation appears for the peer")

    async def test_dm_history_contains_the_message(
        self, friendship, client, peer_identity
    ):
        marker = f"dm-history-{tag()}"
        conversation = await client.open_dm(peer_identity.id)
        await client.direct_message(peer_identity.id, marker)

        async def in_history():
            history = await client.list_messages(conversation.id)
            return any(m.content == marker for m in history)

        await eventually(in_history, describe="the DM appears in history")


@pytest_asyncio.fixture(scope="session")
async def shared_channel(client, peer, sandbox, joined_peer):
    """A channel the peer can genuinely see -- asked for, not assumed.

    ``sandbox.channel`` lives in a channel group the sandbox created, and a
    fresh group grants nothing to the default role, so a plain member cannot
    see it. Every push test against it would then be measuring invisibility
    rather than subscription -- a negative that looks like a finding.

    Creating a replacement is not reliable either: a channel added to the
    default group with the group's permission inherited still did not become
    visible to an already-joined member within a minute. So this does not
    create anything. It asks the peer what it can see and uses that -- which is
    the community template's own ``Text`` channel -- so visibility is a fact of
    the fixture rather than a hope.
    """
    detail = await peer.community_detail(sandbox.community_id, refresh=True)
    visible = list(detail.text_channels or ())
    if not visible:
        pytest.skip(
            "the peer can see no channel in the sandbox community; there is "
            "nothing to measure push against"
        )
    return visible[0]


class TestMentionsArePushed:
    """What actually reaches the socket when a message is posted.

    This has been wrong twice, in opposite directions, so it is now an A/B in
    one run rather than two independent assertions.

    First it sent ``<@id>``, waited for any ``notification`` and *skipped* on
    timeout -- passing every run and reading as "mentions are pushed,
    confirmed". Then it was rewritten to claim the opposite: that *any* message
    pings every member identically, so the event says nothing about mentions.

    Both readings came from the same broken instrument. The gateway dispatches
    ``notification`` for every frame it reads, **keepalive pings included**, so
    ``wait_for("notification")`` returns within seconds whatever you send --
    which is why "mentions are pushed" passed, and why "so does a plain
    message" passed too. The payload recorded as identical in both cases,
    ``
``, is a ping.

    Re-measured on ``packet_notification`` -- plain, mention, plain, same
    channel, same run -- the notification is mention-specific: the plain posts
    produced no NOTIFICATION at all and the mention produced one at +0.5 s.

    The mention half needs no ``community.attach``; notifications are
    user-scoped. See ``TestAttachedChannelPush`` for the channel-message half,
    which does.
    """

    async def _notification_for(self, client, peer, channel_id, body):
        """Post ``body`` and report whether a NOTIFICATION packet followed.

        ``packet_notification``, not ``notification``. The gateway dispatches
        ``notification`` for **every frame it reads, keepalive pings
        included** -- so ``wait_for("notification")`` returns within about five
        seconds no matter what was sent, and proves nothing. That is the
        instrument that produced this file's previous conclusion, and the
        payload it recorded as "identical for a message with no mention"
        (``
``) is a ping.

        ``packet_<type>`` events are dispatched per decoded packet type, so
        this waits for the real thing.
        """
        waiter = asyncio.create_task(
            peer.wait_for("packet_notification", timeout=DELIVERY_TIMEOUT)
        )
        await asyncio.sleep(0.5)
        await client.message(channel_id, body)
        try:
            return await waiter is not None
        except asyncio.TimeoutError:
            return False
        finally:
            waiter.cancel()

    async def test_a_mention_pings_the_members_socket(
        self, client, peer, peer_identity, sandbox, joined_peer, shared_channel
    ):
        mention = build_user_mention(peer_identity.id, peer_identity.username)
        assert await self._notification_for(
            client, peer, shared_channel.id, f"{mention} mention-{tag()}"
        ), "a mention no longer reaches the peer's socket"

    async def test_a_plain_message_does_not(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        """The half that makes the test above mean something."""
        assert not await self._notification_for(
            client, peer, shared_channel.id, f"no mention in this one {tag()}"
        ), (
            "a plain post pinged the socket. Either the notification is an "
            "activity ping after all -- which is what this file claimed "
            "before -- or the peer is attached to the community and this is "
            "the MESSAGE half leaking in."
        )


class TestCallSignalling:
    """Ringing is a plain RPC; only the media stack needs aiortc.

    ``call_user`` opens the DM and creates a session, which rings the other
    side. The peer receives ``packet_direct_message_ring``. None of that
    touches WebRTC, so the signalling path is testable without the optional
    voice extra installed.

    Opening the DM is the first step, so these inherit the friendship
    requirement.
    """

    async def test_calling_creates_a_session(self, friendship, client, peer_identity):
        session = await client.call(peer_identity.id)
        try:
            assert session.session_id
        finally:
            await client.calls.disconnect()

    async def test_the_peer_is_rung(self, friendship, client, peer, peer_identity):
        waiter = asyncio.create_task(
            peer.wait_for(
                "packet_direct_message_ring", timeout=DELIVERY_TIMEOUT
            )
        )
        await asyncio.sleep(0.5)
        session = await client.call(peer_identity.id)
        try:
            event = await waiter
            assert event is not None
        finally:
            await client.calls.disconnect()

    async def test_disconnect_ends_the_session(self, friendship, client, peer_identity):
        await client.call(peer_identity.id)
        await client.calls.disconnect()
        assert client.calls.active_session is None


class TestFriendRequests:
    """Send on one account, see it arrive and be answered on the other.

    The ``friendship`` fixture performs the round trip -- request from one
    account, accept from the other -- so these assert it actually took rather
    than repeating it.
    """

    async def test_pending_is_readable_on_both(self, client, peer):
        assert await client.pending_friend_requests() is not None
        assert await peer.pending_friend_requests() is not None

    async def test_they_end_up_friends(self, friendship, client, peer_identity):
        ids = await client.friends.friend_ids()
        assert any(_ids_match(peer_identity.id, existing) for existing in ids), (
            "friendship fixture reported success but the friend list disagrees"
        )

    async def test_friendship_is_mutual(self, friendship, peer, me):
        ids = await peer.friends.friend_ids()
        assert any(_ids_match(me.id, existing) for existing in ids)

    async def test_nothing_is_left_pending(self, friendship, peer, me):
        assert await peer.friend_requests.pending_from(me.id) is None

    async def test_accept_from_returns_false_with_nothing_pending(
        self, friendship, peer, me
    ):
        """False rather than raising -- callers poll this."""
        assert await peer.friend_requests.accept_from(me.id) is False

    async def test_friend_lists_are_readable(self, client, peer):
        assert await client.list_friends() is not None
        assert await peer.list_friends() is not None


class TestBlocking:
    """Block and unblock -- and the friendship they quietly destroy.

    ``test_unblocked_state_is_restored`` used to check only the *block list*,
    and passed, so the name over-claimed by a mile: blocking also deletes the
    friendship, in both directions, and unblocking does not bring it back.

    That went unnoticed because nothing between this class and the end of the
    run looked at the friendship. Adding a test that did made the two accounts
    turn up unfriended, and the cause took a controlled block/unblock probe to
    find -- the fixture had been silently re-establishing the friendship on
    the *next* run every time.

    Restoration is autouse rather than written into each test, because the
    first attempt at this fixed only the test that *asserts* the destruction
    and left the two ordinary block/unblock tests still tearing it down --
    which failed in exactly the same way one run later. A test that blocks
    must not be able to forget.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def _restore_after_blocking(
        self, client, peer, me, peer_identity, friendship
    ):
        """Put the friendship back after every test in this class."""
        yield
        if not await client.friends.is_friend(peer_identity.id):
            await _restore_friendship(client, peer, me, peer_identity)

    async def test_block_then_unblock(self, friendship, client, peer_identity):
        await client.block(peer_identity.id)
        try:
            blocked = await client.list_blocked()
            assert blocked is not None
        finally:
            await client.unblock(peer_identity.id)

    async def test_the_block_list_no_longer_contains_them(
        self, friendship, client, peer_identity
    ):
        await client.block(peer_identity.id)
        await client.unblock(peer_identity.id)
        blocked = await client.list_blocked()
        ids = {getattr(b, "user_id", getattr(b, "id", None)) for b in blocked}
        assert peer_identity.id not in ids

    async def test_blocking_destroys_the_friendship(
        self, friendship, client, peer, me, peer_identity
    ):
        """The finding, stated. Restores the friendship on the way out.

        Kept as an assertion rather than a comment because it is exactly the
        kind of behaviour that could change server-side without anything else
        in the suite noticing -- and if it does change, this is a much better
        place to find out than a ``PERMISSION_DENIED`` from a DM test.
        """
        assert await client.friends.is_friend(peer_identity.id), (
            "not friends going in, so this cannot show blocking removed it"
        )
        try:
            await client.block(peer_identity.id)

            async def unfriended():
                return not await client.friends.is_friend(peer_identity.id)

            assert await eventually(
                unfriended, describe="blocking removes the friendship"
            ), "blocking no longer removes the friendship -- update the docs"

            await client.unblock(peer_identity.id)

            still_gone = not await client.friends.is_friend(peer_identity.id)
            print(
                "\n   block/unblock and friendship:"
                f"\n     restored by unblock: {not still_gone}\n"
            )
            assert still_gone, (
                "unblocking restored the friendship now -- BlockManager.block's "
                "docstring says it does not, and should be corrected"
            )
        finally:
            # The autouse fixture restores the friendship; this only has to
            # make sure the block itself is not left in place.
            await client.unblock(peer_identity.id)


class TestCommunityMembershipEvents:
    async def test_peer_is_in_the_community(
        self, client, sandbox, peer_identity, joined_peer
    ):
        members = await client.get_members(sandbox.community_id, refresh=True)
        assert any(m.user_id == peer_identity.id for m in members)

    async def test_owner_can_set_the_peers_nickname(
        self, client, sandbox, peer_identity, joined_peer
    ):
        await client.members.edit_nickname(
            sandbox.community_id, peer_identity.id, f"peer{tag()}"
        )

    async def test_peer_sees_the_community_in_its_own_list(
        self, peer, sandbox, joined_peer
    ):
        communities = await peer.list_communities(refresh=True)
        assert any(c.id == sandbox.community_id for c in communities)


class TestPresence:
    """Presence is ``min(ceiling, device)``, and only two accounts show it.

    One account cannot prove any of this. It can set a status and read the
    same status back, which shows that the write succeeded. It does not show
    that another person sees anything, and that is the part that failed:
    ``set_online_status`` set the ceiling only, so an account that never
    announced a device stayed invisible while both requests returned success.

    Every test here puts the peer's ceiling back the way it found it.
    """

    @pytest_asyncio.fixture
    async def restored_ceiling(self, peer):
        """Give back the peer's ceiling, whatever the test does to it."""
        identity = await peer.whoami(refresh=True)
        original = int(getattr(identity, "max_online_status", 0) or 0)
        yield original
        if original:
            await peer.users.set_online_status(original)
            await peer.user_settings.set_device_online_status(
                int(UserOnlineStatus.ACTIVE)
            )

    async def test_peer_status_change_is_visible_to_itself(self, peer):
        await peer.update_status(f"probe {tag()}")
        try:
            refreshed = await peer.whoami(refresh=True)
            assert refreshed is not None
        finally:
            await peer.update_status(None)

    async def test_set_presence_is_visible_to_the_other_account(
        self, client, peer, peer_identity, restored_ceiling
    ):
        """The half one account can never prove.

        The device goes down first, and that is the point. ``peer`` is
        connected, so its device is already Active, and a ceiling-only
        implementation would pass this test without announcing anything. With
        the device down, only a call that sets **both** halves can make the
        other account see this one.
        """
        for name, expected in (
            ("idle", UserOnlineStatus.INACTIVE),
            ("online", UserOnlineStatus.ACTIVE),
        ):
            await peer.user_settings.set_device_online_status(
                int(UserOnlineStatus.DISCONNECTED)
            )
            await peer.set_presence(name)
            assert peer.presence is expected

            async def seen():
                profiles = await client.get_profiles([peer_identity.id])
                profile = profiles.get(peer_identity.id)
                status = int(getattr(profile, "online_status", 0) or 0)
                return status == int(expected)

            assert await eventually(seen, timeout=DELIVERY_TIMEOUT), (
                f"the other account never saw {name} ({expected.name})"
            )

    async def test_the_ceiling_alone_leaves_the_account_invisible(
        self, client, peer, peer_identity, restored_ceiling
    ):
        """The exact fault this API exists to stop.

        Set the ceiling to Active, announce no device, and Root shows the
        account as offline to everybody. Both requests return success, so
        nothing tells the caller. ``set_presence`` sets both halves, which is
        the difference proved by the test above.
        """
        await peer.user_settings.set_device_online_status(
            int(UserOnlineStatus.DISCONNECTED)
        )
        await peer.users.set_online_status(int(UserOnlineStatus.ACTIVE))

        async def offline_to_the_other_account():
            profiles = await client.get_profiles([peer_identity.id])
            profile = profiles.get(peer_identity.id)
            status = int(getattr(profile, "online_status", 0) or 0)
            return status != int(UserOnlineStatus.ACTIVE)

        assert await eventually(
            offline_to_the_other_account, timeout=DELIVERY_TIMEOUT
        ), "a maximal ceiling with no device must not read as Active"

    async def test_watch_presence_reports_a_change(
        self, client, peer, peer_identity, restored_ceiling
    ):
        seen = []

        async def note(user_id, status):
            seen.append((user_id, status))

        watch = client.watch_presence(
            note, users=[peer_identity.id], poll=2.0
        )
        try:
            await peer.set_presence("idle")
            assert await eventually(
                lambda: any(
                    status is UserOnlineStatus.INACTIVE for _u, status in seen
                ),
                timeout=DELIVERY_TIMEOUT,
            ), f"watch_presence saw {seen}"
        finally:
            await watch.stop()


class TestAttachLifetime:
    """``attach`` is silent, and so is losing it.

    The request returns nothing at all, so the only proof is the other
    account reading ``AttachedUserIds`` back off the community.
    """

    async def test_held_attaches_and_leaving_detaches(
        self, client, peer, sandbox, joined_peer, peer_identity
    ):
        async def peer_is_attached():
            extended = await client.fetch_community(sandbox.community_id)
            return extended.is_attached(peer_identity.id)

        async def peer_is_not_attached():
            return not await peer_is_attached()

        async with peer.community.held(sandbox.community_id):
            assert await eventually(
                peer_is_attached, timeout=DELIVERY_TIMEOUT
            ), "held() did not put the account in AttachedUserIds"

        assert await eventually(
            peer_is_not_attached, timeout=DELIVERY_TIMEOUT
        ), "leaving the block did not detach"

    async def test_attached_user_ids_reads_the_raw_response(
        self, client, sandbox, peer_identity
    ):
        """The accessor must answer even when nobody is attached."""
        extended = await client.fetch_community(sandbox.community_id)
        assert isinstance(extended.attached_user_ids, tuple)
        assert all(isinstance(i, str) for i in extended.attached_user_ids)
        assert extended.is_attached(peer_identity.id) in (True, False)


def _content(event) -> str | None:
    """Message content off a message event, whatever shape it arrives in."""
    message = getattr(event, "message", None)
    return getattr(message, "content", None)


class TestModeration:
    """Kick, ban, unban -- destructive, and previously 0/5.

    Everything happens in the throwaway sandbox community, and the peer is
    re-invited afterwards so later tests still find it a member. Ordering
    matters here in a way it does not elsewhere: a kick that is not undone
    breaks ``TestCommunityMembershipEvents``.
    """

    async def test_list_bans_is_readable(self, client, sandbox):
        assert await client.moderation.list_bans(sandbox.community_id) is not None

    async def test_ban_then_unban(self, client, sandbox, peer_identity, joined_peer):
        await client.moderation.ban(
            sandbox.community_id, peer_identity.id, reason="rootpy self-test"
        )
        try:
            bans = await client.moderation.list_bans(sandbox.community_id)
            assert bans is not None
        finally:
            await client.moderation.unban(sandbox.community_id, peer_identity.id)

    async def test_a_ban_appears_in_the_list(
        self, client, sandbox, peer_identity, joined_peer
    ):
        await client.moderation.ban(
            sandbox.community_id, peer_identity.id, reason="listing probe"
        )
        try:
            bans = await client.moderation.list_bans(sandbox.community_id)
            assert isinstance(bans, list), "list_bans should return records"
            ids = {
                getattr(b, "user_id", None) or getattr(b, "id", None)
                for b in bans
            }
            assert any(_ids_match(peer_identity.id, i) for i in ids if i)
        finally:
            await client.moderation.unban(sandbox.community_id, peer_identity.id)

    async def test_unban_clears_it(self, client, sandbox, peer_identity, joined_peer):
        await client.moderation.ban(sandbox.community_id, peer_identity.id)
        await client.moderation.unban(sandbox.community_id, peer_identity.id)
        bans = await client.moderation.list_bans(sandbox.community_id)
        ids = {getattr(b, "user_id", getattr(b, "id", None)) for b in (bans or [])}
        assert not any(_ids_match(peer_identity.id, i) for i in ids if i)

    async def test_kick_then_rejoin(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        """The peer is put back, or every later membership test breaks.

        A ban removes the member as well as barring them, so by the time this
        runs the earlier ban/unban tests have already taken the peer out --
        Kick then answers NOT_FOUND. Re-join first so the kick has a member to
        act on.
        """
        members = await client.get_members(sandbox.community_id, refresh=True)
        if not any(_ids_match(peer_identity.id, m.user_id) for m in members):
            invite = await client.invites.create(sandbox.community_id, max_uses=5)
            await peer.invites.join(getattr(invite, "code", None))

            async def joined():
                current = await client.get_members(
                    sandbox.community_id, refresh=True
                )
                return any(
                    _ids_match(peer_identity.id, m.user_id) for m in current
                )

            await eventually(joined, describe="the peer rejoins before the kick")

        await client.moderation.kick(sandbox.community_id, peer_identity.id)

        async def removed():
            current = await client.get_members(sandbox.community_id, refresh=True)
            return not any(
                _ids_match(peer_identity.id, m.user_id) for m in current
            )

        was_removed = await eventually(
            removed, describe="the kicked member leaves the roster"
        )

        invite = await client.invites.create(sandbox.community_id, max_uses=5)
        await peer.invites.join(getattr(invite, "code", None))

        async def back():
            current = await client.get_members(sandbox.community_id, refresh=True)
            return any(_ids_match(peer_identity.id, m.user_id) for m in current)

        assert was_removed, "kick did not remove the member"
        await eventually(back, describe="the peer is restored after the kick")


class TestVoiceAdmin:
    """Server-side voice moderation. Readable without anyone in the channel."""

    async def test_list_voice_members(self, client, sandbox, joined_peer):
        voice = await client.community.create_voice_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-va-{tag()}"
        )
        listing = await client.voice_admin.list(sandbox.community_id, voice.id)
        assert listing is not None

    async def test_listing_an_empty_channel_is_not_an_error(
        self, client, sandbox, joined_peer
    ):
        voice = await client.community.create_voice_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-vb-{tag()}"
        )
        assert await client.voice_admin.list(sandbox.community_id, voice.id) is not None


class TestFriendGroups:
    """Friend list organisation -- 0/5 before this."""

    async def test_list_is_readable(self, client, friendship):
        assert await client.friend_groups.list() is not None

    async def test_create_edit_delete(self, client, friendship):
        group = await client.friend_groups.create(f"rootpygroup{tag()}")
        group_id = getattr(group, "id", None)
        try:
            assert group_id
            await client.friend_groups.edit(group_id, f"rootpyrenamed{tag()}")
        finally:
            if group_id:
                await client.friend_groups.delete(group_id)

    async def test_move_a_group(self, client, friendship):
        """Move needs a reference group -- Root requires BeforeFriendshipGroupId."""
        first = await client.friend_groups.create(f"rootpyfirst{tag()}")
        second = await client.friend_groups.create(f"rootpysecond{tag()}")
        ids = [getattr(first, "id", None), getattr(second, "id", None)]
        try:
            await client.friend_groups.move(ids[1], before_group_id=ids[0])
        finally:
            for group_id in ids:
                if group_id:
                    await client.friend_groups.delete(group_id)

    async def test_move_without_a_reference_is_refused_locally(
        self, client, friendship
    ):
        """Raises before the round trip rather than after."""
        with pytest.raises((ValueError, TypeError)):
            await client.friend_groups.move("some-id", before_group_id="")


# --------------------------------------------------------------------------
# the member-manager spellings of moderation
# --------------------------------------------------------------------------
async def _ensure_member(client, peer, sandbox, peer_identity):
    """Put the peer back in the sandbox if a ban or kick took them out.

    Every test below removes the peer and has to restore them, and the
    existing ``TestModeration`` already learned the hard way that ordering
    matters here -- a kick that is not undone breaks every later membership
    test. One helper rather than four copies of the dance.
    """

    async def present():
        members = await client.get_members(sandbox.community_id, refresh=True)
        return any(_ids_match(peer_identity.id, m.user_id) for m in members)

    if await present():
        return True

    invite = await client.invites.create(sandbox.community_id, max_uses=5)
    await peer.invites.join(getattr(invite, "code", None))
    return await eventually(present, describe="the peer is back in the community")


async def _absent(client, sandbox, peer_identity):
    members = await client.get_members(sandbox.community_id, refresh=True)
    return not any(_ids_match(peer_identity.id, m.user_id) for m in members)


class TestMemberManagerModeration:
    """``client.members`` forwards to ``client.moderation`` -- 5/10 before this.

    The forwards themselves are what is under test. Three of the seventeen
    bugs found live were a manager passing the wrong argument to a layer that
    worked, and a forward reads as correct right up until it runs.
    """

    async def test_ban_then_unban(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.members.ban(
                sandbox.community_id, peer_identity.id,
                reason="rootpy members.ban",
            )
            bans = await client.moderation.list_bans(sandbox.community_id)
            ids = {
                getattr(b, "user_id", None) or getattr(b, "id", None)
                for b in (bans or [])
            }
            assert any(_ids_match(peer_identity.id, i) for i in ids if i), (
                "members.ban did not produce a ban record"
            )
        finally:
            await client.members.unban(sandbox.community_id, peer_identity.id)
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_ban_bulk_takes_a_list(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        """``user_ids``, plural -- ``add_role`` shipped the singular and sent nothing."""
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.members.ban_bulk(
                sandbox.community_id, [peer_identity.id],
                reason="rootpy members.ban_bulk",
            )
            bans = await client.moderation.list_bans(sandbox.community_id)
            ids = {
                getattr(b, "user_id", None) or getattr(b, "id", None)
                for b in (bans or [])
            }
            assert any(_ids_match(peer_identity.id, i) for i in ids if i), (
                "ban_bulk did not produce a ban record"
            )
        finally:
            await client.members.unban(sandbox.community_id, peer_identity.id)
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_kick_removes_the_member(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.members.kick(sandbox.community_id, peer_identity.id)
            assert await eventually(
                lambda: _absent(client, sandbox, peer_identity),
                describe="members.kick removes the member",
            )
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_kick_bulk_removes_the_member(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.members.kick_bulk(
                sandbox.community_id, [peer_identity.id]
            )
            assert await eventually(
                lambda: _absent(client, sandbox, peer_identity),
                describe="members.kick_bulk removes the member",
            )
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)


class TestCommunityManagerModeration:
    """``community.ban`` / ``kick`` / ``unban`` -- the third spelling of the same three."""

    async def test_ban_then_unban(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.community.ban(
                sandbox.community_id, peer_identity.id,
                reason="rootpy community.ban",
            )
            bans = await client.moderation.list_bans(sandbox.community_id)
            assert bans is not None
        finally:
            await client.community.unban(sandbox.community_id, peer_identity.id)
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_kick(self, client, peer, sandbox, peer_identity, joined_peer):
        await _ensure_member(client, peer, sandbox, peer_identity)
        try:
            await client.community.kick(sandbox.community_id, peer_identity.id)
            assert await eventually(
                lambda: _absent(client, sandbox, peer_identity),
                describe="community.kick removes the member",
            )
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)


class TestCommunityInvite:
    """``moderation.invite_user`` is a direct invite, not an invite *link*.

    Distinct from ``client.invites``, which is ``CommunityMemberInviteLink``:
    this is ``CommunityMemberInviteCreate``, addressed at one user id.

    Run 1 called it with the peer already a member and got ``already_exists``
    from both argument shapes -- a correct answer to a meaningless question,
    which verified nothing about the call. You cannot invite someone who is
    already in the room. The peer is kicked first now, and put back after.
    """

    async def test_invite_a_non_member(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        await _ensure_member(client, peer, sandbox, peer_identity)
        await client.moderation.kick(sandbox.community_id, peer_identity.id)
        assert await eventually(
            lambda: _absent(client, sandbox, peer_identity),
            describe="the peer is out before being invited",
        )

        try:
            await client.moderation.invite_user(
                sandbox.community_id, peer_identity.id
            )
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_invite_with_roles_attached(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        """``community_role_ids`` is the plural field -- the shape that has bitten."""
        await _ensure_member(client, peer, sandbox, peer_identity)
        await client.moderation.kick(sandbox.community_id, peer_identity.id)
        assert await eventually(
            lambda: _absent(client, sandbox, peer_identity),
            describe="the peer is out before being invited",
        )

        try:
            await client.moderation.invite_user(
                sandbox.community_id, peer_identity.id,
                role_ids=[sandbox.role.id],
            )
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)

    async def test_inviting_an_existing_member_is_refused(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        """The run-1 observation, kept as the assertion it should have been."""
        from rootpy.exceptions import RootError

        await _ensure_member(client, peer, sandbox, peer_identity)
        with pytest.raises(RootError) as caught:
            await client.moderation.invite_user(
                sandbox.community_id, peer_identity.id
            )
        assert "exist" in str(caught.value).lower(), (
            f"expected an already-exists answer, got: {caught.value}"
        )


class TestVoiceAdminActions:
    """Kick and force-mute, against a voice channel with nobody in it.

    This is the honest limit: joining voice needs the Playwright/Chromium
    media stack, which is opt-in and unverified, so there is no way to put a
    real participant in the channel from here. What *can* be verified is the
    part that has broken before -- that the request is addressed and encoded
    correctly and reaches Root's handler rather than being rejected as
    malformed. A NOT_FOUND for an absent participant is a pass; an
    INVALID_ARGUMENT naming a field is not, and the ladder reports which.
    """

    async def test_kick_from_voice(
        self, client, sandbox, peer_identity, joined_peer
    ):
        from rootpy.exceptions import GrpcWebError

        voice = await client.community.create_voice_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-vk-{tag()}"
        )
        try:
            try:
                await client.voice_admin.kick(
                    sandbox.community_id, voice.id, peer_identity.id
                )
            except GrpcWebError as exc:
                assert "INVALID_ARGUMENT" not in str(exc), (
                    "WebRtcKick was rejected as malformed rather than as "
                    f"'nobody there': {exc}"
                )
        finally:
            await client.community.delete_channel(
                sandbox.community_id, voice.id
            )

    async def test_force_mute_and_deafen(
        self, client, sandbox, peer_identity, joined_peer
    ):
        from rootpy.exceptions import GrpcWebError

        voice = await client.community.create_voice_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-vm-{tag()}"
        )
        try:
            try:
                await client.voice_admin.set_member_mute_deafen(
                    sandbox.community_id, voice.id, peer_identity.id,
                    muted=True, deafened=False,
                )
            except GrpcWebError as exc:
                assert "INVALID_ARGUMENT" not in str(exc), (
                    "WebRtcSetMuteAndDeafenOther was rejected as malformed: "
                    f"{exc}"
                )
        finally:
            await client.community.delete_channel(
                sandbox.community_id, voice.id
            )


# --------------------------------------------------------------------------
# DM member service -- the cache reader, find, and reply-in-place
# --------------------------------------------------------------------------
class TestDMMemberService:
    async def test_find_locates_the_conversation(
        self, friendship, client, peer_identity
    ):
        await client.dm.open(peer_identity.id)
        found = await client.dm.find(peer_identity.id)
        assert found is not None, "find could not see a DM we just opened"

    async def test_cached_is_a_synchronous_reader(
        self, friendship, client, peer_identity
    ):
        """``dm.cached`` reads the local map -- awaiting it raises TypeError."""
        opened = await client.dm.open(peer_identity.id)
        cached = client.dm.cached(peer_identity.id)
        assert cached is not None, "open() did not populate the DM cache"
        assert cached.id == opened.id

    async def test_cached_is_none_for_a_stranger(self, client, me):
        assert client.dm.cached("not-a-user-id") is None

    async def test_reply_lands_in_the_same_conversation(
        self, friendship, gateway_client, peer, me, peer_identity
    ):
        """``dm.reply`` sends to the message's own container, resolving nothing.

        Needs a *received* message to reply to, so the peer sends first and
        the reply goes back over the socket the other way -- both directions
        verified by delivery rather than by the write returning.
        """
        marker = f"dm-in-{tag()}"
        answer = f"dm-back-{tag()}"

        inbound = asyncio.create_task(
            gateway_client.wait_for(
                "message",
                check=lambda e: _content(e) == marker,
                timeout=DELIVERY_TIMEOUT,
            )
        )
        await asyncio.sleep(0.5)
        await peer.direct_message(me.id, marker)
        event = await inbound

        received = getattr(event, "message", None)
        assert received is not None and received.container_id, (
            f"no replyable message on the event: {event!r}"
        )

        returning = asyncio.create_task(
            peer.wait_for(
                "message",
                check=lambda e: _content(e) == answer,
                timeout=DELIVERY_TIMEOUT,
            )
        )
        await asyncio.sleep(0.5)
        await gateway_client.dm.reply(received, answer)
        back = await returning
        assert _content(back) == answer


# --------------------------------------------------------------------------
# notifications -- viewing and deleting a specific one
# --------------------------------------------------------------------------
#: Candidate user-mention spellings, the library's own format first.
#:
#: ``rootpy.commands.USER_MENTION_RE`` has matched
#: ``[@name](root://user/<id>)`` since it was written, from real Root message
#: content -- so that is the format, and ``User.mention`` now produces it.
#: The Discord-style spellings are kept only as controls: they are what the
#: suite used to send, and showing they behave no differently from the real
#: format is part of the finding below.
MENTION_FORMATS = [
    ("markdown (User.mention)",
     lambda user: build_user_mention(user.id, user.username)),
    ("markdown, no @ in the label",
     lambda user: f"[{user.username}](root://user/{user.id})"),
    ("bare root://user/id", lambda user: f"root://user/{user.id}"),
    ("discord-style id", lambda user: "<@" + f"{user.id}>"),
    ("discord-style username", lambda user: "<@" + f"{user.username}>"),
]


async def _inbox(peer):
    raw = await peer.notifications.list()
    return _items(raw, "notifications")


async def _inbox_ids(peer) -> set:
    """The set of notification ids currently in the peer's inbox.

    Run 1's version of this counted ``len(notifications.list())`` and reported
    that *no* mention spelling notified. That was the instrument, not the
    finding: ``NotificationList`` is paginated, so once the page is full a new
    arrival does not change the length, and the same run's
    ``TestMentionsArePushed`` -- which waits on a pushed event rather than a
    count -- passed on ``<@id>``. Comparing id sets detects an arrival however
    full the page is.
    """
    return {
        _field(item, "id")
        for item in await _inbox(peer)
    } - {None}


class TestMentionFormat:
    """No mention spelling produces a notification. Recorded, not asserted away.

    HANDOFF says "only mentions and DMs arrive over the socket", and the
    mention half of that turns out to rest on nothing. Measured across two
    runs and three separate probes, against a peer confirmed to be a member:

    * **No spelling produces a notification-list entry.** Not
      ``[@name](root://user/<id>)`` -- the format
      ``rootpy.commands.USER_MENTION_RE`` matches, so the one Root's own
      client writes -- nor the label-less markdown variant, nor a bare
      ``root://user/<id>``, nor the Discord-style ``<@id>`` the suite used to
      send.
    * **The socket event is not mention-specific.** A message containing no
      mention at all produces the same ``notification`` event, with a payload
      identical byte for byte.
    * The notification list does work: ``COMMUNITY_MEMBER_KICKED`` and
      ``COMMUNITY_MEMBER_BANNED`` entries arrive there in the same run, so
      this is not a broken reader.

    What is *not* established is why. It could be a per-account notification
    setting, something Root's client sends that we do not, or mentions may
    simply not notify. Guessing at that is how runs get spent, so this
    records the observation with the evidence and does not pretend to a
    conclusion.

    Marked ``xfail(strict=False)``: if a spelling ever does notify, it turns
    up as an XPASS rather than staying silently green.
    """

    @pytest.mark.xfail(
        reason="no mention spelling notifies; see the class docstring",
        strict=False,
    )
    async def test_some_mention_spelling_notifies(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        assert await _ensure_member(client, peer, sandbox, peer_identity), (
            "the peer is not in the community, so a mention could not notify "
            "it whatever the spelling -- this says nothing about the format"
        )

        notified, silent = [], []
        for label, build in MENTION_FORMATS:
            before = await _inbox_ids(peer)

            async def arrived(_before=before):
                return (await _inbox_ids(peer)) - _before

            await client.message(
                sandbox.channel.id,
                f"{build(peer_identity)} rootpy mention probe {tag()}",
            )
            try:
                await eventually(
                    arrived,
                    timeout=12,
                    describe=f"{label} produces a notification",
                )
            except AssertionError:
                silent.append(label)
                continue
            notified.append(label)

        print(
            "\n   user mention format:"
            f"\n     notified: {notified or 'none'}"
            f"\n     silent:   {silent or 'none'}\n"
        )
        assert notified, (
            "no mention spelling produced a notification for a confirmed "
            f"member. Silent: {silent}"
        )

    async def test_the_notification_list_itself_works(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        """The control for the xfail above.

        Without this, "no notification arrived" is indistinguishable from
        "the reader is broken" -- which is the mistake that produced a false
        negative here once already, when the probe counted a paginated list's
        length instead of comparing ids.
        """
        await _ensure_member(client, peer, sandbox, peer_identity)
        before = await _inbox_ids(peer)

        await client.moderation.kick(sandbox.community_id, peer_identity.id)
        try:
            async def arrived():
                return (await _inbox_ids(peer)) - before

            new = await eventually(
                arrived,
                timeout=30,
                describe="a kick produces a notification the reader can see",
            )
            assert new, "the notification reader sees nothing at all"
        finally:
            await _ensure_member(client, peer, sandbox, peer_identity)


class TestNotificationLifecycle:
    """Mark-viewed and delete need a notification that is *ours to identify*.

    Any notification will do -- these tests need an id to operate on, not a
    particular kind -- so the peer's existing inbox is used when it has one
    and a mention is sent only when it does not. That keeps them off the
    unconfirmed mention format as a hard dependency.

    Everything here runs on ``peer``: wiping the primary account's
    notifications could take the friend request ``friendship`` depends on.
    """

    async def _a_notification(self, client, peer, sandbox, peer_identity):
        items = await _inbox(peer)
        if items:
            return items[0]

        # Only reached when the inbox is empty, and only mentions can fill it
        # from here -- which needs the peer to be a member.
        await _ensure_member(client, peer, sandbox, peer_identity)

        async def first():
            items = await _inbox(peer)
            return items[0] if items else None

        for _label, build in MENTION_FORMATS:
            await client.message(
                sandbox.channel.id,
                f"{build(peer_identity)} rootpy notification probe {tag()}",
            )
            try:
                return await eventually(
                    first,
                    timeout=8,
                    describe="a notification reaches the peer",
                )
            except AssertionError:
                continue
        return None

    async def test_mark_one_viewed(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        notification = await self._a_notification(
            client, peer, sandbox, peer_identity
        )
        if notification is None:
            pytest.skip("could not obtain a notification for the peer")
        notification_id = getattr(notification, "id", None)
        if not notification_id:
            pytest.skip(f"no id on the notification: {notification!r}")
        await peer.notifications.mark_viewed(notification_id)

    async def test_delete_one(
        self, client, peer, sandbox, peer_identity, joined_peer
    ):
        notification = await self._a_notification(
            client, peer, sandbox, peer_identity
        )
        if notification is None:
            pytest.skip("could not obtain a notification for the peer")
        notification_id = getattr(notification, "id", None)
        if not notification_id:
            pytest.skip(f"no id on the notification: {notification!r}")
        await peer.notifications.delete(notification_id)

        async def gone():
            items = await _inbox(peer)
            return not any(
                getattr(item, "id", None) == notification_id for item in items
            )

        assert await eventually(
            gone, describe="the deleted notification leaves the list"
        )

    async def test_delete_all_empties_the_peer_inbox(self, peer):
        """Deliberately last, and deliberately on the peer.

        ``delete_all`` is not scoped to anything -- it empties the account's
        entire notification inbox. Run against the primary account it could
        remove a pending friend request that the ``friendship`` fixture is
        waiting on, so it goes to the peer, at the bottom of the last live
        module, where nothing downstream reads notifications.
        """
        await peer.notifications.delete_all()

        async def empty():
            return len(await _inbox(peer)) == 0

        assert await eventually(
            empty, describe="the peer's notification list empties"
        )


# --------------------------------------------------------------------------
# the friendship cycle -- last in the last live module, on purpose
# --------------------------------------------------------------------------
async def _are_friends(client, user_id) -> bool:
    return await client.friends.is_friend(user_id)


async def _restore_friendship(client, peer, me, peer_identity):
    """Last-resort repair, so a broken cycle does not leave the pair apart.

    Deliberately the *other* direction from the test above -- we ask, the peer
    accepts -- because if something about answering a request from the peer is
    what failed, repeating it is unlikely to work. ``accept_all`` is the final
    fallback: these are burners with at most one request in flight, and not
    depending on sender matching is the point.
    """
    from rootpy.exceptions import RootError

    try:
        await client.friend_requests.send(peer_identity.username)
    except RootError as exc:
        print(f"\n   repair: request refused ({exc})\n")

    async def accepted():
        if await peer.friend_requests.accept_from(me.id):
            return True
        return bool(await peer.friend_requests.accept_all())

    try:
        await eventually(accepted, timeout=30, describe="the peer re-accepts")
    except AssertionError:
        pass

    async def friends_again():
        return await client.friends.is_friend(peer_identity.id)

    try:
        await eventually(
            friends_again, timeout=20, describe="the friendship is restored"
        )
        print("\n   friendship repaired after a failed cycle\n")
    except AssertionError:
        pytest.fail(
            f"{me.username} and {peer_identity.username} are LEFT UNFRIENDED. "
            "Every DM, call and invite test will fail until this is fixed. "
            "The `friendship` fixture is idempotent, so re-running the live "
            "suite should repair it; if it does not, accept the request by "
            "hand."
        )


async def _request_and_wait(sender, receiver, sender_identity, username):
    """Send a friend request and return the notification it produced.

    Returns None rather than raising if the request is refused or never
    arrives, so the caller can report *which* stage of the cycle broke
    instead of a bare timeout.
    """
    from rootpy.exceptions import RootError

    try:
        await sender.friend_requests.send(username)
    except RootError as exc:
        print(f"\n   request from {username} refused: {exc}\n")
        return None

    async def arrived():
        return await receiver.friend_requests.pending_from(sender_identity)

    try:
        return await eventually(
            arrived, timeout=30, describe="the friend request arrives"
        )
    except AssertionError:
        return None


class TestFriendshipCycle:
    """The only route to `respond`, and it has to put things back.

    Six methods -- ``friends.remove``/``accept``/``reject``/``respond`` and
    ``friend_requests.accept``/``decline``/``respond`` -- all act on a
    *pending* request, and the two accounts are permanently friends, so there
    is never one pending. Reaching them means unfriending the pair.

    That is why this was previously left out: with results read from a single
    batched run, a failure halfway leaves the accounts unfriended and every
    DM, call and invite test after it fails for a reason that looks like an
    SDK bug and is not.

    Two things make it safe enough now. It is the last test in the last live
    module, so nothing downstream depends on the friendship; and the
    restoration is *verified* in a ``finally`` rather than assumed, with the
    failure message saying plainly what state the accounts were left in.

    The cycle also answers a question nothing else does: whether Root lets the
    same person re-request immediately after being declined. Each stage
    reports, so a refusal there is legible rather than a timeout.
    """

    async def test_the_full_unfriend_respond_refriend_cycle(
        self, client, peer, me, peer_identity, friendship
    ):
        stages = []

        assert await _are_friends(client, peer_identity.id), (
            "the accounts were not friends to begin with, so this test cannot "
            "establish anything about removing a friendship"
        )

        try:
            # ---- friends.remove -------------------------------------------
            await client.friends.remove(peer_identity.id)

            async def unfriended():
                return not await _are_friends(client, peer_identity.id)

            assert await eventually(
                unfriended, describe="friends.remove drops the friendship"
            )
            stages.append("remove: ok")

            async def peer_agrees():
                return not await _are_friends(peer, me.id)

            mutual = False
            try:
                mutual = bool(await eventually(
                    peer_agrees, timeout=15,
                    describe="the peer also stops seeing us as a friend",
                ))
            except AssertionError:
                pass
            stages.append(
                f"remove is mutual: {mutual}"
                + ("" if mutual else "  <-- one-sided, worth knowing")
            )

            # ---- friend_requests.decline ----------------------------------
            notification = await _request_and_wait(
                peer, client, peer_identity, me.username
            )
            assert notification is not None, (
                "no friend request arrived after unfriending.\n  "
                + "\n  ".join(stages)
            )
            await client.friend_requests.decline(notification)
            stages.append("friend_requests.decline: ok")

            assert not await _are_friends(client, peer_identity.id), (
                "declining a request made the accounts friends"
            )

            # ---- friends.reject (the other manager's spelling) -------------
            notification = await _request_and_wait(
                peer, client, peer_identity, me.username
            )
            if notification is None:
                stages.append(
                    "re-request after a decline: REFUSED or never arrived"
                )
            else:
                await client.friends.reject(
                    getattr(notification, "id", None), peer_identity.id
                )
                stages.append("friends.reject: ok")

            # ---- friend_requests.accept -- back to friends ----------------
            notification = await _request_and_wait(
                peer, client, peer_identity, me.username
            )
            assert notification is not None, (
                "could not get a third request through, so the friendship "
                "cannot be restored by accepting one.\n  " + "\n  ".join(stages)
            )
            await client.friend_requests.accept(notification)
            stages.append("friend_requests.accept: ok")

            async def refriended():
                return await _are_friends(client, peer_identity.id)

            assert await eventually(
                refriended, describe="accepting restores the friendship"
            )
            stages.append("friend_requests.accept restored the friendship: ok")

            # ---- friends.accept -- the last of the seven ------------------
            # One more short cycle, because friends.accept and
            # friend_requests.accept are different managers' spellings and
            # only one of them can be the call that ends the previous loop.
            await client.friends.remove(peer_identity.id)
            assert await eventually(
                unfriended, describe="the second remove drops the friendship"
            )
            notification = await _request_and_wait(
                peer, client, peer_identity, me.username
            )
            assert notification is not None, (
                "no request arrived for the friends.accept stage.\n  "
                + "\n  ".join(stages)
            )
            await client.friends.accept(
                getattr(notification, "id", None), peer_identity.id
            )
            assert await eventually(
                refriended, describe="friends.accept restores the friendship"
            )
            stages.append("friends.accept: ok")
            print("\n   friendship cycle:\n     " + "\n     ".join(stages) + "\n")
        finally:
            # Restoration is not optional and not assumed. If the cycle broke
            # partway the accounts may be unfriended, and the next run's
            # `friendship` fixture would have to fix it -- say so loudly here
            # rather than let it surface as a DM failure later.
            if not await _are_friends(client, peer_identity.id):
                await _restore_friendship(client, peer, me, peer_identity)


class TestAttachedChannelPush:
    """The subscription that makes channel messages arrive at all.

    Membership is not a subscription. Until ``community.attach`` is called the
    hub sends no packet for a community -- which is what made channel push look
    impossible for so long. These tests assert both halves, because only the
    pair is meaningful: a positive with no negative could be a DM arriving by
    another route, and a negative with no positive is indistinguishable from a
    dead socket.

    ``joined_peer`` is the prerequisite: the peer has to be a member before any
    of this says anything.
    """

    async def test_an_unattached_community_pushes_nothing(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        """The negative, first -- it is what everything else is measured from."""
        received = []

        async def on_message(event):
            message = getattr(event, "message", None)
            if message is not None:
                received.append(getattr(message, "content", ""))

        await peer.community.detach(sandbox.community_id)
        peer.add_listener("message", on_message)
        try:
            marker = f"unattached-{tag()}"
            await client.message(shared_channel.id, marker)
            await never(
                lambda: marker in received,
                window=8,
                describe=(
                    "a channel post arriving without an attach -- if this "
                    "starts happening, the subscription model changed"
                ),
            )
        finally:
            peer.remove_listener("message", on_message)

    async def test_attaching_makes_channel_posts_arrive(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        received = []

        async def on_message(event):
            message = getattr(event, "message", None)
            if message is not None:
                received.append(getattr(message, "content", ""))

        peer.add_listener("message", on_message)
        await peer.community.attach(sandbox.community_id)
        try:
            marker = f"attached-{tag()}"
            await client.message(shared_channel.id, marker)
            assert await eventually(
                lambda: marker in received,
                timeout=DELIVERY_TIMEOUT,
                describe="a channel post after attaching",
            )
        finally:
            peer.remove_listener("message", on_message)
            await peer.community.detach(sandbox.community_id)

    async def test_the_push_carries_the_channel_and_community(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        """Ids, not names -- and both, or the reader cannot mark it read."""
        received = []

        async def on_message(event):
            message = getattr(event, "message", None)
            if message is not None:
                received.append(message)

        peer.add_listener("message", on_message)
        await peer.community.attach(sandbox.community_id)
        try:
            marker = f"routed-{tag()}"
            await client.message(shared_channel.id, marker)
            assert await eventually(
                lambda: any(
                    getattr(m, "content", "") == marker for m in received
                ),
                timeout=DELIVERY_TIMEOUT,
                describe="the routed channel post",
            )
            pushed = next(
                m for m in received if getattr(m, "content", "") == marker
            )
            assert _ids_match(pushed.container_id, shared_channel.id)
            assert _ids_match(pushed.community_id, sandbox.community_id)
        finally:
            peer.remove_listener("message", on_message)
            await peer.community.detach(sandbox.community_id)

    async def test_detaching_stops_it_again(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        received = []

        async def on_message(event):
            message = getattr(event, "message", None)
            if message is not None:
                received.append(getattr(message, "content", ""))

        await peer.community.attach(sandbox.community_id)
        await peer.community.detach(sandbox.community_id)
        peer.add_listener("message", on_message)
        try:
            marker = f"detached-{tag()}"
            await client.message(shared_channel.id, marker)
            await never(
                lambda: marker in received,
                window=8,
                describe="a channel post arriving after detaching",
            )
        finally:
            peer.remove_listener("message", on_message)

    async def test_the_unread_reader_reads_it_without_polling(
        self, client, peer, sandbox, joined_peer, shared_channel
    ):
        """The whole point: a post is read and cleared with no sweep at all.

        The request count is the assertion. A polling reader's cost grows with
        time; this one spends its requests subscribing and then only on the
        click itself, so the count between "reader up" and "message handled"
        is bounded by the messages, not by how long it waited.
        """
        from rootpy.unread import UnreadReader

        seen = []

        async def on_message(message):
            seen.append(getattr(message, "content", ""))

        reader = UnreadReader(peer, on_message=on_message)
        async with reader:
            settled = reader.stats.requests
            await never(
                lambda: reader.stats.requests != settled,
                window=3,
                describe=(
                    "the reader making a request while idle -- it should be "
                    "waiting on the socket, not polling"
                ),
            )
            marker = f"reader-{tag()}"
            await client.message(shared_channel.id, marker)
            assert await eventually(
                lambda: marker in seen,
                timeout=DELIVERY_TIMEOUT,
                describe="the reader noticing a channel post",
            )
            assert reader.stats.pushed_messages >= 1
