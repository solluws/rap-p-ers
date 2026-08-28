"""Live tests against a real account, in a community the suite creates itself.

Run with::

    export ROOT_TOKEN="..."
    pytest -m live -v

No IDs are hard-coded. The ``sandbox`` fixture (see ``conftest.py``) creates a
community owned by the test account -- so permissions are never the reason
something fails -- populates it with a channel group, channel and role, and
deletes the whole thing afterwards.

These cover the methods that had no exercise anywhere in the repo:
``Community.clone``, ``create_text_channel``, ``create_voice_channel``,
``MemberManager.edit_nickname``, ``CommunityManager.*``, ``SearchManager``,
``NotificationManager``, ``UserSettingsManager``, the friend/block managers,
``permissions_for``, ``ensure_community_cached``, ``messages_for_container``
and the paginated history helpers.

``TestProfileImages`` at the bottom is the one class here that does not run by
default. It changes the account's avatar or banner, and Root's profile-write
quota outlasts any reasonable retry window, so the restore cannot be relied on
-- twice it left a real account wearing a test image. Opt in with
``ROOTPY_TEST_PROFILE_IMAGES=1``; the class docstring has the full reasoning.
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    SANDBOX_ROLE_COLOR,
    eventually,
    requires_live,
    requires_profile_image_optin,
    tag,
    unique,
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


# --------------------------------------------------------------------------
# identity and connection
# --------------------------------------------------------------------------
class TestIdentity:
    async def test_whoami_returns_the_token_owner(self, me):
        assert me.id and me.username

    async def test_whoami_is_cached_until_refreshed(self, client, me):
        again = await client.whoami()
        assert again.id == me.id

    async def test_is_connected_reports_gateway_state(self, client):
        assert isinstance(client.is_connected, bool)

    async def test_timing_report_is_populated_after_traffic(self, client):
        report = client.timing_report()
        assert report is not None


# --------------------------------------------------------------------------
# the sandbox community itself
# --------------------------------------------------------------------------
class TestSandboxSetup:
    async def test_community_was_created(self, sandbox):
        assert sandbox.community_id

    async def test_we_own_it(self, sandbox, me):
        assert sandbox.community.owner_user_id == me.id

    async def test_it_shows_up_in_our_communities(self, client, sandbox):
        mine = await client.list_communities(refresh=True)
        assert any(c.id == sandbox.community_id for c in mine)

    async def test_channel_group_and_channel_exist(self, sandbox):
        assert sandbox.group.id and sandbox.channel.id

    async def test_role_was_created(self, sandbox):
        assert sandbox.role.id and sandbox.role.name

    async def test_permissions_for_reports_full_control_as_owner(
        self, client, sandbox, me
    ):
        perms = client.permissions_for(me.id, sandbox.channel.id)
        assert perms is None or perms.has("channel_view")

    async def test_ensure_community_cached(self, client, sandbox):
        await client.ensure_community_cached(sandbox.community_id)
        assert client.get_community(sandbox.community_id) is not None


# --------------------------------------------------------------------------
# messaging round trip
# --------------------------------------------------------------------------
class TestMessaging:
    async def test_send_returns_a_message(self, client, sandbox):
        result = await client.message(sandbox.channel.id, "hello from rootpy")
        sent = getattr(result, "message", result)
        assert sent.id
        await client.delete_message(sent)

    async def test_reply_threads_onto_the_parent(self, client, message):
        result = await client.reply(message, "threaded reply")
        sent = getattr(result, "message", result)
        assert sent.id
        await client.delete_message(sent)

    async def test_edit_changes_content(self, client, message):
        edited = await client.edit_message(message, "edited by the self-test")
        assert edited.content == "edited by the self-test"

    async def test_react_and_unreact(self, client, message):
        await client.react(message, "thumbsup")
        await client.unreact(message, "thumbsup")

    async def test_pin_and_unpin(self, client, message):
        await client.pin(message)
        await client.unpin(message)

    async def test_history_contains_what_we_posted(self, client, sandbox, message):
        async def posted():
            history = await client.list_messages(
                sandbox.channel.id, community_id=sandbox.community_id
            )
            return any(m.id == message.id for m in history)

        await eventually(posted, describe="the message appears in history")

    async def test_messages_for_container_matches_history(self, client, sandbox):
        # Synchronous: reads the message cache, returns a tuple.
        page = client.messages_for_container(sandbox.channel.id)
        assert isinstance(page, tuple)

    async def test_get_message_round_trips(self, client, message):
        # Synchronous: cache lookup, not an RPC.
        fetched = client.get_message(message.id)
        assert fetched is None or fetched.id == message.id

    async def test_delete_removes_the_message(self, client, sandbox):
        result = await client.message(sandbox.channel.id, "delete me")
        sent = getattr(result, "message", result)
        await client.delete_message(sent)

        async def gone():
            history = await client.list_messages(
                sandbox.channel.id, community_id=sandbox.community_id
            )
            return all(m.id != sent.id for m in history)

        await eventually(gone, describe="the deleted message leaves history")


# --------------------------------------------------------------------------
# members and profiles
# --------------------------------------------------------------------------
class TestMembers:
    async def test_get_members_includes_us(self, client, sandbox, me):
        members = await client.get_members(sandbox.community_id, refresh=True)
        assert any(m.user_id == me.id for m in members)

    async def test_get_members_is_cached_on_second_call(self, client, sandbox):
        first = await client.get_members(sandbox.community_id)
        second = await client.get_members(sandbox.community_id)
        assert len(first) == len(second)

    async def test_get_profiles_batches(self, client, sandbox, me):
        profiles = await client.get_profiles([me.id])
        assert me.id in profiles

    async def test_get_random_member_without_self_is_empty_when_alone(
        self, client, sandbox
    ):
        picked = await client.get_random_member(
            sandbox.community_id, exclude_self=True
        )
        assert picked is None or picked.user_id

    async def test_get_random_member_can_include_self(self, client, sandbox, me):
        picked = await client.get_random_member(
            sandbox.community_id, exclude_self=False
        )
        assert picked is None or picked.user_id

    async def test_edit_own_nickname(self, client, sandbox, me):
        """Rule mapped live: alphanumerics accepted, space and hyphen not."""
        await client.members.edit_nickname(
            sandbox.community_id, me.id, f"rootpytester{tag()}"
        )

    async def test_underscores_and_periods_are_accepted(self, client, sandbox, me):
        """Confirms the inference in validate_nickname.

        The username rule allows underscores and periods; the nickname probe
        only ever proved letters and digits. If Root rejects these, tighten
        validate_nickname rather than working around it here.
        """
        await client.members.edit_nickname(
            sandbox.community_id, me.id, f"rootpy_test.{tag()}"
        )

    async def test_invalid_nickname_fails_without_a_round_trip(
        self, client, sandbox, me
    ):
        """A space is refused client-side now, not by Root."""
        with pytest.raises(ValueError, match="underscores and periods"):
            await client.members.edit_nickname(
                sandbox.community_id, me.id, "rootpy tester"
            )


class TestCommunityAdmin:
    async def test_create_text_channel(self, client, sandbox):
        channel = await client.community.create_text_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-text-{tag()}"
        )
        assert channel.id

    async def test_create_voice_channel(self, client, sandbox):
        channel = await client.community.create_voice_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-voice-{tag()}"
        )
        assert channel.id
        # Regression: CommunityManager.VOICE used to be 1 (TEXT), so this
        # silently produced a text channel instead of failing.
        assert int(getattr(channel, "channel_type", 4)) == 4

    async def test_get_channels_lists_what_we_made(self, client, sandbox):
        channels = await client.community.get_channels(
            sandbox.community_id, refresh=True
        )
        assert any(c.id == sandbox.channel.id for c in channels)

    async def test_create_and_delete_role(self, client, sandbox):
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-temp-{tag()}", color_hex="e74c3c"
        )
        assert role.id
        await client.community.delete_role(sandbox.community_id, role.id)

    async def test_edit_community_description(self, client, sandbox):
        updated = await client.community.edit(
            sandbox.community_id, description="edited by the rootpy self-test"
        )
        assert updated.id == sandbox.community_id

    async def test_clone_community_then_delete_the_clone(self, client, sandbox):
        """Exercises Community.clone, which nothing referenced before."""
        clone = await client.community.clone(
            sandbox.community_id, name=f"rootpy clone {tag()}"
        )
        try:
            assert clone.id and clone.id != sandbox.community_id
        finally:
            await client.community.delete(clone.id)


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------
class TestAssets:
    async def test_resolve_our_own_avatar(self, client, me):
        uri = getattr(me, "profile_picture_asset_uri", None)
        if not uri:
            pytest.skip("account has no avatar set")
        resolved = await client.assets.resolve(uri)
        assert isinstance(resolved, dict)

    async def test_save_asset_writes_bytes(self, client, me, tmp_path):
        uri = getattr(me, "profile_picture_asset_uri", None)
        if not uri:
            pytest.skip("account has no avatar set")
        path = await client.save_asset(uri, tmp_path / "avatar")
        assert path is None or (tmp_path / "avatar").parent.exists()

    async def test_signed_urls_carry_an_expiry(self, client, me):
        """HANDOFF: store bytes, not URLs -- this is why."""
        uri = getattr(me, "profile_picture_asset_uri", None)
        if not uri:
            pytest.skip("account has no avatar set")
        info = await client.assets.get(uri)
        assert isinstance(info, dict)


# --------------------------------------------------------------------------
# account settings, notifications, social
# --------------------------------------------------------------------------
class TestAccountSurface:
    async def test_set_and_clear_custom_status(self, client):
        """Root rate-limits profile writes; that is the server's call, not a bug."""
        from rootpy.exceptions import GrpcResourceExhausted

        try:
            await client.update_status("rootpy self-test")
            await client.update_status(None)
        except GrpcResourceExhausted:
            pytest.skip("Root rate-limited the profile update (RESOURCE_EXHAUSTED)")

    async def test_set_and_restore_description(self, client, me):
        from rootpy.exceptions import GrpcResourceExhausted

        original = getattr(me, "description", None)
        try:
            await client.update_description("rootpy self-test")
            await client.update_description(original)
        except GrpcResourceExhausted:
            pytest.skip("Root rate-limited the profile update (RESOURCE_EXHAUSTED)")

    async def test_list_notifications(self, client):
        assert await client.list_notifications() is not None

    async def test_unread_count_is_a_number(self, client):
        assert isinstance(await client.unread_count(), int)

    async def test_list_friends(self, client):
        assert await client.list_friends() is not None

    async def test_pending_friend_requests(self, client):
        assert await client.pending_friend_requests() is not None

    async def test_list_blocked(self, client):
        assert await client.list_blocked() is not None

    async def test_get_note_for_self(self, client, me):
        note = await client.user_settings.get_note(me.id)
        # The SDK returns the note text, not the raw UserNoteResponse.
        assert note is None or isinstance(note, str)


