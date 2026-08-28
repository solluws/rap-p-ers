from __future__ import annotations

from .validation import validate_nickname

from pathlib import Path
from typing import Iterable, Optional


def unwrap_list(data):
    """Return the item list out of a Root ``*ListResponse`` envelope.

    Every list RPC answers with a message wrapping exactly one repeated field
    -- ``CommunityRoleListResponse.CommunityRoles``,
    ``CommunityMemberBanListResponse.CommunityMemberBans`` and so on. Handing
    the envelope back meant iterating a ``list()`` call yielded field *names*,
    so ``for role in await client.roles.list(...)`` gave strings and
    ``role.id`` raised AttributeError.

    Falls back to the value itself when there is nothing to unwrap, so this is
    safe on a response shape that is already a list.
    """
    if data is None:
        return []
    if isinstance(data, (list, tuple)):
        return list(data)

    if isinstance(data, dict):
        candidates = [v for v in data.values() if isinstance(v, (list, tuple))]
        if len(candidates) == 1:
            return list(candidates[0])
        return list(data.values()) if not candidates else list(candidates[0])

    found = []
    for name in dir(data):
        if name.startswith("_"):
            continue
        try:
            value = getattr(data, name)
        except Exception:
            continue
        if isinstance(value, (list, tuple)):
            found.append(list(value))
    if len(found) == 1:
        return found[0]
    return found[0] if found else []


class RoleManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        name: str,
        *,
        color_hex: str = "",
        community_permission=None,
        channel_permission=None,
        is_mentionable: bool = False,
        is_self_assignable: bool = False,
    ):
        kwargs = {
            "community_id": community_id,
            "name": name,
            "color_hex": color_hex,
            "is_mentionable": is_mentionable,
            "is_self_assignable": is_self_assignable,
        }
        if community_permission is not None:
            kwargs["community_permission"] = community_permission
        if channel_permission is not None:
            kwargs["channel_permission"] = channel_permission

        return (
            await self.client.high.community_role.create(
                **kwargs
            )
        ).data

    async def edit(self, community_id: str, role_id: str, **kwargs):
        """Edit a role. Unsupplied fields keep their current values.

        ``CommunityRoleEdit`` is a *replace*, not a patch -- the request
        carries Name, ColorHex, both permission sets and the two flags, so
        sending only ``name`` blanked the rest and the server answered
        INTERNAL (13) rather than a useful validation error. Same shape as
        CommunityEdit.

        The current role is read once and every field the caller did not give
        is carried forward. Supply them all to skip the read.
        """
        kwargs = dict(kwargs)
        carried = (
            "name",
            "color_hex",
            "community_permission",
            "channel_permission",
            "is_mentionable",
            "is_self_assignable",
        )
        if any(field not in kwargs for field in carried):
            current = await self.get(community_id, role_id)
            for field in carried:
                if field not in kwargs:
                    value = getattr(current, field, None)
                    if value is not None:
                        kwargs[field] = value

        kwargs["community_id"] = community_id
        kwargs["id"] = role_id
        return (
            await self.client.high.community_role.edit(
                **kwargs
            )
        ).data

    async def delete(self, community_id: str, role_id: str):
        return (
            await self.client.high.community_role.delete(
                community_id=community_id,
                id=role_id,
            )
        ).data

    async def get(self, community_id: str, role_id: str):
        return (
            await self.client.high.community_role.get(
                community_id=community_id,
                id=role_id,
            )
        ).data

    async def list(self, community_id: str):
        """The community's roles, as a list of role records."""
        response = await self.client.high.community_role.list(
            community_id=community_id,
        )
        return unwrap_list(response.data)

    async def move(
        self,
        community_id: str,
        role_id: str,
        *,
        before_role_id: Optional[str] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "id": role_id,
        }
        if before_role_id is not None:
            kwargs["before_community_role_id"] = before_role_id
        return (
            await self.client.high.community_role.move(
                **kwargs
            )
        ).data

    async def add_to_members(
        self,
        community_id: str,
        role_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_role.add(
                community_id=community_id,
                community_role_id=role_id,
                user_ids=list(user_ids),
            )
        ).data

    async def remove_from_members(
        self,
        community_id: str,
        role_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_role.remove(
                community_id=community_id,
                community_role_id=role_id,
                user_ids=list(user_ids),
            )
        ).data

    async def set_primary(
        self,
        community_id: str,
        user_id: str,
        role_id: str,
    ):
        return (
            await self.client.high.community_member_role.set_primary(
                community_id=community_id,
                user_id=user_id,
                community_role_id=role_id,
            )
        ).data


class MemberManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member.get(
                community_id=community_id,
                user_id=user_id,
            )
        ).data

    async def list(
        self,
        community_id: str,
        user_ids: Iterable[str],
    ):
        """The named members, as records.

        ``list`` and ``list_all`` answer with the *same* wire message --
        ``CommunityMemberExtendedListResponse`` -- but only ``list_all``
        unwrapped it, so ``for m in await members.list(cid, ids)`` iterated
        the envelope and yielded the field name ``'community_members'``.
        That is the trap HANDOFF documents and every other list method here
        already avoids; this one was simply missed.
        """
        response = await self.client.high.community_member.list(
            community_id=community_id,
            user_ids=list(user_ids),
        )
        return unwrap_list(response.data)

    async def list_all(self, community_id: str):
        response = await self.client.high.community_member.list_all(
            community_id=community_id,
        )
        return unwrap_list(response.data)

    async def edit_nickname(
        self,
        community_id: str,
        user_id: str,
        nickname: str,
    ):
        """Set a member's nickname in this community.

        Root validates Nickname with a RegularExpressionValidator and gives
        no pattern; see :func:`rootpy.validation.validate_nickname` for what
        the rule is and how much of it is confirmed.
        """
        validate_nickname(nickname)
        return (
            await self.client.high.community_member.edit(
                community_id=community_id,
                user_id=user_id,
                nickname=nickname,
            )
        ).data

    async def kick(self, community_id: str, user_id: str):
        return await self.client.moderation.kick(
            community_id,
            user_id,
        )

    async def kick_bulk(
        self,
        community_id: str,
        user_ids: Iterable[str],
    ):
        return (
            await self.client.high.community_member_ban.kick_bulk(
                community_id=community_id,
                user_ids=list(user_ids),
            )
        ).data

    async def ban(
        self,
        community_id: str,
        user_id: str,
        *,
        reason: Optional[str] = None,
        expires_at=None,
    ):
        return await self.client.moderation.ban(
            community_id,
            user_id,
            reason=reason,
            expires_at=expires_at,
        )

    async def ban_bulk(
        self,
        community_id: str,
        user_ids: Iterable[str],
        *,
        reason: Optional[str] = None,
        expires_at=None,
    ):
        kwargs = {
            "community_id": community_id,
            "user_ids": list(user_ids),
        }
        if reason is not None:
            kwargs["reason"] = reason
        if expires_at is not None:
            kwargs["expires_at"] = expires_at

        return (
            await self.client.high.community_member_ban.create_bulk(
                **kwargs
            )
        ).data

    async def unban(self, community_id: str, user_id: str):
        return await self.client.moderation.unban(
            community_id,
            user_id,
        )

    async def roles(self, community_id: str, user_id: str):
        return (
            await self.client.high.community_member_role.list(
                community_id=community_id,
                user_id=user_id,
            )
        ).data


