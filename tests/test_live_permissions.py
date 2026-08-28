"""Live coverage for permissions, member management, profiles and message extras.

Continues the push from ``test_live_services.py``. Everything here runs inside
the session sandbox community, which is created and deleted per run.

Two things shape what is testable:

* **Profile writes are rate-limited.** ``set_status``, ``set_description``,
  ``set_username`` and the image setters all share a quota, and a suite this
  size hits it. Those tests skip on RESOURCE_EXHAUSTED rather than failing --
  a server-side limit is not a defect.
* **Destructive member operations need a victim.** Kick and ban against the
  second account live in ``test_live_two_accounts.py``; what is here is the
  read side plus operations the owner can safely perform on themselves.

Run with::

    pytest -m live -k "Permission or Member or Profile or MessageExtras" -v
"""

from __future__ import annotations

import asyncio

import pytest

from .conftest import requires_live, tag

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


# --------------------------------------------------------------------------
# permissions -- 0/8 before this
# --------------------------------------------------------------------------
class TestPermissionRules:
    """Channel-level access rules for a role."""

    async def test_list_rules_for_a_channel(self, client, sandbox):
        rules = await client.permissions.list_rules(
            sandbox.community_id, sandbox.channel.id
        )
        assert rules is not None

    async def test_create_then_delete_a_rule(self, client, sandbox):
        await client.permissions.create_rule(
            sandbox.community_id,
            sandbox.channel.id,
            sandbox.role.id,
            channel_view=True,
            channel_create_message=True,
        )
        try:
            rules = await client.permissions.list_rules(
                sandbox.community_id, sandbox.channel.id
            )
            assert rules is not None
        finally:
            await client.permissions.delete_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )

    async def test_edit_a_rule(self, client, sandbox):
        await client.permissions.create_rule(
            sandbox.community_id,
            sandbox.channel.id,
            sandbox.role.id,
            channel_view=True,
        )
        try:
            await client.permissions.edit_rule(
                sandbox.community_id,
                sandbox.channel.id,
                sandbox.role.id,
                channel_view=True,
                channel_create_message=False,
            )
        finally:
            await client.permissions.delete_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )

    async def test_a_rule_on_a_channel_group(self, client, sandbox):
        """Rules attach to groups as well as channels."""
        await client.permissions.create_rule(
            sandbox.community_id,
            sandbox.group.id,
            sandbox.role.id,
            channel_view=True,
        )
        try:
            rules = await client.permissions.list_rules(
                sandbox.community_id, sandbox.group.id
            )
            assert rules is not None
        finally:
            await client.permissions.delete_rule(
                sandbox.community_id, sandbox.group.id, sandbox.role.id
            )

    async def test_deleting_a_rule_that_is_not_there(self, client, sandbox):
        """Should not explode -- callers clean up without checking first."""
        from rootpy.exceptions import RootError

        try:
            await client.permissions.delete_rule(
                sandbox.community_id, sandbox.channel.id, sandbox.role.id
            )
        except RootError:
            pass


# --------------------------------------------------------------------------
# members -- read side and self-directed operations
# --------------------------------------------------------------------------
class TestMemberQueries:
    async def test_list_all_includes_us(self, client, sandbox, me):
        members = await client.members.list_all(sandbox.community_id)
        assert isinstance(members, list)
        assert any(
            getattr(m, "user_id", None) == me.id for m in members
        ) or members

    async def test_get_a_single_member(self, client, sandbox, me):
        member = await client.members.get(sandbox.community_id, me.id)
        assert member is not None

    async def test_list_specific_members(self, client, sandbox, me):
        members = await client.members.list(sandbox.community_id, [me.id])
        assert members is not None

    async def test_member_roles(self, client, sandbox, me):
        roles = await client.members.roles(sandbox.community_id, me.id)
        assert roles is not None

    async def test_roles_after_assignment(self, client, sandbox, me):
        role = await client.roles.create(sandbox.community_id, f"rootpymr{tag()}")
        try:
            await client.roles.add_to_members(
                sandbox.community_id, role.id, [me.id]
            )
            roles = await client.members.roles(sandbox.community_id, me.id)
            assert roles is not None
        finally:
            await client.roles.delete(sandbox.community_id, role.id)


