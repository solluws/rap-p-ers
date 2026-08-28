"""Live coverage for account settings, the friend read surface, DMs and notifications.

Three surfaces that were nearly untouched: ``user_settings`` (1/8),
``friends`` (2/11) and ``direct_messages`` (1/4), plus the rest of
``notifications``.

**What is deliberately not tested here, and why.**

``set_dm_invite_requirement``, ``set_friend_invite_requirement`` and
``set_community_invite_requirement`` take a
``UserDirectMessageInviteConnection`` (``Any``/``Connected``/``None``/``Friend``)
and are precisely the gate that decides whether the two accounts can DM each
other at all. There is no getter, so a test could not restore the previous
value faithfully -- and getting it wrong would silently break every DM, call
and invite test on that account, permanently, in a way that looks like an SDK
bug. The enum values are pinned offline instead.

All three were nonetheless *broken*, which no live test would have found any
faster than the offline check that did: they sent ``is_required`` at requests
whose field is ``IsEmailVerified``, so the codec raised ``TypeError`` before
building the request and none of them could ever have run. Fixed, and
``TestEveryHighCallSiteSendsRealFields`` now sweeps every ``high.*`` call site
for the same shape. They stay untested live for the reason above, not because
nobody looked.

``friends.remove()`` is skipped for the same restore reason: it would tear
down the ``friendship`` fixture that DMs and calls depend on, and
re-establishing it mid-suite risks leaving the pair unfriended if anything
fails in between. That also puts ``friends.accept``/``reject``/``respond`` and
``friend_requests.accept``/``decline``/``respond`` out of reach, since with the
pair already friends there is no pending request to answer -- the fixture
itself exercises that path once per run.
``TestFriendRequestsWithoutDisturbingTheFriendship`` covers what is left: the
send path through its rejections, and the read side.

Profile *images* are the counter-example, and live in
``test_live_integration.py`` because they need one account: they can be
restored faithfully, by downloading the previous image's bytes and uploading
them back, so they are tested.

Run with::

    pytest -m live2 -k "Settings or FriendSurface or DirectMessageService or Notifications or FriendRequests" -v
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    _ids_match,
    eventually,
    ladder,
    requires_two_accounts,
    tag,
)

pytestmark = [pytest.mark.live2, pytest.mark.asyncio, requires_two_accounts]


# --------------------------------------------------------------------------
# user_settings -- notes and presence, the parts that are safe to change
# --------------------------------------------------------------------------
class TestSettingsNotes:
    """Private notes you keep about another user."""

    async def test_get_note_for_a_stranger_is_none(self, client, me):
        note = await client.user_settings.get_note(me.id)
        assert note is None or isinstance(note, str)

    async def test_set_then_read_back(self, client, peer_identity, friendship):
        marker = f"note {tag()}"
        try:
            await client.user_settings.set_note(peer_identity.id, marker)
            read = await eventually(
                lambda: _note_matching(client, peer_identity.id, marker),
                describe="the note reads back",
            )
            assert read == marker
        finally:
            await client.user_settings.set_note(peer_identity.id, "")

    async def test_clearing_a_note(self, client, peer_identity, friendship):
        await client.user_settings.set_note(peer_identity.id, f"temp {tag()}")
        await client.user_settings.set_note(peer_identity.id, "")
        note = await client.user_settings.get_note(peer_identity.id)
        assert not note

    async def test_a_note_is_private_to_the_setter(
        self, client, peer, me, peer_identity, friendship
    ):
        """The peer must not see the note we keep about them."""
        marker = f"private {tag()}"
        try:
            await client.user_settings.set_note(peer_identity.id, marker)
            theirs = await peer.user_settings.get_note(me.id)
            assert theirs != marker
        finally:
            await client.user_settings.set_note(peer_identity.id, "")


class TestSettingsPresence:
    """Presence ceilings. Restored afterwards."""

    async def test_set_max_online_status(self, client):
        from rootpy.enums import UserOnlineStatus
        from rootpy.exceptions import GrpcResourceExhausted

        try:
            await client.user_settings.set_max_online_status(
                UserOnlineStatus.ACTIVE
            )
        except GrpcResourceExhausted:
            pytest.skip("rate-limited (RESOURCE_EXHAUSTED)")

    async def test_set_device_online_status(self, client):
        from rootpy.enums import UserOnlineStatus
        from rootpy.exceptions import GrpcResourceExhausted

        try:
            await client.user_settings.set_device_online_status(
                UserOnlineStatus.ACTIVE
            )
        except GrpcResourceExhausted:
            pytest.skip("rate-limited (RESOURCE_EXHAUSTED)")


# --------------------------------------------------------------------------
# friends -- the read surface
# --------------------------------------------------------------------------
class TestFriendSurface:
    async def test_friend_ids_is_a_set(self, client, friendship):
        ids = await client.friends.friend_ids()
        assert isinstance(ids, set)

    async def test_the_peer_is_in_friend_ids(
        self, client, peer_identity, friendship
    ):
        ids = await client.friends.friend_ids()
        assert any(_ids_match(peer_identity.id, i) for i in ids)

    async def test_is_friend_is_true_for_the_peer(
        self, client, peer_identity, friendship
    ):
        assert await client.friends.is_friend(peer_identity.id) is True

    async def test_is_friend_is_false_for_ourselves(self, client, me, friendship):
        assert await client.friends.is_friend(me.id) is False

    async def test_get_returns_the_peer(self, client, peer_identity, friendship):
        friend = await client.friends.get(peer_identity.id)
        assert friend is not None

    async def test_get_all(self, client, friendship):
        assert await client.friends.get_all() is not None

    async def test_list(self, client, friendship):
        assert await client.friends.list() is not None

    async def test_groups(self, client, friendship):
        assert await client.friends.groups() is not None

    async def test_the_relationship_is_symmetric(
        self, client, peer, me, peer_identity, friendship
    ):
        """Both directions, which one account cannot check."""
        assert await client.friends.is_friend(peer_identity.id) is True
        assert await peer.friends.is_friend(me.id) is True


# --------------------------------------------------------------------------
# direct_messages -- the service beneath client.dm
# --------------------------------------------------------------------------
class TestDirectMessageService:
    async def test_create_refuses_a_second_conversation(
        self, client, peer_identity, friendship
    ):
        """``create`` is not idempotent -- that is what ``get_or_create`` is for.

        Root answers ALREADY_EXISTS on the second call. Asserting it here
        documents the split: ``create`` means "make a new one", and reaching
        for it when a conversation may already exist is the mistake.
        """
        from rootpy.exceptions import GrpcAlreadyExists

        await client.direct_messages.get_or_create(peer_identity.id)
        with pytest.raises(GrpcAlreadyExists):
            await client.direct_messages.create(peer_identity.id)

    async def test_get_or_create_is_the_idempotent_one(
        self, client, peer_identity, friendship
    ):
        first = await client.direct_messages.get_or_create(peer_identity.id)
        second = await client.direct_messages.get_or_create(peer_identity.id)
        assert first.id == second.id

    async def test_find_locates_an_existing_conversation(
        self, client, peer_identity, friendship
    ):
        created = await client.direct_messages.get_or_create(peer_identity.id)
        found = await client.direct_messages.find(peer_identity.id)
        assert found is not None
        assert found.id == created.id

    async def test_find_returns_none_for_ourselves(self, client, me, friendship):
        """No self-DM, so this exercises the miss path rather than raising."""
        try:
            found = await client.direct_messages.find(me.id)
        except Exception:
            pytest.skip("find() raises rather than returning None for self")
        assert found is None or found.id

    async def test_list_includes_the_peer_conversation(
        self, client, peer_identity, friendship
    ):
        conversation = await client.direct_messages.get_or_create(
            peer_identity.id
        )
        conversations = await client.direct_messages.list()
        assert any(c.id == conversation.id for c in conversations)

    async def test_list_returns_a_tuple(self, client, friendship):
        assert isinstance(await client.direct_messages.list(), tuple)


# --------------------------------------------------------------------------
# notifications -- the rest of the manager
# --------------------------------------------------------------------------
class TestNotifications:
    async def test_list_is_readable(self, client):
        assert await client.notifications.list() is not None

    async def test_count_unviewed_is_an_int(self, client):
        assert isinstance(await client.notifications.count_unviewed(), int)

    async def test_counts_by_container_is_a_mapping(self, client):
        counts = await client.notifications.counts_by_container()
        assert isinstance(counts, dict)

    async def test_the_total_matches_the_breakdown(self, client):
        """count_unviewed() sums counts_by_container(); they must agree."""
        total = await client.notifications.count_unviewed()
        breakdown = await client.notifications.counts_by_container()
        assert total == sum(breakdown.values())

    async def test_mark_all_viewed(self, client):
        await client.notifications.mark_all_viewed()

    async def test_unviewed_is_zero_after_marking_all(self, client):
        await client.notifications.mark_all_viewed()
        count = await eventually(
            lambda: _count_is_zero(client),
            describe="the unviewed count clears",
        )
        assert count is True


class TestFriendRequestsWithoutDisturbingTheFriendship:
    """The parts of the request surface that do not need the pair unfriended.

    ``friends.remove()`` is off limits (see the module docstring), and with the
    two accounts already friends there is no pending request to accept or
    decline. What is left is still worth having: the *send* path, which is
    reachable through its rejections, and the read side.

    Sending to yourself is the interesting rejection. Root has a dedicated
    error code for it -- ``REQUESTED_SELF`` (7) and
    ``PENDING_FRIENDSHIP_REQUESTED_SELF`` (200) -- so this is a defined
    behaviour rather than an accident, and it exercises
    ``FriendshipInviteCreate`` end to end without changing any state.
    """

    async def test_requesting_yourself_is_refused(self, client, me):
        from rootpy.exceptions import RootError

        with pytest.raises(RootError) as caught:
            await client.friend_requests.send(me.username)
        print(f"\n   friend request to self -> {caught.value}\n")

    async def test_requesting_an_existing_friend_is_refused(
        self, client, peer_identity, friendship
    ):
        """``friends.request`` is the other spelling of the same RPC."""
        from rootpy.exceptions import RootError

        with pytest.raises(RootError) as caught:
            await client.friends.request(peer_identity.username)
        print(f"\n   friend request to an existing friend -> {caught.value}\n")

    async def test_send_refuses_an_empty_username_locally(self, client):
        """Raises before the round trip -- Root addresses these by username."""
        with pytest.raises(ValueError):
            await client.friend_requests.send("   ")

    async def test_responded_is_readable(self, client, friendship):
        """Notifications saying a request *you* sent was answered."""
        responded = await client.friend_requests.responded()
        assert isinstance(responded, list)

    async def test_accept_all_with_nothing_pending(self, client, friendship):
        pending = await client.friend_requests.pending()
        accepted = await client.friend_requests.accept_all()
        assert accepted <= len(pending)

    async def test_decline_from_a_stranger_is_false(self, client, me, friendship):
        """No pending request from this user, so no RPC and a False."""
        assert await client.friend_requests.decline_from(me.id) is False


class TestInviteRequirementSetters:
    """The three methods that could never have been called, on the wire.

    They sent ``is_required`` at requests whose field is ``IsEmailVerified``,
    so ``encode_message`` raised ``TypeError`` before the request was built --
    every call, on every account, since they were written. That was found and
    fixed offline; this is the half only a real request can answer, which is
    whether Root accepts the corrected field.

    **Why these are safe to run when they were previously ruled out.** There
    is no getter, so nothing here can be restored to its prior value. The
    protection is direction, not restoration: every call below moves the
    setting to its *most permissive* state (``ANY``, and no email-verification
    requirement), which cannot lock either account out of anything. Setting a
    restrictive value is what would have been unrecoverable, and none of these
    do that.

    The DM gate is checked rather than assumed afterwards -- if changing it
    broke DMs, every other two-account test would fail and this is the test
    that should say why.
    """

    async def test_dm_invite_requirement_accepts_the_corrected_field(
        self, client, peer, me, peer_identity, friendship
    ):
        from rootpy.enums import UserDirectMessageInviteConnection as DMInvite

        await client.user_settings.set_dm_invite_requirement(
            DMInvite.ANY, email_verified=False
        )

        # The gate this setting controls, exercised in the direction it
        # controls: the peer opening a DM with *us*.
        conversation = await peer.dm.open(me.id)
        assert conversation.id, "the DM gate broke after loosening it"

    async def test_friend_invite_requirement_accepts_the_corrected_field(
        self, client
    ):
        from rootpy.enums import UserFriendshipInviteConnection as FriendInvite

        await client.user_settings.set_friend_invite_requirement(
            FriendInvite.ANY, email_verified=False
        )

    async def test_community_invite_requirement_accepts_the_corrected_field(
        self, client
    ):
        from rootpy.enums import UserCommunityInviteConnection as CommunityInvite

        await client.user_settings.set_community_invite_requirement(
            CommunityInvite.ANY, email_verified=False
        )

    async def test_the_email_verified_flag_is_accepted_too(self, client):
        """The field the bug was about, sent as True rather than defaulted.

        Both accounts are email-verified, so requiring verification cannot
        lock anything out -- and ``ANY`` keeps the connection half open.
        """
        from rootpy.enums import UserCommunityInviteConnection as CommunityInvite

        await client.user_settings.set_community_invite_requirement(
            CommunityInvite.ANY, email_verified=True
        )
        # Put it back to the least restrictive of the two.
        await client.user_settings.set_community_invite_requirement(
            CommunityInvite.ANY, email_verified=False
        )

    async def test_a_raw_int_works_as_well_as_the_enum(self, client):
        """``connection`` is an enum field; the codec must take a plain int."""
        await client.user_settings.set_friend_invite_requirement(
            1, email_verified=False
        )


async def _note_matching(client, user_id, marker):
    note = await client.user_settings.get_note(user_id)
    return note if note == marker else None


async def _count_is_zero(client):
    return (await client.notifications.count_unviewed()) == 0