class CommunityFileManager:
    def __init__(self, client) -> None:
        self.client = client

    async def create(
        self,
        community_id: str,
        container_id: str,
        source: str,
        *,
        directory_id: Optional[str] = None,
    ):
        path = Path(source).expanduser()
        if path.is_file():
            upload_token_uri = await self.client.assets.upload_file(
                str(path)
            )
        else:
            upload_token_uri = source

        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "upload_token_uri": upload_token_uri,
        }
        if directory_id is not None:
            kwargs["directory_id"] = directory_id

        return (
            await self.client.high.file.create(**kwargs)
        ).data

    async def list(self, community_id: str, container_id: str, directory_id: str):
        """Files in a directory -- the items, not the envelope.

        ``directory_id`` is required: Root applies a NotEmptyValidator to it,
        so listing "the channel's files" means listing a directory. Get the
        root directory id from ``client.directories.list(...)``. The old
        ``**kwargs`` signature hid that entirely.
        """
        return unwrap_list(
            (
                await self.client.high.file.list(
                    community_id=community_id,
                    container_id=container_id,
                    directory_id=directory_id,
                )
            ).data
        )

    async def get(
        self, community_id: str, container_id: str, file_id: str, directory_id: str
    ):
        return (
            await self.client.high.file.get(
                community_id=community_id,
                container_id=container_id,
                id=file_id,
                directory_id=directory_id,
            )
        ).data

    async def edit(
        self,
        community_id: str,
        container_id: str,
        file_id: str,
        directory_id: str,
        name: str,
    ):
        """Rename a file. ``name`` must not contain a dot or a space.

        Root validates ``Name`` with a ``RegularExpressionValidator`` and the
        rule is not the one you would guess: **the new name is the stem, not
        the filename.** Measured one variable at a time against a live server:

        ===================  ========
        ``renamedabcd``      accepted
        ``renamed-abcd``     accepted
        ``renamed_abcd``     accepted
        ``Renamedabcd``      accepted
        ``renamed1234``      accepted
        ``renamedabcd.png``  rejected
        ``renamedabcd.txt``  rejected
        ``renamed.abcd``     rejected
        ``renamed abcd``     rejected
        ===================  ========

        So hyphens, underscores, digits and uppercase are all fine, and it is
        the dot and the space that are refused -- any dot, not just an
        extension.

        The asymmetry is the trap: ``FileCreate`` stores the *uploaded*
        filename complete with its extension (``probe-1a2b.png``), and then
        ``FileEdit`` refuses to accept that same string back. Passing the name
        you just read off the record is the natural thing to do and it fails.
        """
        return (
            await self.client.high.file.edit(
                community_id=community_id,
                container_id=container_id,
                id=file_id,
                directory_id=directory_id,
                name=name,
            )
        ).data

    async def move(
        self,
        community_id: str,
        container_id: str,
        file_id: str,
        *,
        old_directory_id=None,
        new_directory_id=None,
    ):
        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "id": file_id,
        }
        if old_directory_id is not None:
            kwargs["old_directory_id"] = old_directory_id
        if new_directory_id is not None:
            kwargs["new_directory_id"] = new_directory_id
        return (await self.client.high.file.move(**kwargs)).data

    async def delete(
        self, community_id: str, container_id: str, file_id: str, directory_id: str
    ):
        """Delete a file. The wire field for the file is ``Id``."""
        return (
            await self.client.high.file.delete(
                community_id=community_id,
                container_id=container_id,
                id=file_id,
                directory_id=directory_id,
            )
        ).data

    async def download(self, **kwargs):
        """Root has not implemented this. Verified live, every argument shape.

        ``FileGrpcService/Download`` answers ``UNIMPLEMENTED (12)`` -- with the
        asset id off the file record, with the file id in its place, and with
        the field omitted entirely. It is not an argument problem.

        The obvious way round is the asset service, and it does not work
        either. A community file's ``asset_id`` builds a valid URI via
        :meth:`~rootpy.services.assets.AssetService.uri_for_id`, but only
        under the ``"file"`` kind, and that resolves to an *unsigned*
        ``static.rootapp.com`` URL which is then refused with HTTP 403. The
        ``"image"`` kind -- the one that produces signed, downloadable
        ``imagedelivery.net`` URLs for avatars, banners and emoji -- does not
        resolve for a file's asset at all, even when the file is a PNG.

        So there is currently no way to retrieve a community file's *content*
        through this API. Listing, renaming, moving and deleting all work; the
        bytes do not come back.

        This still issues the call rather than raising locally, so that if
        Root implements the endpoint it simply starts working -- and the live
        test pinning the ``UNIMPLEMENTED`` answer fails, which is the signal
        you want.
        """
        return (await self.client.high.file.download(**kwargs)).data

    async def search(self, **kwargs):
        return (await self.client.high.file.search(**kwargs)).data

    async def search_community(self, **kwargs):
        response = await self.client.high.file.search_community(**kwargs)
        return unwrap_list(response.data)


