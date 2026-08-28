"""Live budgets: how many round trips does each operation actually make?

The live suite had no performance coverage at all. The obvious way to add it
is to time things, and that is the wrong way -- wall clock on a live API moves
with the network, so a timing assertion is either so loose it catches nothing
or so tight it fails on a bad afternoon.

**Request count is the metric that matters and the one that is deterministic.**
A round trip costs ~190 ms almost regardless of payload, so cost is round
trips; and the count does not vary with the weather. Every performance bug
found in this project has been a request-count bug:

  * ``get_members_detailed`` making 316 requests where 64 would do (63.7 s -> 3.6 s)
  * ``list_communities`` making 10 where 1 would do, on the *default* path
  * ``clone_community`` spending 32 of 85 requests re-reading objects whose
    ids the create response already returned
  * ``watch_unread`` fetching every community twice per sweep

Each test below pins a budget with headroom, so ordinary variation passes and
an N+1 regression fails. ``devscripts/perfmap.py`` prints the same numbers as
a ranked table when you want to go looking rather than guarding.
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.live


class Budget:
    """Counts the requests made inside the block."""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    def __enter__(self):
        self.client.transport.stats.reset()
        return self

    def __exit__(self, *exc):
        self.calls = self.client.transport.stats.totals()["calls"]
        return False


class TestReadBudgets:
    """The everyday reads. Each of these should be a single round trip."""

    async def test_list_communities_is_one_request(self, client):
        """The regression that prompted this file.

        ``CommunityManager.list`` took ``refresh_communities``'s ``expand=True``
        default, so the most basic call in the SDK fetched a full
        ``CommunityGetExtended`` per community -- each one the community's
        entire member, role and channel dump -- to answer a question whose
        return type is plain ``Community``. Measured at 10 requests for 9
        communities before the fix.
        """
        with Budget(client) as budget:
            communities = await client.list_communities()
        assert communities, "the account is in no communities; cannot judge"
        assert budget.calls == 1, (
            f"list_communities() made {budget.calls} requests for "
            f"{len(communities)} communities. It should be one ListMine -- "
            "anything more means it is expanding communities the caller did "
            "not ask for."
        )

    async def test_community_detail_is_one_request(self, client, sandbox):
        with Budget(client) as budget:
            await client.community_detail(sandbox.community_id, refresh=True)
        assert budget.calls == 1, (
            f"community_detail made {budget.calls} requests, expected 1"
        )

    async def test_a_cached_detail_costs_nothing(self, client, sandbox):
        """The point of the cache: the second read is free."""
        await client.community_detail(sandbox.community_id, refresh=True)
        with Budget(client) as budget:
            await client.community_detail(sandbox.community_id)
        assert budget.calls == 0, (
            f"a cached community_detail still made {budget.calls} request(s)"
        )

    async def test_one_page_of_history_is_one_request(self, client, sandbox):
        with Budget(client) as budget:
            await client.messages.list(
                sandbox.channel.id, community_id=sandbox.community_id
            )
        assert budget.calls == 1

    async def test_sync_cache_readers_make_no_requests(self, client, sandbox):
        """They are documented as synchronous cache reads; prove it."""
        await client.community_detail(sandbox.community_id, refresh=True)
        with Budget(client) as budget:
            client.get_community(sandbox.community_id)
            client.get_channel(sandbox.channel.id)
            client.messages_for_container(sandbox.channel.id)
        assert budget.calls == 0


class TestBatchingBudgets:
    """Operations that fan out. The budget is what the batching promises."""

    async def test_get_profiles_is_one_request_for_any_number(self, client, sandbox):
        """Laddered live to 4,000 ids in a single request.

        LLMS.md used to claim "one request per 100 ids", which invites a
        hand-rolled chunking loop that turns one request into N.
        """
        members = await client.get_members(sandbox.community_id)
        ids = [m.user_id for m in members] or [(await client.whoami()).id]
        with Budget(client) as budget:
            profiles = await client.get_profiles(ids)
        assert budget.calls == 1, (
            f"get_profiles({len(ids)} ids) made {budget.calls} requests; "
            "it is one request for any number of ids -- do not chunk it"
        )
        assert profiles is not None

    async def test_members_detailed_batches_rather_than_looping(self, client, sandbox):
        """members + ceil(n / batch_size) profile requests, and no more."""
        from rootpy import RootClient

        import inspect

        batch = inspect.signature(
            RootClient.get_members_detailed
        ).parameters["batch_size"].default

        with Budget(client) as budget:
            detailed = await client.get_members_detailed(
                sandbox.community_id, refresh=True
            )
        ceiling = 1 + max(1, math.ceil(len(detailed) / batch)) + 1   # +1 slack
        assert budget.calls <= ceiling, (
            f"get_members_detailed made {budget.calls} requests for "
            f"{len(detailed)} member(s) at batch_size={batch}; at most "
            f"{ceiling} expected. More than that means it is fetching "
            "profiles one member at a time again."
        )

    async def test_asset_resolve_chunks_rather_than_serialising(self, client, sandbox):
        """One request per chunk, plus retries for URIs Root refuses.

        A URI Root dislikes rejects its whole chunk and forces a per-URI
        refetch, so the budget allows for that -- but not for the chunking
        being skipped entirely.
        """
        import inspect

        from rootpy.services.assets import AssetService

        detailed = await client.get_members_detailed(sandbox.community_id)
        uris = [
            v for m in detailed
            for v in (getattr(m, "profile_picture_uri", None),
                      getattr(m, "banner_uri", None))
            if isinstance(v, str) and v.startswith("root://")
        ]
        if len(uris) < 4:
            pytest.skip(f"only {len(uris)} asset URIs in the sandbox")

        chunk = inspect.signature(
            AssetService.get
        ).parameters["chunk_size"].default

        with Budget(client) as budget:
            await client.assets.resolve(uris)
        # worst case: every chunk holds a bad URI -> chunk request + one each
        worst = math.ceil(len(uris) / chunk) + len(uris)
        assert budget.calls <= worst, (
            f"resolve({len(uris)} uris) made {budget.calls} requests, above "
            f"the worst case of {worst} for chunk_size={chunk}"
        )


class TestNoAccidentalRefetch:
    """Round trips nobody asked for."""

    async def test_sending_a_message_is_one_request(self, client, sandbox):
        with Budget(client) as budget:
            await client.message(sandbox.channel, "budget check")
        assert budget.calls == 1, (
            f"sending one message cost {budget.calls} requests"
        )

    async def test_whoami_is_cached_after_login(self, client):
        await client.whoami()
        with Budget(client) as budget:
            await client.whoami()
        assert budget.calls == 0, (
            "whoami() re-fetched despite being cached; pass refresh=True when "
            "you actually want a round trip"
        )
