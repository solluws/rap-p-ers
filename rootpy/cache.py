"""A small in-memory cache of users seen in traffic.

Typed events want to say *who* did something, not just print a uuid -- but
looking a user up costs a request, and events fire faster than you'd want to
make requests. So instead we remember everyone we see: authors of messages,
members returned by community fetches, users named in notifications.

Nothing here ever performs a request. A miss simply returns ``None`` and the
caller falls back to the id, which keeps event handling fast and predictable.

    client.cache.remember_user(user_id, "someone")
    client.cache.username(user_id)          -> "someone" or None
    client.cache.stats()                    -> {"users": 128, ...}
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional


class LRUCache:
    """Dict with a size cap, evicting whatever was used least recently."""

    def __init__(self, maxsize: int = 5000) -> None:
        self.maxsize = max(1, int(maxsize))
        self._data: "OrderedDict[str, Any]" = OrderedDict()

    def get(self, key: str, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def pop(self, key: str, default=None):
        return self._data.pop(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    # dict-style access, so an LRUCache can stand in for a plain dict
    def __setitem__(self, key: str, value) -> None:
        self.set(key, value)

    def __getitem__(self, key: str):
        if key not in self._data:
            raise KeyError(key)
        self._data.move_to_end(key)
        return self._data[key]

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def clear(self) -> None:
        self._data.clear()


class StateCache:
    """Remembers users, members, and channel/role names seen in traffic."""

    def __init__(
        self,
        *,
        max_users: int = 5000,
        max_communities: int = 500,
        max_members_per_community: int = 2000,
    ) -> None:
        self.users = LRUCache(max_users)              # user_id -> {"username": ...}
        # Bounded on both axes. The per-community bucket was always capped,
        # but the community_id -> bucket dict was a plain dict that never
        # evicted, so a long-running client accumulated one 2000-entry cache
        # per community it ever saw and never gave any of it back. That is
        # invisible in short runs and the whole point of a process meant to
        # stay up for days.
        self.max_members_per_community = max_members_per_community
        self.members: LRUCache = LRUCache(max_communities)
        self._hits = 0
        self._misses = 0

    # --- users ----------------------------------------------------------- #
    def remember_user(self, user_id: Optional[str], username: Optional[str] = None,
                      **extra) -> None:
        """Record a user we've seen. Safe to call with partial information."""
        if not user_id:
            return
        record = dict(self.users.get(user_id) or {})
        if username:
            record["username"] = username
        record.update({k: v for k, v in extra.items() if v is not None})
        # Always store, even with nothing but the id: knowing we've *seen* a
        # user is useful on its own, and the name gets merged in later if any
        # source provides one. (Root's member/message objects carry only ids --
        # usernames arrive via notifications and user fetches.)
        self.users.set(user_id, record)

    def user(self, user_id: Optional[str]) -> Optional[dict]:
        if not user_id:
            return None
        found = self.users.get(user_id)
        if found is None:
            self._misses += 1
        else:
            self._hits += 1
        return found

    def username(self, user_id: Optional[str]) -> Optional[str]:
        """The username we've seen for this id, or None."""
        record = self.user(user_id)
        return record.get("username") if record else None

    # --- members --------------------------------------------------------- #
    def remember_member(self, community_id: Optional[str], member) -> None:
        if not community_id or member is None:
            return
        user_id = getattr(member, "user_id", None) or getattr(member, "id", None)
        if not user_id:
            return
        bucket = self.members.get(community_id)
        if bucket is None:
            bucket = LRUCache(self.max_members_per_community)
            self.members[community_id] = bucket
        bucket.set(user_id, member)
        self.remember_user(
            user_id,
            getattr(member, "username", None) or getattr(member, "nickname", None),
        )

    def member(self, community_id: Optional[str], user_id: Optional[str]):
        if not community_id or not user_id:
            return None
        bucket = self.members.get(community_id)
        return bucket.get(user_id) if bucket else None

    # --- bulk ingestion --------------------------------------------------- #
    def ingest_community(self, extended) -> None:
        """Absorb everything useful from a GetExtended result."""
        community = getattr(extended, "community", None)
        community_id = getattr(community, "id", None)
        for member in getattr(extended, "members", ()) or ():
            self.remember_member(community_id, member)

    def ingest_message(self, message) -> None:
        """Remember a message's author."""
        self.remember_user(getattr(message, "user_id", None))

    def ingest_users(self, users: Iterable) -> None:
        for user in users or ():
            self.remember_user(
                getattr(user, "id", None), getattr(user, "username", None)
            )

    # --- housekeeping ----------------------------------------------------- #
    def stats(self) -> dict:
        """Cache size and hit rate -- handy when tuning."""
        total = self._hits + self._misses
        return {
            "users": len(self.users),
            "communities_with_members": len(self.members),
            "members": sum(len(bucket) for bucket in self.members.values()),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }

    def clear(self) -> None:
        self.users.clear()
        self.members.clear()
        self._hits = self._misses = 0