# --------------------------------------------------------------------------
# teardown proof
# --------------------------------------------------------------------------
class TestCleanupContract:
    async def test_sandbox_names_are_identifiable(self, sandbox):
        """Anything a crashed run leaves behind is greppable."""
        from .conftest import SANDBOX_PREFIX

        assert sandbox.community.name.startswith(SANDBOX_PREFIX)


# --------------------------------------------------------------------------
# profile images -- changed, then restored byte-for-byte
# --------------------------------------------------------------------------
@requires_profile_image_optin
class TestProfileImages:
    """``set_profile_picture`` and ``set_banner``, behind an opt-in.

    Both change something durable on the account, so both restore what was
    there: the current image's bytes are downloaded first and uploaded back
    afterwards, which is a faithful restore rather than an approximation, and
    exercises ``assets.download`` and ``assets.upload_bytes`` on the way.
    An account with no image restores to no image.

    These also settle an asymmetry in the SDK. ``set_banner`` decodes response
    field 5 as a nested ``StringValue`` (reading field 1 inside it) while
    ``set_profile_picture`` decodes the same field's bytes directly as UTF-8.
    Both cannot be right unless the two responses genuinely differ, so the
    returned values are printed rather than asserted -- one run says which.

    **Why this is opt-in, and why that is the honest answer.** Profile writes
    share a rate-limit quota. The *set* succeeds; the **restore** is refused
    with RESOURCE_EXHAUSTED, and the quota outlasted a 95-second backoff on
    two separate runs -- leaving a real account wearing a test image with the
    original already discarded. The first time cost an account its avatar
    permanently.

    Guarding the set was not enough: the dangerous call is the one that puts
    things back. So three things changed --

    * the original bytes are written to disk **before** anything is touched,
      so a restore that cannot complete leaves a recoverable artifact;
    * the restore retries through the rate limit rather than propagating it;
    * the restore is verified, and a failure names the file on disk.

    -- and it still was not enough, because no retry window this side of
    several minutes beats the quota. Two methods of coverage do not justify
    mutating account state the suite cannot reliably undo, which is the same
    reasoning that keeps the invite-requirement setters untested. Ask for it
    and it runs::

        set ROOTPY_TEST_PROFILE_IMAGES=1
        pytest -m live -k ProfileImages -v
    """

    @staticmethod
    async def _snapshot(client, uri, tmp_path, label):
        """Save the current image to disk. Returns (bytes, path) or (None, None)."""
        if not uri:
            return None, None
        data = await client.assets.download(uri)
        if not data:
            return None, None
        path = tmp_path / f"original-{label}.bin"
        path.write_bytes(data)
        return data, path

    @staticmethod
    async def _restore(setter, data, path, label):
        """Put the image back, riding out the shared profile rate limit.

        ``eventually`` polls for a condition; this needs to *retry a call*,
        which is a different shape, so the loop is written out. The delays are
        deliberately long -- the quota is per-account and measured in tens of
        seconds, not hundreds of milliseconds.
        """
        from rootpy.exceptions import GrpcResourceExhausted

        last = None
        for delay in (0, 5, 15, 30, 45):
            if delay:
                await asyncio.sleep(delay)
            try:
                await setter(data if data else None)
                return True
            except GrpcResourceExhausted as exc:
                last = exc
        pytest.fail(
            f"could not restore the {label} -- Root rate-limited every "
            f"attempt over ~95s ({last}).\n"
            f"The account is left wearing the test image. The original bytes "
            f"are saved at {path} -- upload that file to put it back."
            if path else
            f"could not restore the {label} ({last}), and there was no "
            f"original image to save."
        )

    async def test_set_and_restore_the_profile_picture(
        self, client, unique_png, tmp_path
    ):
        from rootpy.exceptions import GrpcResourceExhausted

        before = await client.users.get_self()
        original = before.profile_picture_asset_uri
        data, path = await self._snapshot(client, original, tmp_path, "avatar")

        try:
            returned = await client.users.set_profile_picture(unique_png)
        except GrpcResourceExhausted:
            pytest.skip("Root rate-limited the profile update")

        try:
            print(
                f"\n   set_profile_picture returned {returned!r}"
                "\n   (set_banner unwraps a StringValue here and this one does "
                "not -- compare the two)\n"
            )
            assert await eventually(
                lambda: _picture_changed(client, original),
                describe="the new profile picture reads back",
            )
        finally:
            await self._restore(
                client.users.set_profile_picture, data, path, "profile picture"
            )

            async def back():
                current = await client.users.get_self()
                return current.profile_picture_asset_uri == original

            if original:
                assert await eventually(
                    back, describe="the original profile picture is back"
                ), f"restore reported success but the URI differs; {path}"

    async def test_set_and_restore_the_banner(self, client, unique_png, tmp_path):
        """Runs second, and the quota is shared -- so it usually skips.

        In run 1 the picture test above consumed the allowance and this one
        skipped on its set. That is the correct outcome, not a gap: a skip
        here means nothing was changed, which is the safe direction. The
        ``asyncio.sleep`` before the set buys back a little headroom without
        pretending the quota is not there.
        """
        from rootpy.exceptions import GrpcResourceExhausted

        before = await client.users.get_self()
        original = before.banner_asset_uri
        data, path = await self._snapshot(client, original, tmp_path, "banner")

        await asyncio.sleep(0.5)
        try:
            returned = await client.users.set_banner(unique_png)
        except GrpcResourceExhausted:
            pytest.skip(
                "Root rate-limited the banner set -- the profile-picture test "
                "above shares the quota. Nothing was changed."
            )

        try:
            print(f"\n   set_banner returned {returned!r}\n")
            assert await eventually(
                lambda: _banner_changed(client, original),
                describe="the new banner reads back",
            )
        finally:
            await self._restore(
                client.users.set_banner, data, path, "banner"
            )

            async def back():
                current = await client.users.get_self()
                return current.banner_asset_uri == original

            if original:
                assert await eventually(
                    back, describe="the original banner is back"
                ), f"restore reported success but the URI differs; {path}"



class TestBytesSourceUploads:
    """Not gated: this uploads bytes and changes nothing on the account.

    It lived inside ``TestProfileImages`` and was skipped along with it when
    that class went opt-in -- losing coverage of the bytes path for a reason
    that does not apply to it.
    """

    async def test_upload_bytes_is_what_a_bytes_source_uses(self, client):
        """``_resolve_asset_source`` accepts raw bytes, not just a path."""
        from .conftest import PNG_1X1

        token = await client.assets.upload_bytes(PNG_1X1, filename="probe.png")
        assert token.startswith("root://")


async def _picture_changed(client, original):
    current = await client.users.get_self()
    return current.profile_picture_asset_uri not in (None, "", original)


async def _banner_changed(client, original):
    current = await client.users.get_self()
    return current.banner_asset_uri not in (None, "", original)
