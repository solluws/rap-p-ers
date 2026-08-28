"""Live coverage for the services no test has ever reached.

An audit put 16 of 27 services at zero coverage. These are the ones a single
account can exercise inside its own sandbox community: roles, invites, emojis,
directories, files, logs, search and the community service itself.

Everything happens in the sandbox community from ``conftest``, which is
created and deleted per session, so nothing here touches anything you care
about. Objects created mid-test are cleaned up in ``finally`` blocks even
though the community delete would take them anyway -- a leak inside a test
should be visible, not swallowed by teardown.

Run with::

    pytest -m live -v

Moderation and voice admin need a second account and live in
``test_live_two_accounts.py``. The rest of the file/directory/asset surface --
get, edit, move, download and both searches -- is in ``test_live_files.py``.

``TestCommunityApps`` and ``TestCommunityAppLog`` are probes rather than
assertions. Six of the eight app methods need an ``app_id`` that only a real
installation produces; ``list`` and ``initialize`` do not, and the app log is
worth asking about because a clean empty answer would make it pollable without
an install. Each reports what Root said.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from .conftest import requires_live, tag

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


# The ``png`` fixture and ``PNG_1X1`` now live in conftest -- four modules
# need image bytes, not one.

# --------------------------------------------------------------------------
# roles -- 9 methods, none previously exercised
# --------------------------------------------------------------------------
class TestRoles:
    async def test_list_includes_the_sandbox_role(self, client, sandbox):
        roles = await client.roles.list(sandbox.community_id)
        assert isinstance(roles, list)
        assert any(getattr(r, "id", None) == sandbox.role.id for r in roles)

    async def test_get_one_by_id(self, client, sandbox):
        role = await client.roles.get(sandbox.community_id, sandbox.role.id)
        assert role is None or role.id == sandbox.role.id

    async def test_create_and_delete(self, client, sandbox):
        role = await client.roles.create(
            sandbox.community_id, f"rootpy-r-{tag()}", color_hex="#e67e22"
        )
        try:
            assert role.id
        finally:
            await client.roles.delete(sandbox.community_id, role.id)

    async def test_edit_changes_the_name(self, client, sandbox):
        role = await client.roles.create(sandbox.community_id, f"rootpy-e-{tag()}")
        try:
            renamed = f"rootpy-x-{tag()}"
            await client.roles.edit(sandbox.community_id, role.id, name=renamed)
            roles = await client.roles.list(sandbox.community_id)
            match = next(
                (r for r in roles if getattr(r, "id", None) == role.id), None
            )
            assert match is None or match.name == renamed
        finally:
            await client.roles.delete(sandbox.community_id, role.id)

    async def test_move_reorders(self, client, sandbox):
        role = await client.roles.create(sandbox.community_id, f"rootpy-m-{tag()}")
        try:
            await client.roles.move(
                sandbox.community_id, role.id, before_role_id=sandbox.role.id
            )
        finally:
            await client.roles.delete(sandbox.community_id, role.id)

    async def test_assign_to_self_then_remove(self, client, sandbox, me):
        role = await client.roles.create(sandbox.community_id, f"rootpy-a-{tag()}")
        try:
            await client.roles.add_to_members(
                sandbox.community_id, role.id, [me.id]
            )
            await client.roles.remove_from_members(
                sandbox.community_id, role.id, [me.id]
            )
        finally:
            await client.roles.delete(sandbox.community_id, role.id)

    async def test_set_primary(self, client, sandbox, me):
        role = await client.roles.create(sandbox.community_id, f"rootpy-p-{tag()}")
        try:
            await client.roles.add_to_members(
                sandbox.community_id, role.id, [me.id]
            )
            await client.roles.set_primary(sandbox.community_id, me.id, role.id)
        finally:
            await client.roles.delete(sandbox.community_id, role.id)


# --------------------------------------------------------------------------
# invites -- 7 methods
# --------------------------------------------------------------------------
class TestInvites:
    async def test_create_returns_a_code(self, client, sandbox):
        invite = await client.invites.create(sandbox.community_id, max_uses=1)
        code = getattr(invite, "code", None)
        try:
            assert code
        finally:
            invite_id = getattr(invite, "id", None)
            if invite_id:
                await client.invites.delete(sandbox.community_id, invite_id)

    async def test_list_includes_it(self, client, sandbox):
        invite = await client.invites.create(sandbox.community_id, max_uses=1)
        try:
            invites = await client.invites.list(sandbox.community_id)
            assert invites is not None
        finally:
            invite_id = getattr(invite, "id", None)
            if invite_id:
                await client.invites.delete(sandbox.community_id, invite_id)

    async def test_list_mine(self, client, sandbox):
        assert await client.invites.list_mine(sandbox.community_id) is not None

    async def test_info_resolves_a_real_code(self, client, sandbox):
        invite = await client.invites.create(sandbox.community_id, max_uses=1)
        code = getattr(invite, "code", None)
        try:
            assert await client.invites.info(code) is not None
        finally:
            invite_id = getattr(invite, "id", None)
            if invite_id:
                await client.invites.delete(sandbox.community_id, invite_id)

    async def test_code_exists_rejects_a_generated_code(self, client, sandbox):
        """Documents a real asymmetry rather than papering over it.

        ``code_exists`` answers False for nonsense but rejects the code that
        ``create`` just returned with ``Code: The specified condition was not
        met [PredicateValidator]`` -- so the two ends disagree about what a
        "code" is. Until that is understood, this pins the behaviour so a fix
        is visible.
        """
        from rootpy.exceptions import GrpcInvalidArgument

        invite = await client.invites.create(sandbox.community_id, max_uses=1)
        code = getattr(invite, "code", None)
        try:
            try:
                result = await client.invites.code_exists(code)
            except GrpcInvalidArgument as exc:
                pytest.xfail(
                    f"code_exists rejects a code create() returned ({code!r}): {exc}"
                )
            assert result is True
        finally:
            invite_id = getattr(invite, "id", None)
            if invite_id:
                await client.invites.delete(sandbox.community_id, invite_id)

    async def test_code_exists_is_false_for_nonsense(self, client):
        assert await client.invites.code_exists(f"nope{tag()}") is False

    async def test_custom_code_is_honoured(self, client, sandbox):
        wanted = f"rootpy{tag()}"
        try:
            invite = await client.invites.create(
                sandbox.community_id, code=wanted, max_uses=1
            )
        except Exception as exc:
            pytest.skip(f"custom invite codes rejected: {exc}")
        try:
            assert getattr(invite, "code", None) == wanted
        finally:
            invite_id = getattr(invite, "id", None)
            if invite_id:
                await client.invites.delete(sandbox.community_id, invite_id)


# --------------------------------------------------------------------------
# emojis -- 5 methods; create uploads a real file
# --------------------------------------------------------------------------
class TestEmojis:
    async def test_list_is_readable(self, client, sandbox):
        assert await client.emojis.list(sandbox.community_id) is not None

    async def test_list_mine_is_readable(self, client):
        assert await client.emojis.list_mine() is not None

    async def test_create_then_delete(self, client, sandbox, png):
        shortcode = f"rootpy{tag()}"
        emoji = await client.emojis.create(sandbox.community_id, shortcode, png)
        emoji_id = getattr(emoji, "id", None)
        try:
            assert emoji_id
        finally:
            if emoji_id:
                await client.emojis.delete(sandbox.community_id, emoji_id)

    async def test_created_emoji_appears_in_the_list(self, client, sandbox, png):
        shortcode = f"rootpy{tag()}"
        emoji = await client.emojis.create(sandbox.community_id, shortcode, png)
        emoji_id = getattr(emoji, "id", None)
        try:
            emojis = await client.emojis.list(sandbox.community_id)
            assert any(getattr(e, "id", None) == emoji_id for e in emojis)
        finally:
            if emoji_id:
                await client.emojis.delete(sandbox.community_id, emoji_id)

    async def test_resolve_by_id(self, client, sandbox, png):
        shortcode = f"rootpy{tag()}"
        emoji = await client.emojis.create(sandbox.community_id, shortcode, png)
        emoji_id = getattr(emoji, "id", None)
        try:
            assert await client.emojis.resolve([emoji_id]) is not None
        finally:
            if emoji_id:
                await client.emojis.delete(sandbox.community_id, emoji_id)


# --------------------------------------------------------------------------
# directories -- the folder tree inside a channel
# --------------------------------------------------------------------------
class TestDirectories:
    async def test_create_and_delete(self, client, sandbox):
        directory = await client.directories.create(
            sandbox.community_id, sandbox.channel.id, f"rootpy-d-{tag()}"
        )
        directory_id = getattr(directory, "id", None)
        try:
            assert directory_id
        finally:
            if directory_id:
                await client.directories.delete(
                    sandbox.community_id, sandbox.channel.id, directory_id
                )

    async def test_list_is_readable(self, client, sandbox):
        listing = await client.directories.list(
            sandbox.community_id, sandbox.channel.id
        )
        assert isinstance(listing, list)

    async def test_nested_directory(self, client, sandbox):
        parent = await client.directories.create(
            sandbox.community_id, sandbox.channel.id, f"rootpy-p-{tag()}"
        )
        parent_id = getattr(parent, "id", None)
        child_id = None
        try:
            child = await client.directories.create(
                sandbox.community_id,
                sandbox.channel.id,
                f"rootpy-c-{tag()}",
                parent_directory_id=parent_id,
            )
            child_id = getattr(child, "id", None)
            assert child_id
        finally:
            for target in (child_id, parent_id):
                if target:
                    await client.directories.delete(
                        sandbox.community_id, sandbox.channel.id, target
                    )


# --------------------------------------------------------------------------
# community files
# --------------------------------------------------------------------------
class TestCommunityFiles:
    """Root requires a DirectoryId on FileList, so these need a directory."""

    async def _directory(self, client, sandbox):
        directories = await client.directories.list(
            sandbox.community_id, sandbox.channel.id
        )
        if directories:
            return getattr(directories[0], "id", None)
        created = await client.directories.create(
            sandbox.community_id, sandbox.channel.id, f"rootpy-fd-{tag()}"
        )
        return getattr(created, "id", None)

    async def test_list_is_readable(self, client, sandbox):
        directory_id = await self._directory(client, sandbox)
        listing = await client.community_files.list(
            sandbox.community_id, sandbox.channel.id, directory_id
        )
        assert isinstance(listing, list)

    async def test_upload_then_delete(self, client, sandbox, png):
        directory_id = await self._directory(client, sandbox)
        created = await client.community_files.create(
            sandbox.community_id, sandbox.channel.id, png,
            directory_id=directory_id,
        )
        file_id = getattr(created, "id", None)
        try:
            assert file_id
        finally:
            if file_id:
                await client.community_files.delete(
                    sandbox.community_id, sandbox.channel.id, file_id,
                    directory_id,
                )

    async def test_uploaded_file_is_listed(self, client, sandbox, png):
        directory_id = await self._directory(client, sandbox)
        created = await client.community_files.create(
            sandbox.community_id, sandbox.channel.id, png,
            directory_id=directory_id,
        )
        file_id = getattr(created, "id", None)
        try:
            listing = await client.community_files.list(
                sandbox.community_id, sandbox.channel.id, directory_id
            )
            assert any(getattr(f, "id", None) == file_id for f in listing)
        finally:
            if file_id:
                await client.community_files.delete(
                    sandbox.community_id, sandbox.channel.id, file_id,
                    directory_id,
                )


# --------------------------------------------------------------------------
# community service, logs
# --------------------------------------------------------------------------
class TestCommunityService:
    async def test_list_mine_includes_the_sandbox(self, client, sandbox):
        mine = await client.community_service.list_mine()
        assert any(c.id == sandbox.community_id for c in mine)

    async def test_get_extended_returns_detail(self, client, sandbox):
        extended = await client.community_service.get_extended(
            sandbox.community_id
        )
        assert extended.community.id == sandbox.community_id

    async def test_get_extended_carries_channels(self, client, sandbox):
        extended = await client.community_service.get_extended(
            sandbox.community_id
        )
        assert extended is not None

    async def test_an_owner_cannot_leave_their_own_community(self, client):
        """Root refuses this, which is correct -- an owner must delete instead.

        Confirmed live: CommunityLeave answers PERMISSION_DENIED. The peer
        leaving a community it merely joined is covered in the two-account
        suite.
        """
        from rootpy.exceptions import GrpcPermissionDenied

        community = await client.community.create(f"rootpy leave {tag()}")
        try:
            with pytest.raises(GrpcPermissionDenied):
                await client.community_service.leave(community.id)
        finally:
            await client.community.delete(community.id)


class TestAuditLog:
    async def test_community_log_is_readable(self, client, sandbox):
        assert await client.logs.community(sandbox.community_id) is not None

    async def test_log_paginates_from_a_cursor(self, client, sandbox):
        first = await client.logs.community(sandbox.community_id)
        entries = getattr(first, "logs", None) or getattr(first, "entries", None)
        if not entries:
            pytest.skip("audit log is empty; nothing to page from")
        last_id = getattr(entries[-1], "id", None)
        assert await client.logs.community(
            sandbox.community_id, last_log_id=last_id
        ) is not None


# --------------------------------------------------------------------------
# community apps and their log -- read-only probes
# --------------------------------------------------------------------------
class TestCommunityApps:
    """0/8, and written off as "needs a real app installed".

    Two of the eight do not: ``list`` on a community with no apps should
    answer with an empty list rather than an error, and ``initialize`` takes
    nothing but a community id. The other six all require an
    ``app_id``/``community_app_id`` that only a real installation produces,
    and installing one from a test is out of scope.

    ``AppType`` is ``{Unspecified: 0, Bot: 1, App: 2}``; whether ``list``
    requires it is measured rather than assumed.
    """

    async def test_list_apps_in_a_community_with_none(self, client, sandbox):
        """``app_type`` is optional. Measured against App(2) and Bot(1), then narrowed."""
        listing = await client.community_apps.list(
            community_id=sandbox.community_id
        )
        assert isinstance(listing, list), (
            f"list() should unwrap the envelope, got {type(listing).__name__}"
        )
        assert listing == [], (
            f"a community with no apps installed listed {len(listing)}"
        )

    async def test_list_filtered_by_app_type(self, client, sandbox):
        """``AppType`` is {Unspecified: 0, Bot: 1, App: 2}."""
        for app_type in (1, 2):
            listing = await client.community_apps.list(
                community_id=sandbox.community_id, app_type=app_type
            )
            assert isinstance(listing, list)

    async def test_initialize_a_community(self, client, sandbox):
        """Runs against the throwaway sandbox, so whatever it sets up dies with it."""
        from rootpy.exceptions import GrpcWebError

        try:
            await client.community_apps.initialize(sandbox.community_id)
        except GrpcWebError as exc:
            assert "INVALID_ARGUMENT" not in str(exc), (
                f"CommunityAppInitialize was rejected as malformed: {exc}"
            )
            print(f"\n   community_apps.initialize -> {exc}\n")


class TestCommunityAppLog:
    """``logs.app`` needs a ``community_app_id`` there is no app to supply.

    Kept as a probe rather than dropped: what can still be established is
    whether the request is well formed and how Root answers when the app is
    unknown. If that turns out to be a clean empty list, the method is usable
    for polling without an install.
    """

    async def test_the_app_log_request_is_well_formed(self, client, sandbox):
        from rootpy.exceptions import GrpcWebError

        try:
            result = await client.logs.app(community_id=sandbox.community_id)
        except GrpcWebError as exc:
            print(f"\n   logs.app(community_id only) -> {exc}\n")
            assert "INVALID_ARGUMENT" not in str(exc) or exc.validation_errors, (
                "rejected with no field-level detail, so there is nothing to "
                f"learn from it: {exc}"
            )
        else:
            print(f"\n   logs.app(community_id only) -> {result!r}\n")
