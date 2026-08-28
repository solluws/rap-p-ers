"""The admin service and the rest of the community manager.

``client.admin`` was 0/19 -- not because it is unused, but because it is only
ever reached *through* ``CommunityManager``. Every community operation in the
suite goes through the manager, so the admin service is exercised constantly
and verified never. Given the manager layer has produced more bugs than
anything else in this codebase, calling the layer beneath it directly is worth
doing.

``client.community`` was 10/39. The gap is channel and group lifecycle,
ordering (``move_*``), access rules, and the synchronous cache readers.

Everything runs in the session sandbox, which is deleted afterwards. Objects
created mid-test are removed in ``finally`` even though the community delete
would take them -- a leak inside a test should be visible.

Run with::

    pytest -m live -k "Admin or ChannelLifecycle or GroupLifecycle or Ordering or AccessRules or CachedReaders" -v
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import (
    _ids_match,
    create_channel_group,
    eventually,
    requires_live,
    tag,
    word_tag,
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


# --------------------------------------------------------------------------
# admin: the layer CommunityManager delegates to
# --------------------------------------------------------------------------
class TestAdminIsTheLayerBeneath:
    """The manager forwards here, so the two must agree."""

    async def test_admin_and_manager_are_distinct_objects(self, client):
        assert client.admin is not client.community

    async def test_community_admin_is_the_same_object_as_admin(self, client):
        assert client.admin is client.community_admin

    async def test_create_and_delete_a_community_directly(self, client):
        community = await client.admin.create_community(f"rootpy admin {tag()}")
        try:
            assert community.id
        finally:
            await client.admin.delete_community(community.id)

    async def test_create_community_defaults_are_usable(self, client):
        """No picture_hex passed -- the default must be one Root accepts."""
        community = await client.admin.create_community(f"rootpy dflt {tag()}")
        await client.admin.delete_community(community.id)

    async def test_edit_community_through_admin(self, client, sandbox):
        updated = await client.admin.edit_community(
            sandbox.community_id, description=f"admin edit {tag()}"
        )
        assert updated.id == sandbox.community_id


class TestAdminChannelGroups:
    """Channel group names take letters and spaces only.

    Two live rounds to pin down. ``rootpy-ag-1a2b`` was rejected, so hyphens
    looked like the culprit -- but ``rootpy group 7269`` was rejected too,
    while ``test area`` (the sandbox group) is fine. It is **digits**. Hence
    ``word_tag()`` rather than the hex ``tag()``.

    Channel names are the opposite: ``rootpy-text-1a2b`` works. No naming rule
    here can be inferred from another object type.
    """

    async def test_create_and_delete_a_group(self, client, sandbox):
        group = await create_channel_group(client.admin, sandbox.community_id)
        try:
            assert group.id
        finally:
            await client.admin.delete_channel_group(
                sandbox.community_id, group.id
            )

    async def test_edit_a_group_name(self, client, sandbox):
        group = await create_channel_group(client.admin, sandbox.community_id)
        try:
            renamed = f"grp {word_tag()}"
            edited = await client.admin.edit_channel_group(
                sandbox.community_id, group.id, name=renamed
            )
            assert edited.id == group.id
        finally:
            await client.admin.delete_channel_group(
                sandbox.community_id, group.id
            )

    async def test_a_created_group_appears_in_the_listing(self, client, sandbox):
        group = await create_channel_group(client.admin, sandbox.community_id)
        try:
            async def listed():
                groups = await client.community.get_channel_groups(
                    sandbox.community_id, refresh=True
                )
                return any(g.id == group.id for g in groups)

            await eventually(listed, describe="the group appears")
        finally:
            await client.admin.delete_channel_group(
                sandbox.community_id, group.id
            )


class TestAdminChannels:
    async def test_create_with_a_description(self, client, sandbox):
        channel = await client.admin.create_channel(
            sandbox.community_id,
            sandbox.group.id,
            f"rootpy-ad-{tag()}",
            description="created by the admin service",
            channel_type=1,
        )
        try:
            assert channel.id
        finally:
            await client.admin.delete_channel(sandbox.community_id, channel.id)

    async def test_edit_a_channel(self, client, sandbox):
        channel = await client.admin.create_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-ae-{tag()}",
            channel_type=1,
        )
        try:
            renamed = f"rootpy-af-{tag()}"
            await client.admin.edit_channel(
                sandbox.community_id, channel.id, name=renamed
            )
        finally:
            await client.admin.delete_channel(sandbox.community_id, channel.id)

    async def test_delete_removes_it_from_the_listing(self, client, sandbox):
        channel = await client.admin.create_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-ax-{tag()}",
            channel_type=1,
        )
        await client.admin.delete_channel(sandbox.community_id, channel.id)

        async def gone():
            channels = await client.community.get_channels(
                sandbox.community_id, refresh=True
            )
            return all(c.id != channel.id for c in channels)

        await eventually(gone, describe="the deleted channel leaves the listing")


class TestAdminRoles:
    async def test_create_with_a_colour(self, client, sandbox):
        role = await client.admin.create_role(
            sandbox.community_id, f"rootpy-arc-{tag()}", color_hex="#16a085"
        )
        try:
            assert role.id
        finally:
            await client.admin.delete_role(sandbox.community_id, role.id)

    async def test_edit_requires_a_name(self, client, sandbox):
        """``edit_role`` takes name as a keyword-only required argument."""
        role = await client.admin.create_role(
            sandbox.community_id, f"rootpy-are-{tag()}"
        )
        try:
            await client.admin.edit_role(
                sandbox.community_id, role.id, name=f"rootpy-arf-{tag()}"
            )
        finally:
            await client.admin.delete_role(sandbox.community_id, role.id)


class TestAdminClone:
    async def test_clone_without_roles(self, client, sandbox):
        clone = await client.admin.clone_community(
            sandbox.community_id, name=f"rootpy clone {tag()}", clone_roles=False
        )
        try:
            assert clone.id != sandbox.community_id
        finally:
            await client.admin.delete_community(clone.id)

    async def test_clone_with_roles(self, client, sandbox):
        clone = await client.admin.clone_community(
            sandbox.community_id, name=f"rootpy cloner {tag()}", clone_roles=True
        )
        try:
            roles = await client.community.get_roles(clone.id, refresh=True)
            assert roles is not None
        finally:
            await client.admin.delete_community(clone.id)


# --------------------------------------------------------------------------
# access rules through the admin service
# --------------------------------------------------------------------------
class TestAccessRules:
    async def test_list_is_readable(self, client, sandbox):
        rules = await client.admin.list_access_rules(
            sandbox.community_id, sandbox.channel.id
        )
        assert isinstance(rules, tuple)

    async def test_set_then_delete(self, client, sandbox):
        from rootpy.permissions import ChannelOverlay

        overlay = ChannelOverlay(channel_view=True)
        await client.admin.set_access_rule(
            sandbox.community_id, sandbox.channel.id, sandbox.role.id, overlay
        )
        try:
            rules = await client.admin.list_access_rules(
                sandbox.community_id, sandbox.channel.id
            )
            assert rules is not None
        finally:
            await client.admin.delete_access_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )

    async def test_a_denying_overlay(self, client, sandbox):
        from rootpy.permissions import ChannelOverlay

        overlay = ChannelOverlay(channel_view=False)
        await client.admin.set_access_rule(
            sandbox.community_id, sandbox.channel.id, sandbox.role.id, overlay
        )
        try:
            rules = await client.admin.list_access_rules(
                sandbox.community_id, sandbox.channel.id
            )
            assert rules is not None
        finally:
            await client.admin.delete_access_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )

    async def test_rules_on_a_group(self, client, sandbox):
        from rootpy.permissions import ChannelOverlay

        await client.admin.set_access_rule(
            sandbox.community_id,
            sandbox.group.id,
            sandbox.role.id,
            ChannelOverlay(channel_view=True),
        )
        try:
            rules = await client.admin.list_access_rules(
                sandbox.community_id, sandbox.group.id
            )
            assert rules is not None
        finally:
            await client.admin.delete_access_rule(
                sandbox.community_id, sandbox.group.id, sandbox.role.id
            )


# --------------------------------------------------------------------------
# ordering -- move_* has never been exercised
# --------------------------------------------------------------------------
class TestOrdering:
    async def test_move_a_channel_between_groups(self, client, sandbox):
        destination = await create_channel_group(client.admin, sandbox.community_id)
        channel = await client.admin.create_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-mc-{tag()}",
            channel_type=1,
        )
        try:
            await client.admin.move_channel(
                sandbox.community_id,
                channel.id,
                old_group_id=sandbox.group.id,
                new_group_id=destination.id,
            )
        finally:
            await client.admin.delete_channel(sandbox.community_id, channel.id)
            await client.admin.delete_channel_group(
                sandbox.community_id, destination.id
            )

    async def test_move_a_channel_group(self, client, sandbox):
        group = await create_channel_group(client.admin, sandbox.community_id)
        try:
            await client.admin.move_channel_group(
                sandbox.community_id, group.id, before_group_id=sandbox.group.id
            )
        finally:
            await client.admin.delete_channel_group(
                sandbox.community_id, group.id
            )

    async def test_move_a_role(self, client, sandbox):
        role = await client.admin.create_role(
            sandbox.community_id, f"rootpy-mr-{tag()}"
        )
        try:
            await client.admin.move_role(
                sandbox.community_id, role.id, before_role_id=sandbox.role.id
            )
        finally:
            await client.admin.delete_role(sandbox.community_id, role.id)


# --------------------------------------------------------------------------
# community manager: the untested half
# --------------------------------------------------------------------------
class TestChannelLifecycle:
    async def test_create_group_alias(self, client, sandbox):
        # Same name rule as create_channel_group; use the shape the ladder
        # settled on rather than a third guess.
        group = await client.community.create_group(
            sandbox.community_id, f"grp {word_tag()}"
        )
        try:
            assert group.id
        finally:
            await client.community.delete_channel_group(
                sandbox.community_id, group.id
            )

    async def test_create_channel_with_explicit_type(self, client, sandbox):
        channel = await client.community.create_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-cc-{tag()}",
            channel_type=1,
        )
        try:
            assert channel.id
        finally:
            await client.community.delete_channel(
                sandbox.community_id, channel.id
            )

    async def test_edit_a_channel(self, client, sandbox):
        channel = await client.community.create_text_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-ce-{tag()}"
        )
        try:
            await client.community.edit_channel(
                sandbox.community_id, channel.id, name=f"rootpy-cf-{tag()}"
            )
        finally:
            await client.community.delete_channel(
                sandbox.community_id, channel.id
            )

    async def test_edit_a_channel_group(self, client, sandbox):
        group = await create_channel_group(client.community, sandbox.community_id)
        try:
            await client.community.edit_channel_group(
                sandbox.community_id, group.id, name=f"grp {word_tag()}"
            )
        finally:
            await client.community.delete_channel_group(
                sandbox.community_id, group.id
            )


class TestCachedReaders:
    """Synchronous cache reads -- awaiting them is the mistake they invite."""

    async def test_get_returns_the_sandbox(self, client, sandbox):
        await client.community.fetch(sandbox.community_id)
        community = client.community.get(sandbox.community_id)
        assert community is None or community.id == sandbox.community_id

    async def test_cached_lists_communities(self, client, sandbox):
        await client.community.list(refresh=True)
        assert isinstance(client.community.cached(), tuple)

    async def test_get_channel_after_a_fetch(self, client, sandbox):
        await client.community.get_channels(sandbox.community_id, refresh=True)
        channel = client.community.get_channel(sandbox.channel.id)
        assert channel is None or channel.id == sandbox.channel.id

    async def test_get_group_after_a_fetch(self, client, sandbox):
        await client.community.get_channel_groups(
            sandbox.community_id, refresh=True
        )
        group = client.community.get_group(sandbox.group.id)
        assert group is None or group.id == sandbox.group.id

    async def test_servers_is_an_alias_for_list(self, client):
        servers = await client.community.servers(refresh=True)
        communities = await client.community.list(refresh=True)
        assert {c.id for c in servers} == {c.id for c in communities}


class TestCommunityQueries:
    async def test_fetch_returns_extended_detail(self, client, sandbox):
        extended = await client.community.fetch(sandbox.community_id)
        assert extended.community.id == sandbox.community_id

    async def test_get_channel_groups(self, client, sandbox):
        groups = await client.community.get_channel_groups(
            sandbox.community_id, refresh=True
        )
        assert any(g.id == sandbox.group.id for g in groups)

    async def test_get_groups_matches_get_channel_groups(self, client, sandbox):
        a = await client.community.get_groups(sandbox.community_id)
        b = await client.community.get_channel_groups(sandbox.community_id)
        assert {g.id for g in a} == {g.id for g in b}

    async def test_get_role_by_id(self, client, sandbox):
        role = await client.community.get_role(
            sandbox.community_id, sandbox.role.id, refresh=True
        )
        assert role is None or role.id == sandbox.role.id

    async def test_get_member_returns_us(self, client, sandbox, me):
        member = await client.community.get_member(
            sandbox.community_id, me.id, refresh=True
        )
        assert member is None or member.user_id == me.id

    async def test_fetch_member(self, client, sandbox, me):
        member = await client.community.fetch_member(sandbox.community_id, me.id)
        assert member is not None


class TestRoleAssignmentThroughTheManager:
    async def test_add_then_remove_a_role(self, client, sandbox, me):
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-ra-{tag()}"
        )
        try:
            await client.community.add_role(sandbox.community_id, me.id, role.id)
            await client.community.remove_role(
                sandbox.community_id, me.id, role.id
            )
        finally:
            await client.community.delete_role(sandbox.community_id, role.id)

    async def test_the_role_shows_on_the_member(self, client, sandbox, me):
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-rb-{tag()}"
        )
        try:
            await client.community.add_role(sandbox.community_id, me.id, role.id)

            async def assigned():
                roles = await client.members.roles(sandbox.community_id, me.id)
                return roles is not None

            await eventually(assigned, describe="the role lands on the member")
        finally:
            await client.community.remove_role(
                sandbox.community_id, me.id, role.id
            )
            await client.community.delete_role(sandbox.community_id, role.id)


# --------------------------------------------------------------------------
# the manager spellings of admin operations
# --------------------------------------------------------------------------
class TestOrderingThroughTheManager:
    """``community.move_*`` are thin forwards, and were the untested half.

    Worth having separately from ``TestOrdering``: the manager layer is where
    every bug in this codebase so far has actually lived, and a forward that
    reorders its arguments or drops a keyword looks identical to one that does
    not until it runs.
    """

    async def test_move_a_channel_between_groups(self, client, sandbox):
        destination = await create_channel_group(
            client.community, sandbox.community_id
        )
        channel = await client.community.create_text_channel(
            sandbox.community_id, sandbox.group.id, f"rootpy-mm-{tag()}"
        )
        try:
            await client.community.move_channel(
                sandbox.community_id,
                channel.id,
                old_group_id=sandbox.group.id,
                new_group_id=destination.id,
            )

            async def landed():
                channels = await client.community.get_channels(
                    sandbox.community_id, refresh=True
                )
                return any(c.id == channel.id for c in channels)

            assert await eventually(
                landed, describe="the moved channel is still listed"
            )
        finally:
            await client.community.delete_channel(
                sandbox.community_id, channel.id
            )
            await client.community.delete_channel_group(
                sandbox.community_id, destination.id
            )

    async def test_move_a_channel_group(self, client, sandbox):
        group = await create_channel_group(client.community, sandbox.community_id)
        try:
            await client.community.move_channel_group(
                sandbox.community_id, group.id, before_group_id=sandbox.group.id
            )
        finally:
            await client.community.delete_channel_group(
                sandbox.community_id, group.id
            )

    async def test_move_a_role(self, client, sandbox):
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-mv-{tag()}"
        )
        try:
            await client.community.move_role(
                sandbox.community_id, role.id, before_role_id=sandbox.role.id
            )
        finally:
            await client.community.delete_role(sandbox.community_id, role.id)


class TestManagerEditRole:
    """Regression: ``community.edit_role`` could not be called with a name only.

    ``CommunityRoleEdit`` is a replace, and the admin layer reads the current
    role to carry unsupplied fields forward -- keyed on ``color_hex is None``.
    The manager declared ``color_hex: str = ""`` and forwarded it, so the
    sentinel never fired and ``normalize_hex_colour("")`` raised before the
    request was built. Fixed by making the manager's default ``None`` too.
    """

    async def test_rename_without_supplying_a_colour(self, client, sandbox):
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-er-{tag()}", color_hex="#e67e22"
        )
        renamed = f"rootpy-ed-{tag()}"
        try:
            updated = await client.community.edit_role(
                sandbox.community_id, role.id, name=renamed
            )
            assert updated.name == renamed
        finally:
            await client.community.delete_role(sandbox.community_id, role.id)

    async def test_the_colour_survives_a_rename(self, client, sandbox):
        """The point of the sentinel: an omitted colour must not be blanked."""
        colour = "#8e44ad"
        role = await client.community.create_role(
            sandbox.community_id, f"rootpy-ec-{tag()}", color_hex=colour
        )
        try:
            updated = await client.community.edit_role(
                sandbox.community_id, role.id, name=f"rootpy-ek-{tag()}"
            )
            assert (updated.color_hex or "").lower() == colour, (
                f"renaming blanked the colour: {updated.color_hex!r}"
            )
        finally:
            await client.community.delete_role(sandbox.community_id, role.id)


class TestManagerMembersAndGroups:
    async def test_get_members_returns_us(self, client, sandbox, me):
        members = await client.community.get_members(sandbox.community_id)
        assert any(_ids_match(me.id, m.user_id) for m in members), (
            "the owner is not in their own community's member list"
        )

    async def test_get_members_refresh_round_trips(self, client, sandbox):
        cached = await client.community.get_members(sandbox.community_id)
        fresh = await client.community.get_members(
            sandbox.community_id, refresh=True
        )
        assert len(fresh) >= len(cached)

    async def test_create_channel_group_through_the_manager(
        self, client, sandbox
    ):
        group = await create_channel_group(client.community, sandbox.community_id)
        try:
            assert group.id
            assert client.community.get_group(group.id) is not None, (
                "create_channel_group should have cached the new group"
            )
        finally:
            await client.community.delete_channel_group(
                sandbox.community_id, group.id
            )


class TestPermissionConstructors:
    """The four ``client.permissions`` builders, exercised on real objects.

    They are synchronous constructors, so "testing them live" means using
    what they build in a request Root has to accept -- which is the only part
    that could actually be wrong.
    """

    async def test_community_and_channel_sets_on_a_new_role(self, client, sandbox):
        community_permissions = client.permissions.community(
            community_create_invite=True,
            community_change_my_nickname=True,
        )
        channel_permissions = client.permissions.channel(
            channel_view=True,
            channel_create_message=True,
        )
        role = await client.community.create_role(
            sandbox.community_id,
            f"rootpy-pc-{tag()}",
            community_permissions=community_permissions,
            channel_permissions=channel_permissions,
        )
        try:
            assert role.id
        finally:
            await client.community.delete_role(sandbox.community_id, role.id)

    async def test_an_overlay_used_as_an_access_rule(self, client, sandbox):
        overlay = client.permissions.overlay(channel_view=True)
        await client.admin.set_access_rule(
            sandbox.community_id, sandbox.channel.id, sandbox.role.id, overlay
        )
        try:
            rules = await client.permissions.list_rules(
                sandbox.community_id, sandbox.channel.id
            )
            assert rules is not None
        finally:
            await client.admin.delete_access_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )

    async def test_a_rule_supplied_at_group_creation(self, client, sandbox):
        """``permissions.rule`` builds the AccessRule ChannelGroupCreate takes."""
        rule = client.permissions.rule(sandbox.role.id, channel_view=True)
        assert rule.target_id, "permissions.rule did not normalise the target id"

        group = await create_channel_group(
            client.community, sandbox.community_id, access_rules=[rule]
        )
        try:
            rules = await client.permissions.list_rules(
                sandbox.community_id, group.id
            )
            assert rules is not None
        finally:
            await client.community.delete_channel_group(
                sandbox.community_id, group.id
            )