class LogManager:
    def __init__(self, client) -> None:
        self.client = client

    async def community(
        self,
        community_id: str,
        *,
        last_log_id: Optional[str] = None,
    ):
        kwargs = {"community_id": community_id}
        if last_log_id is not None:
            kwargs["last_community_log_id"] = last_log_id
        return (
            await self.client.high.community_log.list(**kwargs)
        ).data

    async def app(self, **kwargs):
        return (
            await self.client.high.community_app_log.list(**kwargs)
        ).data


class CommunityAppManager:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self, **kwargs):
        return (
            await self.client.high.community_app.get(**kwargs)
        ).data

    async def list(self, **kwargs):
        """Apps installed in a community, as a list."""
        response = await self.client.high.community_app.list(**kwargs)
        return unwrap_list(response.data)

    async def add(self, **kwargs):
        return (
            await self.client.high.community_app.add(**kwargs)
        ).data

    async def remove(self, **kwargs):
        return (
            await self.client.high.community_app.remove(**kwargs)
        ).data

    async def update_version(self, **kwargs):
        return (
            await self.client.high.community_app.update_version(**kwargs)
        ).data

    async def get_settings(self, **kwargs):
        return (
            await self.client.high.community_app.get_settings(**kwargs)
        ).data

    async def set_settings(self, **kwargs):
        return (
            await self.client.high.community_app.set_settings(**kwargs)
        ).data

    async def initialize(self, community_id: str):
        return (
            await self.client.high.community_app.initialize(
                community_id=community_id,
            )
        ).data


class VoiceAdminManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self, community_id: str, container_id: str):
        """Who is currently connected to a voice channel."""
        response = await self.client.high.web_rtc.list(
            community_id=community_id,
            container_id=container_id,
        )
        return unwrap_list(response.data)

    async def kick(
        self,
        community_id: str,
        container_id: str,
        user_id: str,
    ):
        return (
            await self.client.high.web_rtc.kick(
                community_id=community_id,
                container_id=container_id,
                user_id=user_id,
            )
        ).data

    async def set_member_mute_deafen(
        self,
        community_id: str,
        container_id: str,
        user_id: str,
        *,
        muted: Optional[bool] = None,
        deafened: Optional[bool] = None,
    ):
        kwargs = {
            "community_id": community_id,
            "container_id": container_id,
            "user_id": user_id,
        }
        if muted is not None:
            kwargs["is_muted"] = muted
        if deafened is not None:
            kwargs["is_deafened"] = deafened

        return (
            await self.client.high.web_rtc.set_mute_and_deafen_other(
                **kwargs
            )
        ).data


class FriendshipGroupManager:
    def __init__(self, client) -> None:
        self.client = client

    async def list(self):
        """Your friendship groups, as a list."""
        response = await self.client.high.friendship_group.list()
        return unwrap_list(response.data)

    async def create(self, name: str):
        return (
            await self.client.high.friendship_group.create(
                name=name,
            )
        ).data

    async def edit(self, group_id: str, name: str):
        return (
            await self.client.high.friendship_group.edit(
                id=group_id,
                name=name,
            )
        ).data

    async def delete(self, group_id: str):
        return (
            await self.client.high.friendship_group.delete(
                id=group_id,
            )
        ).data

    async def move(self, group_id: str, *, before_group_id: str):
        """Move a group so it sits before ``before_group_id``.

        ``before_group_id`` is required -- Root applies a NotEmptyValidator to
        ``BeforeFriendshipGroupId``. It used to default to None and be omitted,
        which made the request fail rather than mean "move to the end", so the
        optionality was misleading.
        """
        if not before_group_id:
            raise ValueError(
                "before_group_id is required: Root rejects a move without a "
                "reference group. Pass the id of the group to sit before."
            )
        return (
            await self.client.high.friendship_group.move(
                id=group_id,
                before_friendship_group_id=before_group_id,
            )
        ).data