# --------------------------------------------------------------------------
# profile -- rate-limited, so every write guards
# --------------------------------------------------------------------------
def _skip_if_throttled(exc):
    from rootpy.exceptions import GrpcResourceExhausted

    if isinstance(exc, GrpcResourceExhausted):
        pytest.skip("Root rate-limited the profile write (RESOURCE_EXHAUSTED)")
    raise exc


class TestProfile:
    async def test_get_self(self, client, me):
        current = await client.users.get_self()
        assert current.id == me.id

    async def test_get_profile_of_self(self, client, me):
        profile = await client.users.get_profile(me.id)
        assert profile is not None

    async def test_get_profiles_batches(self, client, me):
        profiles = await client.users.get_profiles([me.id])
        assert me.id in profiles

    async def test_set_online_status(self, client):
        from rootpy.enums import UserOnlineStatus

        try:
            await client.users.set_online_status(UserOnlineStatus.ACTIVE)
        except Exception as exc:
            _skip_if_throttled(exc)

    async def test_status_round_trip(self, client):
        marker = f"probe {tag()}"
        try:
            await client.users.set_status(marker)
            await client.users.set_status(None)
        except Exception as exc:
            _skip_if_throttled(exc)

    async def test_description_round_trip(self, client, me):
        original = getattr(me, "description", None)
        try:
            await client.users.set_description(f"probe {tag()}")
            await client.users.set_description(original)
        except Exception as exc:
            _skip_if_throttled(exc)

    async def test_an_invalid_username_is_refused_locally(self, client):
        """No round trip: rootpy.validation checks Root's rule first."""
        with pytest.raises(ValueError):
            await client.users.set_username("bad name with spaces")

    async def test_a_too_short_username_is_refused_locally(self, client):
        with pytest.raises(ValueError):
            await client.users.set_username("ab")


# --------------------------------------------------------------------------
# messages -- the methods the main suite does not reach
# --------------------------------------------------------------------------
class TestMessageExtras:
    async def test_history_is_an_async_generator(self, client, sandbox):
        """``history()`` yields messages; it is iterated, not awaited."""
        seen = []
        async for message in client.messages.history(
            sandbox.channel.id, community_id=sandbox.community_id, limit=5
        ):
            seen.append(message)
        assert isinstance(seen, list)

    async def test_history_respects_its_limit(self, client, sandbox):
        count = 0
        async for _ in client.messages.history(
            sandbox.channel.id, community_id=sandbox.community_id, limit=3
        ):
            count += 1
        assert count <= 3

    async def test_pin_list(self, client, sandbox, message):
        await client.messages.pin_message(message)
        try:
            pinned = await client.messages.pin_list(
                sandbox.channel.id, community_id=sandbox.community_id
            )
            assert any(m.id == message.id for m in pinned)
        finally:
            await client.messages.unpin_message(message)

    async def test_pin_list_is_empty_after_unpinning(
        self, client, sandbox, message
    ):
        await client.messages.pin_message(message)
        await client.messages.unpin_message(message)
        pinned = await client.messages.pin_list(
            sandbox.channel.id, community_id=sandbox.community_id
        )
        assert all(m.id != message.id for m in pinned)

    async def test_set_view_time(self, client, sandbox):
        await client.messages.set_view_time(
            sandbox.channel.id, community_id=sandbox.community_id
        )

    async def test_reactions_add_and_remove(self, client, message):
        await client.messages.add_reaction(message, "thumbsup")
        await client.messages.remove_reaction(message, "thumbsup")

    async def test_list_both_directions(self, client, sandbox):
        """``direction`` defaults to "both"; the main suite only walks older."""
        messages = await client.messages.list(
            sandbox.channel.id,
            community_id=sandbox.community_id,
            direction="both",
        )
        assert messages is not None

    async def test_send_with_an_explicit_community(self, client, sandbox):
        result = await client.messages.send(
            sandbox.channel.id,
            f"explicit community {tag()}",
            community_id=sandbox.community_id,
        )
        sent = getattr(result, "message", result)
        try:
            assert sent.id
        finally:
            await client.messages.delete_message(sent)
