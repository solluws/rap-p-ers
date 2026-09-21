from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .permissions import (
    ActionNotApplicable,
    MissingPermissions,
    PermissionStateUnavailable,
)

if TYPE_CHECKING:
    from .client import RootClient
    from .commands import Context


@dataclass(frozen=True)
class User:
    client: "RootClient"
    id: str
    username: Optional[str] = None

    @property
    def mention(self) -> str:
        """This user, written the way Root writes mentions in message content.

        ``[@name](root://user/<id>)`` -- a markdown link, not ``<@id>``. This
        is the shape ``rootpy.commands.USER_MENTION_RE`` parses, so a mention
        built here round-trips.

        Mirrors :attr:`rootpy.models.Channel.mention`.
        """
        from .models import build_user_mention

        return build_user_mention(self.id, self.username)

    async def call(self):
        return await self.client.calls.call_user(self)

    async def dm(
        self,
        content: str,
        *,
        attachment_token_uris=None,
        parent_message_ids=None,
        delete_after: Optional[float] = None,
    ):
        """Open/reuse the direct-message container and send a message.

        ``User.dm`` delegates to :attr:`RootClient.dm`, which performs the
        correct DirectMessage.Find/Create flow before sending into the actual
        DM container.
        """
        return await self.client.dm.send(
            self,
            content,
            attachment_token_uris=attachment_token_uris,
            parent_message_ids=parent_message_ids,
            delete_after=delete_after,
        )

    def _is_self(self) -> bool:
        return self.client.user_id == self.id

    def _active_target(self) -> tuple[str, Optional[str]]:
        return self.client.calls.require_active_target()

    async def _require_other_permission(
        self,
        ctx: "Context",
        permission: str,
    ) -> tuple[str, str]:
        container_id, community_id = self._active_target()
        if community_id is None:
            raise ActionNotApplicable(
                "Moderating another user requires an active community call"
            )

        await self.client.ensure_community_cached(community_id)
        permissions = self.client.permissions_for(
            ctx.author_id,
            container_id,
        )
        if permissions is None:
            raise PermissionStateUnavailable(
                "The SDK cannot verify the caller's permissions "
                "for the active voice channel"
            )
        if not permissions.has(permission):
            raise MissingPermissions(permission)
        return container_id, community_id

    async def mute(self, ctx: "Context"):
        container_id, community_id = self._active_target()
        if self._is_self():
            return await self.client.calls.set_mute_and_deafen(
                container_id=container_id,
                community_id=community_id,
                muted=True,
            )

        container_id, community_id = await self._require_other_permission(
            ctx,
            "channel_voice_mute_other",
        )
        return await self.client.calls.set_mute_and_deafen_other(
            container_id=container_id,
            community_id=community_id,
            user_id=self.id,
            muted=True,
        )

    async def unmute(self, ctx: "Context"):
        container_id, community_id = self._active_target()
        if self._is_self():
            return await self.client.calls.set_mute_and_deafen(
                container_id=container_id,
                community_id=community_id,
                muted=False,
            )

        container_id, community_id = await self._require_other_permission(
            ctx,
            "channel_voice_mute_other",
        )
        return await self.client.calls.set_mute_and_deafen_other(
            container_id=container_id,
            community_id=community_id,
            user_id=self.id,
            muted=False,
        )

    async def deafen(self, ctx: "Context"):
        container_id, community_id = self._active_target()
        if self._is_self():
            return await self.client.calls.set_mute_and_deafen(
                container_id=container_id,
                community_id=community_id,
                deafened=True,
            )

        container_id, community_id = await self._require_other_permission(
            ctx,
            "channel_voice_deafen_other",
        )
        return await self.client.calls.set_mute_and_deafen_other(
            container_id=container_id,
            community_id=community_id,
            user_id=self.id,
            deafened=True,
        )

    async def undeafen(self, ctx: "Context"):
        container_id, community_id = self._active_target()
        if self._is_self():
            return await self.client.calls.set_mute_and_deafen(
                container_id=container_id,
                community_id=community_id,
                deafened=False,
            )

        container_id, community_id = await self._require_other_permission(
            ctx,
            "channel_voice_deafen_other",
        )
        return await self.client.calls.set_mute_and_deafen_other(
            container_id=container_id,
            community_id=community_id,
            user_id=self.id,
            deafened=False,
        )

    async def kick(self, ctx: "Context"):
        if self._is_self():
            raise ActionNotApplicable(
                "Use ctx.hangup() to leave the active call"
            )

        container_id, community_id = await self._require_other_permission(
            ctx,
            "channel_voice_kick",
        )
        return await self.client.calls.kick(
            container_id=container_id,
            community_id=community_id,
            user_id=self.id,
        )
