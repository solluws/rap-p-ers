"""Typed service layer.

``CallService`` is intentionally absent from the eager imports: the voice
module pulls in ``urllib.request``, ``http.cookiejar`` and ``subprocess``,
which text-only programs should not pay for. Import it directly
(``from rootpy.services.calls import CallService``) or use ``client.calls``,
which builds it on first access.
"""

from .community_admin import CommunityAdminService
from .assets import AssetService
from .communities import CommunityService
from .direct_messages import DirectMessage, DirectMessageService
from .messages import MessageService
from .users import UserService

__all__ = [
    "CommunityAdminService",
    "AssetService",
    "CommunityService",
    "DirectMessage",
    "DirectMessageService",
    "MessageService",
    "UserService",
]
