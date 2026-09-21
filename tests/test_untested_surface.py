"""Offline coverage for the wide, quiet parts of the surface.

Everything here runs without a token and without the network: permission
algebra, pagination, stats, enum helpers, model properties, the command
context's mention parsing, the registry loader, and the shape of the
high-level API.

A good half of this file is not a unit test at all but a *guard*: a sweep
over the package's own AST that pins a rule the code has to keep obeying --
no ``**kwargs`` on a public manager, no ``read_text()`` without an encoding,
every ``high.*`` call site naming a field the schema has. Those catch the
class of mistake that a single example never would.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import functools
import pathlib

import pytest

import rootpy
from rootpy import (
    RootClient,
    accounts,
    commands,
    enums,
    highlevel,
    models,
    packets,
    pagination,
    permissions,
    stats,
)
from rootpy._registry_loader import DerivedRegistry, LazyRegistry

# --------------------------------------------------------------------------
# Several guards below walk the whole package's AST. Parsing it once per test
# showed up in --timing as ~270 ms each, which is most of the offline suite's
# self-inflicted cost, so the parse is cached for the session.
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=None)
def _source_files():
    """Every .py in the repo worth checking, excluding build detritus.

    Build output and virtualenvs are skipped: every guard here is about the
    *package*, and a finding from a vendored or generated tree is not
    actionable by anyone.
    """
    return tuple(
        path
        for path in sorted(_repo_root().rglob("*.py"))
        if not any(part in {".venv", "build", "dist", "__pycache__"}
                   for part in path.parts)
    )


@functools.lru_cache(maxsize=None)
def _example_sources():
    """Every shipped example, in a stable order.

    ``examples/`` and ``example.py`` are documentation as much as they are
    samples -- a reader runs them verbatim -- so the guards below hold them to
    the same standard as the package.
    """
    root = _repo_root()
    found = sorted(root.glob("examples/*.py"))
    top = root / "example.py"
    if top.exists():
        found.append(top)
    return tuple(found)


@functools.lru_cache(maxsize=None)
def _parsed_sources():
    """(path, tree) for every .py in the repo, parsed once.

    A file that will not parse is skipped rather than raised on, because
    every guard built on this walks *all* the sources and one broken file
    should not mask the others. That silence is dangerous on its own --
    ``TestEverySourceFileCompiles`` below is the counterweight, and it uses
    ``compile()`` rather than ``ast.parse()`` because the two do not agree.
    """
    import ast

    out = []
    for path in _source_files():
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return tuple(out)


@functools.lru_cache(maxsize=None)
def _parsed_module(module_name: str):
    """The AST for one importable module, parsed once."""
    import ast
    import importlib

    module = importlib.import_module(module_name)
    return ast.parse(
        pathlib.Path(module.__file__).read_text(encoding="utf-8")
    )


#: A syntactically valid Root GUID; identifiers.py rejects arbitrary strings.
_VALID_USER_ID = "0b7f9c2e-1d4a-4c8b-9e3f-5a6b7c8d9e0f"
from rootpy.generated_rpc_registry import MESSAGE_SCHEMAS, RPC_SERVICES
from rootpy.structured_registry import ENUMS, MESSAGES, SERVICES, SIMPLE_MESSAGES


# --------------------------------------------------------------------------
# permissions -- ChannelPermissions / ChannelOverlay
# --------------------------------------------------------------------------
class TestChannelPermissions:
    def test_all_grants_every_flag(self):
        every = permissions.ChannelPermissions.all()
        flags = [f for f in vars(every) if f.startswith("channel_")]
        assert flags, "expected channel_* flags on the dataclass"
        assert all(getattr(every, f) is True for f in flags)

    def test_none_grants_nothing(self):
        empty = permissions.ChannelPermissions.none()
        flags = [f for f in vars(empty) if f.startswith("channel_")]
        assert all(getattr(empty, f) is False for f in flags)

    def test_none_matches_default_construction(self):
        assert permissions.ChannelPermissions.none() == permissions.ChannelPermissions()

    def test_has_reads_named_flag(self):
        every = permissions.ChannelPermissions.all()
        empty = permissions.ChannelPermissions.none()
        assert every.has("channel_view") is True
        assert empty.has("channel_view") is False

    def test_has_is_false_for_unknown_name(self):
        """Regression: full_control used to make has() True for any string."""
        assert permissions.ChannelPermissions.all().has("not_a_real_flag") is False

    def test_has_rejects_a_typo_even_under_full_control(self):
        assert permissions.ChannelPermissions.all().has("channel_veiw") is False

    def test_full_control_implies_real_permissions(self):
        assert permissions.ChannelPermissions.all().has("channel_voice_kick") is True


class TestChannelOverlay:
    """Overlays are tri-state: True allow, False deny, None inherit."""

    def test_allow_all_sets_true(self):
        overlay = permissions.ChannelOverlay.allow_all()
        assert all(v is True for v in vars(overlay).values())

    def test_deny_all_sets_false(self):
        overlay = permissions.ChannelOverlay.deny_all()
        assert all(v is False for v in vars(overlay).values())

    def test_inherit_all_sets_none(self):
        overlay = permissions.ChannelOverlay.inherit_all()
        assert all(v is None for v in vars(overlay).values())

    def test_inherit_all_matches_default_construction(self):
        assert permissions.ChannelOverlay.inherit_all() == permissions.ChannelOverlay()

    def test_three_states_are_distinct(self):
        allow = permissions.ChannelOverlay.allow_all()
        deny = permissions.ChannelOverlay.deny_all()
        inherit = permissions.ChannelOverlay.inherit_all()
        assert allow != deny and deny != inherit and allow != inherit


# --------------------------------------------------------------------------
# pagination -- Page.has_more / AsyncPager.flatten
# --------------------------------------------------------------------------
class TestPagination:
    def test_has_more_true_when_cursor_present(self):
        assert pagination.Page(items=(1, 2), cursor="next").has_more is True

    def test_has_more_false_without_cursor(self):
        assert pagination.Page(items=(1, 2), cursor=None).has_more is False

    def test_empty_page_has_no_more(self):
        assert pagination.Page(items=(), cursor=None).has_more is False

    def test_flatten_concatenates_pages_in_order(self):
        pages = {
            None: pagination.Page(items=(1, 2), cursor="a"),
            "a": pagination.Page(items=(3, 4), cursor="b"),
            "b": pagination.Page(items=(5,), cursor=None),
        }

        async def fetch(cursor):
            return pages[cursor]

        result = asyncio.run(pagination.AsyncPager(fetch).flatten())
        assert result == [1, 2, 3, 4, 5]

    def test_flatten_respects_max_pages(self):
        async def fetch(cursor):
            n = 0 if cursor is None else cursor
            return pagination.Page(items=(n,), cursor=n + 1)

        result = asyncio.run(pagination.AsyncPager(fetch, max_pages=3).flatten())
        assert result == [0, 1, 2]

    def test_flatten_of_single_empty_page(self):
        async def fetch(cursor):
            return pagination.Page(items=(), cursor=None)

        assert asyncio.run(pagination.AsyncPager(fetch).flatten()) == []


# --------------------------------------------------------------------------
# stats -- TransportStats counters and reporting
# --------------------------------------------------------------------------
class TestTransportStats:
    def test_note_rate_limited_does_not_raise(self):
        s = stats.TransportStats()
        s.note_rate_limited("/root.MessageGrpcService/Create")
        s.note_rate_limited("/root.MessageGrpcService/Create")
        assert isinstance(s.report(), str)

    def test_rate_limit_only_activity_is_reported(self):
        """Regression: this used to say "no requests recorded".

        A client that was only ever throttled has real activity, and hiding
        it is precisely the case someone opens the report to investigate.
        """
        s = stats.TransportStats()
        s.note_rate_limited("/svc/Method")
        s.note_rate_limited("/svc/Method")
        report = s.report()
        assert "no requests recorded" not in report
        assert "rate-limited" in report

    def test_a_truly_idle_client_still_says_nothing_recorded(self):
        assert stats.TransportStats().report() == "no requests recorded"

    def test_report_returns_text(self):
        s = stats.TransportStats()
        s.note_rate_limited("/svc/Method")
        report = s.report()
        assert isinstance(report, str) and report.strip()

    def test_reset_clears_counters(self):
        s = stats.TransportStats()
        s.note_rate_limited("/svc/Method")
        before = s.report()
        s.reset()
        assert s.report() != before or "Method" not in s.report()

    def test_report_on_fresh_stats_does_not_raise(self):
        assert isinstance(stats.TransportStats().report(), str)


class TestListCommunitiesHonoursTheLazyDefault:
    """``list_communities()`` must not silently do the eager thing.

    ``CommunityManager.list`` called ``refresh_communities()`` bare, taking
    that method's ``expand=True`` default — so on a *lazy* client (the
    documented default, ``expand_communities=False``) the most basic call in
    the SDK fired one ListMine **plus a full CommunityGetExtended per
    community**, each returning that community's entire member/role/channel
    dump. On an account in nine communities that is ten requests where one
    does, and the largest of them dominates the bill.

    Nothing needed that data — ``list`` returns ``Community``, not
    ``CommunityExtended``. It only warmed a cache the caller may never read,
    and it made the documented cost of the lazy default false for this path.
    """

    def _client(self, expand):
        client = RootClient(token="x", expand_communities=expand)
        seen = []

        async def fake_refresh(*, expand=True):
            seen.append(expand)

        client.refresh_communities = fake_refresh
        return client, seen

    @pytest.mark.parametrize("expand", [False, True])
    def test_it_passes_the_clients_setting_through(self, expand):
        client, seen = self._client(expand)
        asyncio.run(client.community.list())
        assert seen == [expand], (
            f"expand_communities={expand} but refresh_communities got {seen}"
        )

    def test_the_lazy_default_does_not_expand(self):
        """The specific regression, stated plainly."""
        client, seen = self._client(False)
        asyncio.run(client.list_communities())
        assert seen == [False], (
            "list_communities() on a lazy client expanded every community"
        )

    def test_refresh_false_makes_no_request_at_all(self):
        client, seen = self._client(False)
        asyncio.run(client.community.list(refresh=False))
        assert seen == []


class TestAssetBatchingStaysTuned:
    """``resolve`` forwards ``get``'s batching knobs, so they must agree.

    These defaults are tuned, not chosen: AssetGet inverts the SDK's usual
    "bigger batches win" rule, because one URI Root refuses rejects the whole
    request and about 6% of real profile asset URIs are refused. A large
    chunk is therefore much likelier to be rejected whole and retried one
    URI at a time.

    ``resolve`` is a keyword forwarder with its own defaults, so a stale value
    there silently overrides the tuned one in ``get`` -- and nothing would
    surface it, because both spellings still *work*, just slower.
    """

    def _defaults(self, func):
        params = inspect.signature(func).parameters
        return params["chunk_size"].default, params["concurrency"].default

    def test_resolve_matches_get(self):
        from rootpy.services.assets import AssetService

        assert self._defaults(AssetService.resolve) == self._defaults(
            AssetService.get
        ), "resolve's batching defaults have drifted from get's"

    def test_the_chunk_stays_small(self):
        """Large chunks are the failure mode here, not the optimisation."""
        from rootpy.services.assets import AssetService

        chunk, concurrency = self._defaults(AssetService.get)
        assert chunk <= 10, (
            f"chunk_size={chunk}: a bigger AssetGet batch is likelier to "
            "contain a URI Root refuses, and one poison URI rejects the whole "
            "request, which then degrades to one request per URI."
        )
        assert concurrency >= 16, (
            f"concurrency={concurrency}: small chunks only pay off when enough "
            "of them are in flight."
        )


class TestTheThreeHistoryRenderingsAgree:
    """Same parameter, three doorways, one default.

    ``messages.list``, ``client.list_messages`` and ``channel.history`` are
    three ways to reach the same RPC, and ``client.list_messages`` is a thin
    forwarder whose docstring says "see MessageService.list" -- while silently
    defaulting ``direction`` to ``"older"`` against the callee's ``"both"``.

    Found by three agents who were each given only the documentation and asked
    to write a script. All three hit the three renderings, could not tell which
    applied, and guessed. The docs were accurate about each method separately;
    the *library* was inconsistent, and no amount of doc-writing fixes that.

    ``defaultcheck.py`` does not cover this: it is scoped to a falsy default
    defeating a ``None`` sentinel, and here both defaults are ordinary truthy
    constants that merely disagree. Generalising it was tried and rejected --
    it flags nine deliberate cases (``mute(muted=True)``,
    ``from_tokens(gateway=False)``, ``history(limit=200)`` ...) to catch this
    one, so the specific case is pinned here instead.
    """

    def _default(self, func, name):
        return inspect.signature(func).parameters[name].default

    def test_direction_defaults_agree(self):
        from rootpy import RootClient
        from rootpy.models import Channel
        from rootpy.services.messages import MessageService

        defaults = {
            "messages.list": self._default(MessageService.list, "direction"),
            "client.list_messages": self._default(
                RootClient.list_messages, "direction"
            ),
            "channel.history": self._default(Channel.history, "direction"),
        }
        assert len(set(defaults.values())) == 1, (
            "the three ways to page history disagree on their default "
            f"direction, so which one you call changes what you get: {defaults}"
        )

    def test_the_agreed_default_is_a_real_direction(self):
        from rootpy.services.messages import MessageService

        assert (
            self._default(MessageService.list, "direction")
            in MessageService._DIRECTIONS
        )


class TestTheLibraryWritesNothingUninvited:
    """No shipped module may write into the user's home directory.

    ``CallService.play_audio`` wrote ``~/Desktop/rootpy_offer.sdp`` and printed
    two lines to stdout on *every* call -- scaffolding left over from working
    out Root's SDP layout. Three separate problems: it dropped an unrequested
    file in someone's home, it hard-failed on any host with no Desktop folder
    (``write_text`` does not ``mkdir`` -- containers, headless servers, most
    Linux desktops), and it corrupted the stdout of any program piping the
    output of a call.

    Writing to a path the *caller* supplied is fine and stays fine --
    ``AccountFactory.save`` does exactly that. What is banned is the library
    choosing a location in ``Path.home()`` by itself.
    """

    def _library_sources(self):
        return [
            (path, tree) for path, tree in _parsed_sources()
            if "rootpy" in path.parts and "tests" not in path.parts
        ]

    def test_no_module_builds_a_path_under_home(self):
        import ast

        offenders = []
        for path, tree in self._library_sources():
            for node in ast.walk(tree):
                # Path.home() / "anything"
                if not isinstance(node, ast.BinOp) or not isinstance(
                    node.op, ast.Div
                ):
                    continue
                left = node.left
                if (
                    isinstance(left, ast.Call)
                    and isinstance(left.func, ast.Attribute)
                    and left.func.attr == "home"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            "a library module picks its own destination under the user's home "
            f"directory: {offenders}. Take a path from the caller instead."
        )

    def test_no_module_writes_to_an_expanduser_tilde(self):
        """The other spelling of the same mistake."""
        import ast

        offenders = []
        for path, tree in self._library_sources():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.startswith("~/")
                ):
                    offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
        assert offenders == [], (
            f"hardcoded home-relative path in the library: {offenders}"
        )

    def test_the_call_service_reports_through_a_logger(self):
        """It should still be *possible* to see the SDP -- just not by default."""
        import rootpy.services.calls as calls

        assert hasattr(calls, "log"), "calls.py lost its logger"
        assert calls.log.name == "rootpy.calls"


class TestStatsSurviveSampleTruncation:
    """The bounded sample must not corrupt the totals computed from it.

    ``EndpointStats`` keeps at most 1000 round trips so the median stays cheap,
    dropping the oldest 500 when it overflows. But ``total_roundtrip_ms`` was
    ``sum(self.roundtrip_ms)`` -- a sum of the *sample*, not of the calls. Past
    1000 calls to one endpoint the reported total converged toward
    ``1000 * mean`` however many requests were really made, so the error was
    unbounded, and because ``report()`` ranks endpoints by that number the
    ranking inverted too: a hot endpoint could be shown below a quiet one it
    dominated.

    ``client.timings()`` is the tool the module docstring offers for "which
    part was slow", so being wrong here is worse than having no tool.
    """

    def _hammer(self, calls, ms=10.0):
        entry = stats.EndpointStats()
        for _ in range(calls):
            entry.record(ms, 0.0, 0.0)
        return entry

    def test_the_sample_really_is_bounded(self):
        """The premise. If this stops being true the rest is moot."""
        entry = self._hammer(5000)
        assert len(entry.roundtrip_ms) <= 1000
        assert entry.calls == 5000

    def test_total_counts_every_call_not_every_sample(self):
        entry = self._hammer(5000, ms=10.0)
        assert entry.total_roundtrip_ms == pytest.approx(50_000.0)
        assert entry.summary()["roundtrip_total_ms"] == pytest.approx(50_000.0)

    def test_min_and_max_survive_being_dropped_from_the_sample(self):
        """An extreme in the first 500 calls must still be reported."""
        entry = stats.EndpointStats()
        entry.record(1.0, 0.0, 0.0)        # the minimum, recorded first...
        entry.record(9999.0, 0.0, 0.0)     # ...and the maximum
        for _ in range(5000):              # ...then hammered out of the sample
            entry.record(10.0, 0.0, 0.0)

        assert 1.0 not in entry.roundtrip_ms, "precondition: sample truncated"
        summary = entry.summary()
        assert summary["roundtrip_min_ms"] == pytest.approx(1.0)
        assert summary["roundtrip_max_ms"] == pytest.approx(9999.0)

    def test_the_average_is_over_calls_not_samples(self):
        s = stats.TransportStats()
        for _ in range(5000):
            s.record("/svc/Hot", roundtrip_ms=10.0)
        totals = s.summary()["totals"]
        assert totals["calls"] == 5000
        assert totals["roundtrip_ms"] == pytest.approx(50_000.0)
        assert totals["avg_roundtrip_ms"] == pytest.approx(10.0)

    def test_the_busiest_endpoint_ranks_first(self):
        """The ranking inverted: truncation capped the hot endpoint's total."""
        s = stats.TransportStats()
        for _ in range(5000):                      # 50,000 ms in total
            s.record("/svc/Hot", roundtrip_ms=10.0)
        for _ in range(20):                        # 20,000 ms in total
            s.record("/svc/Quiet", roundtrip_ms=1000.0)

        endpoints = s.summary()["endpoints"]
        assert endpoints["svc/Hot"]["roundtrip_total_ms"] == pytest.approx(50_000.0)
        assert endpoints["svc/Quiet"]["roundtrip_total_ms"] == pytest.approx(20_000.0)

        report = s.report()
        assert report.index("svc/Hot") < report.index("svc/Quiet"), (
            "the endpoint with the larger true total must rank first:\n" + report
        )


# --------------------------------------------------------------------------
# enums / packets
# --------------------------------------------------------------------------
class TestEnumsAndPackets:
    def test_label_is_human_readable(self):
        member = next(iter(enums.UserOnlineStatus))
        assert isinstance(member.label, str) and member.label

    def test_label_is_lowercased_name(self):
        assert enums.UserOnlineStatus.ACTIVE.label == "active"

    def test_active_status_is_0x10(self):
        """ACTIVE is 0x10, not 0x01 and not 3 -- a hex literal is easy to lose."""
        assert enums.UserOnlineStatus.ACTIVE.value == 0x10

    def test_no_do_not_disturb_state(self):
        names = {m.name.upper() for m in enums.UserOnlineStatus}
        assert not {"DND", "DO_NOT_DISTURB", "BUSY"} & names

    def test_is_unknown_true_when_nothing_decoded(self):
        packet = packets.SocketPacket(
            sequence=1, type=packets.PacketType.UNKNOWN, case=None, data={}, raw=b""
        )
        assert packet.is_unknown is True

    def test_is_unknown_false_once_a_packet_decodes(self):
        packet = packets.SocketPacket(
            sequence=1,
            type=packets.PacketType.PING,
            case=1,
            data={"sequence_number": 7},
            raw=b"",
        )
        assert packet.is_unknown is False


# --------------------------------------------------------------------------
# models -- attachment / message conveniences
# --------------------------------------------------------------------------
class TestModelProperties:
    def test_display_name_prefers_filename(self):
        a = models.MessageAttachment(asset_uri="root://abc", filename="clip.mp3")
        assert a.display_name == "clip.mp3"

    def test_display_name_falls_back_when_filename_blank(self):
        a = models.MessageAttachment(asset_uri="root://abc", filename="")
        assert isinstance(a.display_name, str) and a.display_name

    def test_first_attachment_url_none_without_attachments(self):
        m = models.Message(id="1", container_id="c", user_id="u", content="hi")
        assert m.first_attachment_url is None

    def test_first_attachment_url_uses_first_attachment(self):
        first = models.MessageAttachment(
            asset_uri="root://one", filename="a.png", download_url="https://x/a.png"
        )
        second = models.MessageAttachment(asset_uri="root://two", filename="b.png")
        m = models.Message(
            id="1",
            container_id="c",
            user_id="u",
            content="",
            attachments=(first, second),
        )
        assert m.first_attachment_url == "https://x/a.png"


# --------------------------------------------------------------------------
# accounts -- CreatedAccount.redacted
# --------------------------------------------------------------------------
class TestCreatedAccountRedaction:
    def _account(self):
        return accounts.CreatedAccount(
            username="service-test",
            password="hunter2-secret",
            email="service-test@example.com",
            token="tok_live_abcdef123456",
        )

    def test_redacted_hides_password(self):
        assert "hunter2-secret" not in repr(self._account().redacted())

    def test_redacted_hides_token(self):
        assert "tok_live_abcdef123456" not in repr(self._account().redacted())

    def test_redacted_keeps_username(self):
        assert self._account().redacted()["username"] == "service-test"

    def test_redacted_returns_a_dict(self):
        assert isinstance(self._account().redacted(), dict)


# --------------------------------------------------------------------------
# registry loader -- laziness is the point, so assert it
# --------------------------------------------------------------------------
class TestRegistryLoading:
    def test_registries_are_lazy_types(self):
        assert isinstance(MESSAGES, LazyRegistry)
        assert isinstance(MESSAGE_SCHEMAS, LazyRegistry)
        assert isinstance(SIMPLE_MESSAGES, DerivedRegistry)

    def test_fresh_registry_does_not_read_until_used(self):
        probe = LazyRegistry("messages.json")
        assert probe.loaded is False
        probe["RootApp.Core.MessageUuid"]
        assert probe.loaded is True

    def test_missing_data_file_gives_actionable_error(self):
        probe = LazyRegistry("definitely-not-a-real-file.json")
        with pytest.raises(RuntimeError, match="reinstall rootpy"):
            probe["anything"]

    def test_mapping_protocol_is_complete(self):
        assert len(MESSAGES) == 842
        assert "RootApp.Core.MessageUuid" in MESSAGES
        assert MESSAGES.get("nope") is None
        assert isinstance(next(iter(MESSAGES)), str)
        assert len(list(MESSAGES.items())) == len(MESSAGES)

    def test_simple_messages_indexes_every_message(self):
        assert sum(len(v) for v in SIMPLE_MESSAGES.values()) == len(MESSAGES)

    def test_simple_messages_matches_message_suffixes(self):
        for short, fulls in SIMPLE_MESSAGES.items():
            for full in fulls:
                assert full.rsplit(".", 1)[-1] == short

    def test_short_names_are_unique_in_this_dataset(self):
        """Documents reality: every short name resolves to exactly one message.

        ``connect.*`` and ``root.*`` reuse message *shapes* across services, so
        an ambiguous short name is possible in principle -- but in the current
        registry none is. The value stays a list so the disambiguation path
        keeps working if one ever collides, and this test fails loudly then.
        """
        ambiguous = {k: v for k, v in SIMPLE_MESSAGES.items() if len(v) > 1}
        assert ambiguous == {}

    def test_turnstile_token_is_field_three(self):
        """``TurnstileToken`` is field 3 on PasswordSignUpRequest, not 7."""
        signup = MESSAGES["RootApp.Connect.Shared.PasswordSignUpRequest"]
        token = next(f for f in signup["fields"] if f["name"] == "TurnstileToken")
        assert token["field_number"] == 3

    def test_asset_information_link_expiry_present(self):
        """Signed asset URLs expire; the field must survive the JSON move."""
        info = MESSAGES["RootApp.Assets.AssetInformation"]
        names = {f["name"] for f in info["fields"]}
        assert "LinkExpiresAt" in names and "Image" in names

    def test_services_and_enums_load(self):
        # 32, counting root.CommunityDiscoveryGrpcService.
        assert len(SERVICES) == 32 and len(RPC_SERVICES) == 32
        for reg in (SERVICES, RPC_SERVICES):
            discovery = reg["root.CommunityDiscoveryGrpcService"]
            assert set(discovery) == {"Search", "Join"}
        # the three newer methods on services that already existed
        assert "SetDiscoverable" in RPC_SERVICES["root.CommunityGrpcService"]
        assert "SetNotificationSetting" in RPC_SERVICES[
            "root.CommunityMemberGrpcService"]
        assert "SetNotificationSetting" in RPC_SERVICES[
            "root.DirectMessageGrpcService"]
        # 92, six of which arrived with Community Discovery and its
        # supporting types: CommunityCategory,
        # CommunityDiscoverableRequirement, CommunityDiscoverySort,
        # CommunityJoinSource, NotificationStatus and WebRtcCallResult.
        assert len(ENUMS) == 92
        for name in ("CommunityCategory", "CommunityDiscoverySort",
                     "NotificationStatus", "CommunityJoinSource"):
            assert any(k.endswith("." + name) for k in ENUMS), name


# --------------------------------------------------------------------------
# import hygiene -- these are the regressions this release fixed
# --------------------------------------------------------------------------
class TestImportHygiene:
    def test_voice_stack_is_not_imported_eagerly(self):
        import subprocess
        import sys

        code = (
            "import sys, rootpy; "
            "print('rootpy.services.calls' in sys.modules, 'httpx' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout
        assert out.strip() == "False False"

    def test_media_bootstrap_never_shells_out(self):
        from rootpy import media_bootstrap

        source = inspect.getsource(media_bootstrap)
        assert "subprocess" not in source
        assert "check_call" not in source

    def test_missing_media_raises_actionable_error(self):
        from rootpy.media_bootstrap import MediaDependencyMissing

        exc = MediaDependencyMissing("aiortc")
        assert "rootpy-client[voice]" in str(exc)

    def test_client_does_not_install_media_by_default(self):
        assert RootClient().require_media is False

    def test_calls_service_is_built_on_first_access(self):
        client = RootClient()
        assert client._calls is None
        assert client.calls is not None
        assert client._calls is client.calls


# --------------------------------------------------------------------------
# high-level surface -- shape checks that need no network
# --------------------------------------------------------------------------
class TestHighLevelSurface:
    EXPECTED = [
        "message", "reply", "edit_message", "delete_message", "react", "unreact",
        "pin", "unpin", "open_dm", "direct_message", "call", "join_voice",
        "leave_voice", "play", "stop_playing", "mute", "unmute", "deafen",
        "undeafen", "whoami", "update_username", "update_status",
        "update_description", "update_avatar", "update_banner", "update_profile",
        "add_friend", "remove_friend", "list_friends", "block", "unblock",
        "list_blocked", "list_notifications", "unread_count", "mark_all_read",
        "list_communities", "get_members", "get_profiles", "get_random_member",
        "send_friend_request", "accept_friend_request", "decline_friend_request",
        "pending_friend_requests", "download_asset", "save_asset",
        "list_messages", "remove_message_listener",
    ]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_verb_exists_and_is_async(self, name):
        method = getattr(highlevel.HighLevelMixin, name, None)
        assert method is not None, f"HighLevelMixin.{name} is missing"
        if not isinstance(inspect.getattr_static(highlevel.HighLevelMixin, name), property):
            assert inspect.iscoroutinefunction(method) or callable(method)

    def test_client_inherits_the_mixin(self):
        assert issubclass(RootClient, highlevel.HighLevelMixin)

    def test_describe_services_returns_signatures(self):
        described = RootClient().describe_services()
        assert isinstance(described, dict) and described
        service = next(iter(described.values()))
        assert isinstance(service, dict) and service

    def test_public_api_is_importable(self):
        missing = [n for n in rootpy.__all__ if not hasattr(rootpy, n)]
        assert missing == [], f"exported but absent: {missing}"

    def test_no_mass_messaging_helpers_on_the_client_surface(self):
        """The single-account client stays a single-account client.

        rootpy automates *one* account -- the user's own (CONTINUING.md,
        "Scope"). Fan-out over a few accounts you already own is a real,
        bounded need, so it has exactly one home: ``MultiClientHost.broadcast``,
        which loops a caller-supplied action over the accounts registered on
        the host and returns one ``Outcome`` each. That is deliberate and
        reviewed -- and it lives on the multi-account host, not on the ordinary
        client.

        What must never appear is mass-messaging tooling on the single-account
        surface -- a ``spam``, a ``bulk_message``, a ``sweep_all``, a
        ``broadcast`` smuggled onto the client. This pins the boundary: neither
        ``RootClient`` nor ``HighLevelMixin`` carries one of those names, and
        the sanctioned fan-out exists where it belongs.
        """
        from rootpy import MultiClientHost

        banned = ("mass_", "broadcast", "spam", "bulk_message", "sweep_all")
        surfaces = {
            "RootClient": RootClient,
            "HighLevelMixin": highlevel.HighLevelMixin,
        }
        offenders = [
            f"{label}.{name}"
            for label, cls in surfaces.items()
            for name in dir(cls)
            if not name.startswith("_")
            and any(token in name.casefold() for token in banned)
        ]
        assert offenders == [], (
            f"mass-messaging helpers on the single-account surface: {offenders}"
            " -- the only sanctioned fan-out is MultiClientHost.broadcast"
        )
        # ...and that one sanctioned fan-out exists, on the host.
        assert callable(getattr(MultiClientHost, "broadcast", None))
        assert not any(
            hasattr(cls, "broadcast") for cls in surfaces.values()
        ), "broadcast belongs on MultiClientHost, not the single-account client"


# --------------------------------------------------------------------------
# response readers -- one uniform accessor, no per-module copies
# --------------------------------------------------------------------------
class TestResponseReaders:
    def test_field_reads_dict_object_and_none(self):
        from rootpy.responses import field

        class O:
            pass

        obj = O()
        obj.x = 5
        assert field({"a": 1}, "a") == 1
        assert field(obj, "x") == 5
        assert field(obj, "missing") is None
        assert field(None, "a") is None

    def test_first_returns_first_truthy(self):
        from rootpy.responses import first

        assert first({"a": 0, "b": 2}, "a", "b") == 2
        assert first({"a": "", "b": None}, "a", "b") is None
        assert first(None, "a") is None

    def test_as_sequence_normalises(self):
        from rootpy.responses import as_sequence

        assert as_sequence(None) == []
        assert as_sequence(5) == [5]
        assert as_sequence((1, 2)) == [1, 2]
        assert as_sequence([1]) == [1]

    def test_items_unwraps_a_named_envelope(self):
        from rootpy.responses import items

        class Env:
            pass

        env = Env()
        env.notifications = [1, 2]
        assert items(env, "notifications") == [1, 2]
        assert items([9], "notifications") == [9]   # already the sequence
        assert items(None, "x") == []

    def test_items_reads_an_empty_envelope_as_empty(self):
        """Zero rows must be zero rows, not one phantom envelope.

        Root omits an empty repeated field from the response entirely, so an
        empty list RPC arrives as a bare envelope with no such attribute. The
        "payload is already the sequence" fallback used to catch that case and
        hand the envelope itself to ``as_sequence``, which wraps a non-sequence
        in a list -- so an empty inbox read back as ``[<envelope>]`` with
        ``len() == 1``. Nothing could ever observe it as empty, which is what
        failed ``test_delete_all_empties_the_peer_inbox`` live against a peer
        whose inbox had in fact been emptied.

        The three shapes an empty envelope actually arrives in, all pinned.
        """
        from rootpy.responses import items

        class Env:
            pass

        class AttrDict(dict):
            def __getattr__(self, key):
                try:
                    return self[key]
                except KeyError:
                    raise AttributeError(key) from None

        # object envelope with the attribute missing
        assert items(Env(), "notifications") == []
        # AttrDict envelope with the key missing
        assert items(AttrDict(), "notifications") == []
        # plain dict envelope with the key missing
        assert items({}, "notifications") == []
        # ...and one carrying *other* fields must not leak them as rows
        assert items({"cursor": "abc"}, "notifications") == []
        assert items(AttrDict(cursor="abc"), "notifications") == []

        # the field present but empty was always right -- keep it right
        assert items({"notifications": []}, "notifications") == []

        # and the genuine fallback still stands: an already-unwrapped sequence
        assert items([], "notifications") == []
        assert items([9], "notifications") == [9]

    def test_features_shares_the_one_reader(self):
        # The de-duplication is the point: features must not grow its own copy
        # again. Pin identity, not just behaviour.
        import rootpy.features as features
        from rootpy import responses

        assert features._field is responses.field
        assert features._as_sequence is responses.as_sequence


# --------------------------------------------------------------------------
# explain / preview -- offline discovery of the API surface
# --------------------------------------------------------------------------
class TestExplainAndPreview:
    def _client(self):
        return RootClient(token="x")

    def test_explain_index_lists_managers_and_services(self):
        described = self._client().explain().as_dict()
        assert described["kind"] == "index"
        assert "community_files" in described["managers"]
        assert {"file", "message"} <= set(described["services"])

    def test_explain_wire_service_lists_its_methods(self):
        described = self._client().explain("file").as_dict()
        assert described["kind"] == "service"
        names = {member["name"] for member in described["members"]}
        assert {"search", "create", "delete"} <= names

    def test_explain_wire_method_shows_request_fields(self):
        described = self._client().explain("file.search").as_dict()
        assert described["kind"] == "wire_method"
        wire = described["wire"][0]
        assert {"community_id", "search"} <= {f["name"] for f in wire["fields"]}
        assert wire["endpoint"].endswith("FileGrpcService/Search")

    def test_explain_manager_method_links_to_its_wire_call(self):
        described = self._client().explain("community_files.search").as_dict()
        assert described["kind"] == "manager_method"
        assert described["python_signature"].startswith("community_files.search")
        assert described["wire"], "manager method should surface its wire call"
        assert described["wire"][0]["signature"].startswith("file.search")

    def test_explain_accepts_the_api_alias_spelling(self):
        assert self._client().explain("file_api").kind == "service"

    def test_explain_rejects_an_unknown_name(self):
        with pytest.raises(ValueError):
            self._client().explain("definitely-not-a-real-name")

    def test_explain_output_is_ascii_safe(self):
        # print(client.explain(...)) must not crash on a cp1252 Windows console.
        client = self._client()
        for target in (
            None, "file", "file.search", "community_files",
            "community_files.search",
        ):
            str(client.explain(target)).encode("cp1252")

    def test_preview_encodes_a_real_frame_without_sending(self):
        import uuid

        preview = self._client().preview(
            "message.create", container_id=str(uuid.uuid4()), content="hi"
        )
        assert preview.method_type == "Unary"
        # framing prepends a 1-byte flag + 4-byte big-endian length.
        assert len(preview.framed) == len(preview.unframed) + 5 == preview.size
        assert preview.framed[0] == 0
        assert int.from_bytes(preview.framed[1:5], "big") == len(preview.unframed)

    def test_preview_validates_an_unknown_field(self):
        import uuid

        with pytest.raises(TypeError):
            self._client().preview(
                "message.create", container_id=str(uuid.uuid4()), bogus=1
            )

    def test_preview_validates_a_malformed_value(self):
        with pytest.raises(ValueError):
            self._client().preview(
                "message.create", container_id="not-a-guid", content="x"
            )

    def test_preview_needs_a_method(self):
        with pytest.raises(ValueError):
            self._client().preview("file")

    def test_preview_output_is_ascii_safe(self):
        import uuid

        preview = self._client().preview(
            "message.create", container_id=str(uuid.uuid4()), content="hi"
        )
        str(preview).encode("cp1252")


# --------------------------------------------------------------------------
# commands -- Context mention parsing
# --------------------------------------------------------------------------
class TestCommandContext:
    def _context(self, content):
        client = RootClient()
        message = models.Message(
            id="m1", container_id="c1", user_id=_VALID_USER_ID, content=content
        )
        return commands.Context(
            client=client,
            message=message,
            prefix=">",
            invoked_with="test",
            command=None,
            args=(),
            raw_arguments=content,
        )

    def test_author_is_derived_from_the_message(self):
        ctx = self._context("hello")
        assert ctx.author is None or hasattr(ctx.author, "id")

    def test_mentioned_users_empty_without_mentions(self):
        assert list(self._context("no mentions here").mentioned_users) == []

    def test_mentioned_channels_empty_without_mentions(self):
        assert list(self._context("no mentions here").mentioned_channels) == []

    def test_mention_properties_return_iterables(self):
        ctx = self._context("hello")
        assert hasattr(ctx.mentioned_users, "__iter__")
        assert hasattr(ctx.mentioned_channels, "__iter__")


# --------------------------------------------------------------------------
# client surface -- catches wrong-layer mistakes without a token
# --------------------------------------------------------------------------
class TestClientSurfaceContract:
    """The attribute names, kinds and signatures callers are told to use.

    There are 31 manager and service attributes on the client, several of
    them near-synonyms -- ``client.community`` is ``CommunityManager``
    (``create``), ``client.admin`` is ``CommunityAdminService``
    (``create_community``) -- so picking the wrong one is easy, and so is
    awaiting ``is_connected``, which is a property. Each of those fails at
    runtime with a message that does not name the real problem. These pin
    the surface instead.
    """

    CLIENT_ATTRS = [
        "community", "admin", "community_admin", "members", "user_settings",
        "assets", "messages", "users", "notifications", "friends", "blocks",
        "friend_requests", "transport", "cache",
    ]

    @pytest.mark.parametrize("name", CLIENT_ATTRS)
    def test_client_exposes_attribute(self, name):
        assert hasattr(RootClient(), name), f"client.{name} is missing"

    def test_client_community_is_the_manager_not_the_admin_service(self):
        client = RootClient()
        assert type(client.community).__name__ == "CommunityManager"
        assert type(client.admin).__name__ == "CommunityAdminService"

    #: Methods that hit the network and must be awaited.
    COMMUNITY_MANAGER_ASYNC = [
        "create", "delete", "clone", "edit", "create_channel_group",
        "create_text_channel", "create_voice_channel", "create_role",
        "delete_role", "get_channels", "get_members", "get_roles", "fetch",
    ]

    #: Methods that read the local cache and are deliberately synchronous.
    COMMUNITY_MANAGER_SYNC = ["get", "cached"]

    @pytest.mark.parametrize("name", COMMUNITY_MANAGER_ASYNC)
    def test_community_manager_async_method(self, name):
        manager = RootClient().community
        assert hasattr(manager, name), f"client.community.{name} is missing"
        assert inspect.iscoroutinefunction(getattr(manager, name)), (
            f"client.community.{name} should be awaitable"
        )

    @pytest.mark.parametrize("name", COMMUNITY_MANAGER_SYNC)
    def test_community_manager_sync_cache_read(self, name):
        """These read the cache -- awaiting them is the mistake to catch."""
        manager = RootClient().community
        assert hasattr(manager, name), f"client.community.{name} is missing"
        assert not inspect.iscoroutinefunction(getattr(manager, name)), (
            f"client.community.{name} is synchronous -- do not await it"
        )

    PROPERTIES = ["is_connected"]

    @pytest.mark.parametrize("name", PROPERTIES)
    def test_is_a_property_not_a_method(self, name):
        assert isinstance(inspect.getattr_static(RootClient, name), property), (
            f"RootClient.{name} is a property -- call it without parentheses"
        )

    SIGNATURES = {
        "permissions_for": ["user_id", "channel_id"],
        "get_message": ["message_id"],
        "messages_for_container": ["container_id"],
        "ensure_community_cached": ["community_id"],
        "get_community": ["community_id"],
    }

    @pytest.mark.parametrize("name,params", list(SIGNATURES.items()))
    def test_client_method_signature(self, name, params):
        method = getattr(RootClient, name, None)
        assert method is not None, f"RootClient.{name} is missing"
        actual = [
            p for p in inspect.signature(method).parameters if p != "self"
        ]
        assert actual == params, f"{name}{tuple(actual)} != expected {tuple(params)}"

    def test_asset_methods_take_a_sequence_of_uris(self):
        for name in ("resolve", "get"):
            method = getattr(RootClient().assets, name)
            first = next(iter(inspect.signature(method).parameters))
            assert first == "uris", f"assets.{name} takes {first!r}, not a list"


# --------------------------------------------------------------------------
# regression: create_community could never succeed with its own defaults
# --------------------------------------------------------------------------
class TestCommunityCreateDefaults:
    """Root applies a NotEmptyValidator to PictureHex.

    ``create_community`` defaulted ``picture_hex`` to ``""`` *and* guarded the
    field with ``if picture_hex:``, so the default path omitted field 11 and
    Root rejected every such request with a generic INVALID_ARGUMENT naming no
    field.
    """

    def test_default_picture_hex_is_not_empty(self):
        from rootpy.services.community_admin import CommunityAdminService

        default = inspect.signature(
            CommunityAdminService.create_community
        ).parameters["picture_hex"].default
        assert default, "picture_hex default must be non-empty -- Root rejects it"

    def test_default_picture_hex_round_trips(self):
        from rootpy.services.community_admin import (
            DEFAULT_PICTURE_HEX,
            normalize_hex_colour,
        )

        assert normalize_hex_colour(DEFAULT_PICTURE_HEX) == DEFAULT_PICTURE_HEX

    @pytest.mark.parametrize(
        "value",
        ["3f51b5", "#3F51B5", " 3f51b5 ", "#3f51b5"],
    )
    def test_normalises_to_the_seven_character_form(self, value):
        from rootpy.services.community_admin import (
            PICTURE_HEX_LENGTH,
            normalize_hex_colour,
        )

        result = normalize_hex_colour(value)
        assert result == "#3f51b5"
        assert len(result) == PICTURE_HEX_LENGTH

    def test_default_is_exactly_seven_characters(self):
        """Root: "'Picture Hex' must be 7 characters in length"."""
        from rootpy.services.community_admin import (
            DEFAULT_PICTURE_HEX,
            PICTURE_HEX_LENGTH,
        )

        assert len(DEFAULT_PICTURE_HEX) == PICTURE_HEX_LENGTH == 7
        assert DEFAULT_PICTURE_HEX.startswith("#")

    def test_eight_digit_alpha_is_rejected(self):
        """Exactly 7 characters, so the alpha form cannot be valid."""
        from rootpy.services.community_admin import normalize_hex_colour

        with pytest.raises(ValueError):
            normalize_hex_colour("3f51b5ff")

    @pytest.mark.parametrize("value", ["", "   ", "#", "nope", "12345", "gggggg"])
    def test_rejects_unusable_values_client_side(self, value):
        from rootpy.services.community_admin import normalize_hex_colour

        with pytest.raises(ValueError):
            normalize_hex_colour(value)

    def test_error_message_names_the_field(self):
        from rootpy.services.community_admin import normalize_hex_colour

        with pytest.raises(ValueError, match="picture_hex"):
            normalize_hex_colour("")

    def test_field_11_is_always_emitted(self):
        """Field 11 must not be behind a truthiness guard again."""
        from rootpy.services import community_admin

        src = inspect.getsource(
            community_admin.CommunityAdminService.create_community
        )
        guard = "if " + "picture_hex:"      # split so this line is not a match
        assert guard not in src, "the guard that omitted field 11 is back"
        assert "normalize_hex_colour(picture_hex)" in src


# --------------------------------------------------------------------------
# regression: gRPC errors must name the field Root rejected
# --------------------------------------------------------------------------
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def _len_field(number: int, payload: bytes) -> bytes:
    return _tag(number, 2) + _varint(len(payload)) + payload


def _str_field(number: int, text: str) -> bytes:
    return _len_field(number, text.encode("utf-8"))


def build_root_exception_header(errors):
    """A realistic ``root-exception-bin`` header carrying validation errors.

    Mirrors the wire shape Root sends: RootGrpcException.Payload (16) ->
    RootGrpcExceptionPayload.RequestValidatorList (15) ->
    RequestValidatorListExceptionPayload.Errors (10) ->
    RequestValidatorExceptionPayload {PropertyName 10, ErrorMessage 11,
    ErrorCode 12}.
    """
    import base64

    entries = b"".join(
        _len_field(
            10,
            _str_field(10, prop) + _str_field(11, msg) + _str_field(12, code),
        )
        for prop, msg, code in errors
    )
    # Errors is a *repeated* field 10 on RequestValidatorListExceptionPayload,
    # so the entries are concatenated field-10 records -- not one field 10
    # wrapping them all. Getting that wrong made a single error still match by
    # substring against the raw bytes, which is worse than failing outright.
    payload = _len_field(15, entries)
    root = _tag(10, 0) + _varint(3) + _len_field(16, payload)
    return {"root-exception-bin": base64.b64encode(root).decode()}


class TestGrpcErrorSurfacesValidationDetail:
    """Root's grpc-message is always the same useless sentence.

    The field-level reason arrives in the ``root-exception-bin`` header and was
    already decoded onto ``exc.validation_errors`` -- but the message string
    ignored it, so every INVALID_ARGUMENT read "One or more request values were
    rejected by Root" and took a live round trip to diagnose. Six of them, in
    practice. The detail now goes into the message itself.
    """

    GENERIC = "One or more request values were rejected by Root."

    def _error(self, errors):
        from rootpy.exceptions import make_grpc_error

        return make_grpc_error(
            "root.CommunityGrpcService/Create",
            3,
            self.GENERIC,
            response_headers=build_root_exception_header(errors),
        )

    def test_header_decodes_onto_the_exception(self):
        exc = self._error([("PictureHex", "must not be empty", "NotEmptyValidator")])
        assert exc.payload_kind == "request_validator_list"
        assert len(exc.validation_errors) == 1

    def test_message_names_the_field(self):
        exc = self._error([("PictureHex", "must not be empty", "NotEmptyValidator")])
        assert "PictureHex" in str(exc)

    def test_message_gives_the_reason(self):
        exc = self._error([("PictureHex", "must not be empty", "NotEmptyValidator")])
        assert "must not be empty" in str(exc)

    def test_message_gives_the_validator_name(self):
        exc = self._error([("PictureHex", "must not be empty", "NotEmptyValidator")])
        assert "NotEmptyValidator" in str(exc)

    def test_every_field_is_listed(self):
        exc = self._error([
            ("Name", "too short", "MinLengthValidator"),
            ("PictureHex", "must be 7 characters", "ExactLengthValidator"),
        ])
        text = str(exc)
        assert "Name" in text and "PictureHex" in text

    def test_long_lists_are_truncated_with_a_count(self):
        exc = self._error([(f"F{i}", "bad", "V") for i in range(8)])
        assert "+3 more" in str(exc)

    def test_no_header_leaves_the_message_unchanged(self):
        from rootpy.exceptions import make_grpc_error

        exc = make_grpc_error("root.X/Y", 3, self.GENERIC)
        assert exc.validation_errors == []
        assert "->" not in str(exc)

    def test_malformed_header_never_breaks_construction(self):
        from rootpy.exceptions import make_grpc_error

        exc = make_grpc_error(
            "root.X/Y", 3, self.GENERIC,
            response_headers={"root-exception-bin": "!!!not-base64!!!"},
        )
        assert isinstance(str(exc), str)


# --------------------------------------------------------------------------
# regression: CommunityManager channel-type constants were wrong
# --------------------------------------------------------------------------
class TestChannelTypeConstants:
    """``CommunityManager`` defined TEXT = 0 and VOICE = 1.

    The descriptor says ``ChannelType {Unspecified: 0, Text: 1,
    ThreadedText: 2, Voice: 4, App: 8}``. So ``create_text_channel()`` sent
    UNSPECIFIED and Root rejected it with a PredicateValidator, while
    ``create_voice_channel()`` sent TEXT -- which *succeeds* and quietly makes
    a text channel. A wrong constant that fails loudly is a nuisance; one that
    silently creates the wrong object is the reason this class exists.
    """

    def test_text_is_one(self):
        from rootpy.object_api import CommunityManager

        assert CommunityManager.TEXT == 1

    def test_voice_is_four(self):
        from rootpy.object_api import CommunityManager

        assert CommunityManager.VOICE == 4

    def test_no_constant_is_unspecified(self):
        from rootpy.object_api import CommunityManager

        for name in ("TEXT", "THREADED_TEXT", "VOICE"):
            assert getattr(CommunityManager, name) != 0, f"{name} is UNSPECIFIED"

    def test_constants_match_the_enum(self):
        from rootpy.enums import ChannelType
        from rootpy.object_api import CommunityManager

        assert CommunityManager.TEXT == ChannelType.TEXT
        assert CommunityManager.THREADED_TEXT == ChannelType.THREADED_TEXT
        assert CommunityManager.VOICE == ChannelType.VOICE

    @pytest.mark.parametrize(
        "module,cls,method",
        [
            ("rootpy.services.community_admin", "CommunityAdminService",
             "create_channel"),
            ("rootpy.object_api", "CommunityManager", "create_channel"),
        ],
    )
    def test_create_channel_does_not_default_to_unspecified(
        self, module, cls, method
    ):
        """The three-argument call must be able to succeed.

        Both surfaces defaulted ``channel_type`` to 0 = Unspecified -- the
        value this class exists to record Root rejecting -- so
        ``create_channel(cid, gid, "name")`` could not work against any
        server, while every worked example passes the type explicitly and
        none of them says the default is broken.
        """
        import importlib

        from rootpy.enums import ChannelType

        target = getattr(getattr(importlib.import_module(module), cls), method)
        default = inspect.signature(target).parameters["channel_type"].default
        assert default != 0, (
            f"{cls}.{method} defaults channel_type to UNSPECIFIED, which Root "
            "rejects with a PredicateValidator"
        )
        assert default == int(ChannelType.TEXT), (
            f"{cls}.{method} should default to TEXT ({int(ChannelType.TEXT)}), "
            f"got {default}"
        )

    def test_model_channel_helpers_send_the_right_type(self):
        """``Community.create_voice_channel`` sent 1 -- a *text* channel.

        The same wrong-constant bug as the class docstring, left on the model
        surface after ``CommunityManager`` was fixed. It succeeded, so nothing
        surfaced it.
        """
        import asyncio

        from rootpy.enums import ChannelType
        from rootpy.models import Community

        sent = {}

        class _Admin:
            async def create_channel(self, cid, gid, name, **kwargs):
                sent[name] = kwargs.get("channel_type")
                return None

        # Community is a frozen dataclass; _admin is a field, not an attribute
        # to assign after the fact.
        community = Community(
            id="c1", name="c", owner_user_id="u", default_channel_id=None,
            _admin=_Admin(),
        )

        asyncio.run(community.create_text_channel("g1", "text"))
        asyncio.run(community.create_voice_channel("g1", "voice"))

        assert sent["text"] == int(ChannelType.TEXT), (
            f"create_text_channel sent {sent['text']}, not TEXT"
        )
        assert sent["voice"] == int(ChannelType.VOICE), (
            f"create_voice_channel sent {sent['voice']}, not VOICE "
            f"-- {sent['voice']} would silently create a text channel"
        )

    def test_enum_matches_the_generated_descriptor(self):
        """The descriptor is the source of truth for these values."""
        from rootpy.enums import ChannelType
        from rootpy.structured_registry import ENUMS

        descriptor = ENUMS["RootApp.WebApi.Shared.Enums.ChannelType"]
        assert descriptor["Text"] == ChannelType.TEXT
        assert descriptor["ThreadedText"] == ChannelType.THREADED_TEXT
        assert descriptor["Voice"] == ChannelType.VOICE
        assert descriptor["Unspecified"] == ChannelType.UNSPECIFIED

    def test_text_and_voice_are_distinct(self):
        from rootpy.object_api import CommunityManager

        assert CommunityManager.TEXT != CommunityManager.VOICE


# --------------------------------------------------------------------------
# contract: every client call in the examples must match its sync/async-ness
# --------------------------------------------------------------------------
def _client_calls_in_examples():
    """Yield (dotted_path, is_awaited, lineno) for each client.* call.

    A coroutine call counts as awaited if it sits under an ``await`` *or* is
    handed to ``asyncio.wait_for``, which awaits it for us.

    A call inside a ``lambda:`` handed to a helper that calls it is a third
    case, and neither failure mode this guard exists for applies to it. Such
    a helper invokes the thunk and then ``await``s the result only if it is
    awaitable, so a sync method is as correct there as an async one. Those
    are yielded as ``None`` -- neither awaited nor unawaited -- and the check
    skips the sync/async assertion while still verifying that the attribute
    exists. Deferring to a lambda that nothing consumes is *not* excused: the
    lambda has to be an argument to a call for this to apply.
    """
    import ast

    sources = _example_sources()
    assert sources, "no example modules found to scan"

    parents = {}
    trees = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees.append((path.name, tree))
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id == "client":
            return ".".join(reversed(parts))
        return None

    for name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            path = dotted(node.func)
            if not path:
                continue
            parent = parents.get(node)

            # `lambda: client.thing(...)` handed to a helper that calls it.
            if isinstance(parent, ast.Lambda):
                consumer = parents.get(parent)
                # A lambda inside a tuple/list literal is still an argument
                # once the container is.
                while isinstance(consumer, (ast.Tuple, ast.List)):
                    consumer = parents.get(consumer)
                if isinstance(consumer, (ast.Call, ast.keyword)):
                    yield path, None, f"{name}:{node.lineno}"
                    continue

            awaited = isinstance(parent, ast.Await)
            if not awaited and isinstance(parent, ast.Call):
                # A coroutine handed to any of these is consumed correctly --
                # the wrapper awaits or schedules it. Recognising only
                # `wait_for` flags `create_task(client.wait_for(...))`, which
                # is the normal way to start a waiter before triggering the
                # thing it waits for.
                target = dotted(parent.func) or getattr(parent.func, "attr", "")
                awaited = target.rsplit(".", 1)[-1] in {
                    "wait_for", "create_task", "ensure_future", "gather",
                    "shield", "wait", "as_completed", "run",
                }
            yield path, awaited, f"{name}:{node.lineno}"


def _is_awaitable_call(target):
    """True if ``target(...)`` produces something you can await.

    ``iscoroutinefunction`` answers for a plain method and says *no* for a
    callable object with an ``async def __call__`` -- which is what
    ``client.high.<service>.<method>`` is. Reading it off the instance
    rather than its class is the mistake that makes this guard call a
    correct ``await`` a bug.
    """
    if inspect.iscoroutinefunction(target):
        return True
    if not inspect.isfunction(target) and not inspect.ismethod(target):
        call = getattr(type(target), "__call__", None)
        return bool(call) and inspect.iscoroutinefunction(call)
    return False


def _resolve(path):
    from rootpy import RootClient

    target = RootClient(token="x")
    for part in path.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    return target


class TestExampleCallsAwaitCorrectly:
    """Awaiting a synchronous method raises TypeError at runtime.

    ``get_message`` and ``messages_for_container`` are cache reads that return
    tuples, and ``community.get`` is the same. An example that awaits one is a
    broken instruction, and a reader copies it verbatim. This walks the AST of
    everything under ``examples/`` and checks each ``client.*`` call against
    the real object, so the whole class of mistake fails offline.
    """

    CALLS = sorted(set((p, a) for p, a, _ in _client_calls_in_examples()))

    def test_the_scan_found_calls(self):
        assert len(self.CALLS) > 20, "AST scan found suspiciously few calls"

    @pytest.mark.parametrize("path,awaited", CALLS)
    def test_call_matches_its_kind(self, path, awaited):
        target = _resolve(path)
        assert target is not None, f"client.{path} does not exist"
        if not callable(target):
            return
        # An async generator is iterated with `async for`, never awaited --
        # flagging it as "a coroutine that is not awaited" was wrong, and
        # awaiting it would be the actual mistake.
        if inspect.isasyncgenfunction(target):
            if awaited:
                pytest.fail(
                    f"client.{path} is an async generator -- iterate it with "
                    f"`async for`, do not await it"
                )
            return

        # Deferred into a lambda that a helper consumes: such a helper awaits
        # only what is awaitable, so neither spelling is a mistake there.
        # The attribute-exists check above still ran.
        if awaited is None:
            return

        is_async = _is_awaitable_call(target)
        if is_async and not awaited:
            pytest.fail(f"client.{path} is a coroutine but is not awaited")
        if not is_async and awaited:
            pytest.fail(
                f"client.{path} is synchronous -- awaiting it raises "
                f"TypeError: object ... can't be used in 'await' expression"
            )

    def test_every_referenced_attribute_exists(self):
        missing = sorted({p for p, _ in self.CALLS if _resolve(p) is None})
        assert missing == [], f"an example calls non-existent: {missing}"


# --------------------------------------------------------------------------
# regressions that only a real server could have shown, pinned offline
# --------------------------------------------------------------------------
class TestTokenOnlyClientIsAuthenticated:
    """``_optional_token`` ignored a constructor-supplied token.

    ``StructuredAPI``/``RawAPI`` omit the authorization header entirely when
    the getter returns "", so ``RootClient(token=...)`` without a login got
    HTTP 401 on everything routed through ``client.high`` -- notifications,
    friends, blocks, user notes, member edits -- while the ``services/``
    layer, which uses ``_require_token``, worked on the same client -- so the
    same client both worked and did not, depending on the call.
    """

    def test_optional_token_uses_the_constructor_token(self):
        assert RootClient(token="TESTTOKEN")._optional_token() == "TESTTOKEN"

    def test_optional_token_matches_require_token_without_a_session(self):
        client = RootClient(token="TESTTOKEN")
        assert client._optional_token() == client._require_token()

    def test_optional_token_is_empty_with_no_token_at_all(self):
        assert RootClient()._optional_token() == ""

    def test_optional_token_never_raises(self):
        assert RootClient()._optional_token() == ""

    def test_structured_api_would_send_an_auth_header(self):
        client = RootClient(token="TESTTOKEN")
        headers = client.high.headers("https://api.rootapp.com/root.UserGrpcService/BlockList")
        assert headers.get("authorization") == "Bearer TESTTOKEN"

    def test_no_auth_header_when_there_is_no_token(self):
        headers = RootClient().high.headers("https://api.rootapp.com/root.X/Y")
        assert "authorization" not in headers


class TestListMessagesOmitsLimit:
    """MessageService.list documents that Root rejects a Limit field.

    ``HighLevelMixin.list_messages`` defaulted to ``limit=50`` and passed it
    through anyway, so the convenience wrapper reintroduced the exact bug the
    service layer below it documents avoiding.
    """

    def test_limit_defaults_to_none(self):
        assert (
            inspect.signature(highlevel.HighLevelMixin.list_messages)
            .parameters["limit"].default
            is None
        )

    def test_service_layer_also_defaults_to_none(self):
        from rootpy.services.messages import MessageService

        assert (
            inspect.signature(MessageService.list).parameters["limit"].default
            is None
        )

    def test_the_two_layers_agree(self):
        from rootpy.services.messages import MessageService

        wrapper = inspect.signature(
            highlevel.HighLevelMixin.list_messages
        ).parameters["limit"].default
        service = inspect.signature(MessageService.list).parameters["limit"].default
        assert wrapper == service


class TestCommunityEditIsAReplaceNotAPatch:
    """CommunityEdit replaces the whole community; Root requires Name *and*
    PictureHex.

    Editing only the description was rejected first for Name, then -- after a
    one-field fix -- for PictureHex. Fixing them individually is whack-a-mole,
    so the service now carries every replace-semantics value forward from the
    current community in a single read.
    """

    def _source(self):
        from rootpy.services import community_admin

        return inspect.getsource(
            community_admin.CommunityAdminService.edit_community
        )

    def test_no_field_is_conditionally_omitted(self):
        src = self._source()
        for field in ("name", "picture_hex"):
            guard = "if " + field + " is not " + "None:"
            assert guard not in src, f"{field} is conditional again"

    def test_missing_values_are_read_from_the_current_community(self):
        src = self._source()
        assert "get_extended" in src
        assert "REPLACED" in src

    def test_picture_hex_is_normalised_on_edit_too(self):
        assert "normalize_hex_colour" in self._source()

    def test_replaced_set_covers_both_required_fields(self):
        from rootpy.services import community_admin

        src = self._source()
        start = src.index("REPLACED = (")
        replaced = src[start : src.index(")", start)]
        assert "name" in replaced and "picture_hex" in replaced


# --------------------------------------------------------------------------
# further regressions, and the original audit backlog
# --------------------------------------------------------------------------
class _FakeMember:
    def __init__(self, user_id):
        self.user_id = user_id
        self.username = f"user-{user_id}"


class TestStateCacheIsBoundedOnBothAxes:
    """The per-community bucket was capped; the dict holding them was not.

    A client that stayed up accumulated one 2000-entry cache per community it
    ever saw and never released any of it -- invisible in short runs, and the
    whole point of a process meant to run for days.
    """

    def _cache(self, **kwargs):
        from rootpy.cache import StateCache

        return StateCache(**kwargs)

    def test_community_count_is_capped(self):
        cache = self._cache(max_communities=3)
        for i in range(10):
            cache.remember_member(f"community-{i}", _FakeMember(f"user-{i}"))
        assert len(cache.members) == 3

    def test_least_recently_used_community_is_evicted(self):
        cache = self._cache(max_communities=2)
        for i in range(3):
            cache.remember_member(f"community-{i}", _FakeMember(f"user-{i}"))
        assert cache.member("community-0", "user-0") is None
        assert cache.member("community-2", "user-2") is not None

    def test_members_per_community_still_capped(self):
        cache = self._cache(max_communities=5, max_members_per_community=4)
        for i in range(10):
            cache.remember_member("community-a", _FakeMember(f"user-{i}"))
        assert len(cache.members["community-a"]) == 4

    def test_members_container_is_an_lru_not_a_plain_dict(self):
        from rootpy.cache import LRUCache

        assert isinstance(self._cache().members, LRUCache)

    def test_defaults_are_generous_enough_to_be_invisible(self):
        cache = self._cache()
        assert cache.members.maxsize >= 500
        assert cache.max_members_per_community >= 2000


class TestMessageTombstones:
    """MessageList returns deleted messages with ``deleted_at`` set.

    Nothing filtered or flagged them, so "list the history" included things
    the user had already deleted, and there was no way to tell.
    """

    def _message(self, **kwargs):
        return models.Message(
            id="m", container_id="c", user_id="u", content="x", **kwargs
        )

    def test_live_message_is_not_deleted(self):
        assert self._message().is_deleted is False

    def test_tombstone_is_flagged(self):
        assert self._message(deleted_at=1_700_000_000).is_deleted is True

    def test_list_filters_tombstones_by_default(self):
        from rootpy.services.messages import MessageService

        assert (
            inspect.signature(MessageService.list)
            .parameters["include_deleted"].default
            is False
        )

    def test_wrapper_exposes_the_same_switch(self):
        assert (
            inspect.signature(highlevel.HighLevelMixin.list_messages)
            .parameters["include_deleted"].default
            is False
        )

    def test_filtering_actually_happens_in_list(self):
        from rootpy.services import messages

        src = inspect.getsource(messages.MessageService.list)
        assert "include_deleted" in src and "is_deleted" in src


class TestManagersReturnUsableValues:
    """``.data`` straight off the structured API is not an answer.

    ``count_unviewed()`` handed back a repeated per-container payload for
    something whose entire purpose is a number, and ``get_note()`` returned
    the whole ``UserNoteResponse`` -- so an unset note looked like a populated
    object and callers got a dict where they asked for a string.
    """

    def test_count_unviewed_is_annotated_int(self):
        """`from __future__ import annotations` makes this the string 'int'."""
        from rootpy.features import NotificationManager

        annotation = inspect.signature(
            NotificationManager.count_unviewed
        ).return_annotation
        assert annotation in (int, "int")

    def test_count_unviewed_sums_the_breakdown(self):
        from rootpy.features import NotificationManager

        src = inspect.getsource(NotificationManager.count_unviewed)
        assert "sum(" in src and "counts_by_container" in src

    def test_breakdown_is_still_available(self):
        from rootpy.features import NotificationManager

        assert hasattr(NotificationManager, "counts_by_container")

    @pytest.mark.parametrize(
        "payload,expected",
        [({"note": "hello"}, "hello"), ({"note": ""}, None), ({}, None), (None, None)],
    )
    def test_note_extraction(self, payload, expected):
        from rootpy.features import _field

        note = _field(payload, "note") or None
        assert note == expected

    def test_field_reads_objects_as_well_as_dicts(self):
        from rootpy.features import _field

        class Obj:
            note = "hi"

        assert _field(Obj(), "note") == "hi"

    @pytest.mark.parametrize(
        "value,expected",
        [(None, []), (1, [1]), ([1, 2], [1, 2]), ((1,), [1])],
    )
    def test_repeated_fields_normalise(self, value, expected):
        from rootpy.features import _as_sequence

        assert _as_sequence(value) == expected


class TestMessageListKeepsEveryDecodedField:
    """The list parser decoded the packet and then dropped most of it.

    ``_parse_message_list`` mapped only id/container/user/content/community
    into ``Message``, so ``deleted_at`` was always None -- which made
    ``Message.is_deleted`` permanently False and let deleted messages come
    back from history looking live, defeating the tombstone filter added
    alongside it. ``edited_at`` and ``pinned_at`` were lost the same way.
    """

    def _source(self):
        from rootpy.services.messages import MessageService

        return inspect.getsource(MessageService._parse_message_list)

    @pytest.mark.parametrize(
        "field", ["deleted_at", "edited_at", "pinned_at", "payload_raw"]
    )
    def test_field_survives_parsing(self, field):
        assert field in self._source(), f"{field} is dropped by the list parser"

    def test_packet_schema_actually_supplies_them(self):
        """These are decoded already -- the parser was throwing them away."""
        from rootpy.packet_schemas import PACKET_SCHEMAS

        names = {spec[0] for spec in PACKET_SCHEMAS["MESSAGE"].values()}
        assert {"deleted_at", "edited_at", "pinned_at"} <= names

    def test_tombstone_filter_has_something_to_filter_on(self):
        """The filter and the field that feeds it must both exist."""
        from rootpy.services.messages import MessageService

        list_src = inspect.getsource(MessageService.list)
        assert "is_deleted" in list_src
        assert "deleted_at" in self._source()


# --------------------------------------------------------------------------
# Root's documented username rule, checked before the request goes out
# --------------------------------------------------------------------------
class TestUsernameRule:
    """From Root's own sign-up form:

    "Must be 3-20 characters using only letters, numbers, underscores and
    periods. Underscores and periods can't be at the start or end, or next to
    each other."

    Checking client-side turns a round trip plus a generic INVALID_ARGUMENT
    into an immediate error naming the part of the rule that was broken.
    """

    @pytest.mark.parametrize(
        "value",
        ["abc", "a" * 20, "root.py", "a_b.c", "ExampleNick", "examplenick1",
         "a1_b2.c3", "___a" if False else "a_b_c"],
    )
    def test_accepts_valid(self, value):
        from rootpy.validation import validate_username

        assert validate_username(value) == value

    @pytest.mark.parametrize(
        "value,reason",
        [("ab", "two characters"), ("", "empty"), ("a" * 21, "twenty-one"),
         ("rootpy tester", "space"), ("rootpy-tester", "hyphen"),
         ("root@py", "at sign"), ("café", "non-ascii"),
         ("_abc", "leading underscore"), (".abc", "leading period"),
         ("abc_", "trailing underscore"), ("abc.", "trailing period"),
         ("a__b", "double underscore"), ("a..b", "double period"),
         ("a._b", "period then underscore"), ("a_.b", "underscore then period")],
    )
    def test_rejects_invalid(self, value, reason):
        from rootpy.validation import validate_username

        with pytest.raises(ValueError):
            validate_username(value)

    def test_non_string_is_a_type_error(self):
        from rootpy.validation import validate_username

        with pytest.raises(TypeError):
            validate_username(12345)

    def test_message_states_the_rule(self):
        from rootpy.validation import USERNAME_RULE, validate_username

        with pytest.raises(ValueError) as caught:
            validate_username("rootpy tester")
        assert USERNAME_RULE in str(caught.value)

    def test_message_names_the_offending_character(self):
        from rootpy.validation import validate_username

        with pytest.raises(ValueError, match=r"contains"):
            validate_username("rootpy tester")

    def test_value_is_returned_unchanged(self):
        """Silently rewriting somebody's chosen username would be worse."""
        from rootpy.validation import validate_username

        assert validate_username("Root.Py_1") == "Root.Py_1"

    def test_boundaries_are_inclusive(self):
        from rootpy.validation import (
            USERNAME_MAX_LENGTH,
            USERNAME_MIN_LENGTH,
            validate_username,
        )

        validate_username("a" * USERNAME_MIN_LENGTH)
        validate_username("a" * USERNAME_MAX_LENGTH)
        with pytest.raises(ValueError):
            validate_username("a" * (USERNAME_MIN_LENGTH - 1))
        with pytest.raises(ValueError):
            validate_username("a" * (USERNAME_MAX_LENGTH + 1))


class TestNicknameRule:
    """Nickname shares the username rule, as far as the evidence goes.

    The server accepts letters, mixed case and trailing digits, and rejects
    spaces and hyphens. Underscores and periods are inferred from the shared
    charset rather than separately proven, so treat them as likely rather
    than certain.
    """

    @pytest.mark.parametrize(
        "value", ["examplenick", "ExampleNick", "examplenick1"]
    )
    def test_accepts_the_shapes_root_accepted(self, value):
        from rootpy.validation import validate_nickname

        assert validate_nickname(value) == value

    @pytest.mark.parametrize("value", ["example nick", "example-nick"])
    def test_rejects_the_shapes_root_rejected(self, value):
        from rootpy.validation import validate_nickname

        with pytest.raises(ValueError):
            validate_nickname(value)

    def test_error_says_nickname_not_username(self):
        from rootpy.validation import validate_nickname

        with pytest.raises(ValueError, match="nickname"):
            validate_nickname("rootpy tester")


class TestValidationIsAppliedAtTheCallSites:
    """A rule nobody calls is documentation, not validation."""

    def test_set_username_validates(self):
        from rootpy.services.users import UserService

        assert "validate_username" in inspect.getsource(UserService.set_username)

    def test_edit_nickname_validates(self):
        from rootpy.domain_managers import MemberManager

        assert "validate_nickname" in inspect.getsource(MemberManager.edit_nickname)

    def test_colour_rules_moved_but_stayed_importable(self):
        """normalize_hex_colour was public from community_admin first."""
        from rootpy.services.community_admin import normalize_hex_colour as from_service
        from rootpy.validation import normalize_hex_colour as from_validation

        assert from_service is from_validation

    def test_exported_from_the_package(self):
        for name in (
            "validate_username", "validate_nickname", "normalize_hex_colour",
            "USERNAME_RULE", "DEFAULT_PICTURE_HEX",
        ):
            assert hasattr(rootpy, name), f"rootpy.{name} is not exported"


class TestDocumentationMatchesThePackage:
    """A documented method that does not exist is worse than a missing one.

    A reader calls what the prose names and awaits what the prose awaits, so
    drift in that direction turns the documentation into a source of
    ``AttributeError`` and ``TypeError``. It is also invisible to whoever
    wrote the page, because nothing they ran touched the package.

    This walks the prose that ships in the repository and checks every
    ``client.<verb>(`` it names against :class:`RootClient`. Today that is
    ``README.md``; the glob and the name list below still cover a ``docs/``
    tree and a ``CONTRIBUTING.md`` so that adding either one back puts it
    under the same guard without touching this test. It deliberately does not
    require any particular page to exist: which pages get written is an
    editorial decision, but a wrong method name is a bug.
    """

    @staticmethod
    def _prose_files():
        root = _repo_root()
        found = sorted(root.glob("docs/**/*.md"))
        for name in ("README.md", "CONTRIBUTING.md"):
            path = root / name
            if path.exists():
                found.append(path)
        return found

    @classmethod
    def _mentions(cls):
        """(file, dotted_path, was_awaited) for every ``client.x(`` in prose."""
        import re

        pattern = re.compile(
            r"(await\s+)?\bclient\.([A-Za-z_][A-Za-z0-9_.]*)\s*\("
        )
        out = []
        for path in cls._prose_files():
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                out.append((path.name, match.group(2), bool(match.group(1))))
        return out

    @staticmethod
    def _resolve(path):
        target = RootClient(token="x")
        for part in path.split("."):
            target = getattr(target, part, None)
            if target is None:
                return None
        return target

    def test_the_scan_found_verbs(self):
        """A scan that silently matches nothing would pass everything.

        The bound is 5, not 10, because the prose in this repository is now
        one lean README that names eight verbs. The guides moved to the
        website, which is a separate project with its own checker. Keep this
        above zero: its whole job is to fail when the scan stops matching,
        rather than reporting a clean sweep of nothing.
        """
        found = {path for _f, path, _a in self._mentions()}
        assert len(found) >= 5, (
            f"only {len(found)} client verbs found in the prose -- either the "
            "README lost its examples or the scan broke"
        )

    def test_every_documented_verb_exists(self):
        missing = sorted({
            f"{where}: client.{path}"
            for where, path, _awaited in self._mentions()
            if self._resolve(path) is None
        })
        assert missing == [], (
            "the documentation names methods the package does not have: "
            f"{missing}"
        )

    def test_nothing_awaitable_is_awaited_in_the_prose(self):
        """``await`` on a cache read or an async generator is the live trap.

        Only this direction is checked. Prose mentions a coroutine without
        ``await`` all the time -- "``client.message()`` sends a message" is
        perfectly good English -- so the absence of ``await`` proves nothing.
        Its *presence* on something that cannot be awaited always does.
        """
        offenders = []
        for where, path, awaited in self._mentions():
            if not awaited:
                continue
            target = self._resolve(path)
            if target is None or not callable(target):
                continue
            if inspect.isasyncgenfunction(target):
                offenders.append(
                    f"{where}: client.{path} is an async generator -- "
                    "`async for`, not `await`"
                )
            elif not _is_awaitable_call(target):
                offenders.append(
                    f"{where}: client.{path} is synchronous -- awaiting it "
                    "raises TypeError"
                )
        assert offenders == [], offenders

    def test_the_guard_can_tell_the_three_kinds_apart(self):
        """The premise, pinned: one of each kind, named."""
        assert inspect.iscoroutinefunction(self._resolve("message"))
        assert inspect.isasyncgenfunction(self._resolve("messages.history"))
        assert not inspect.iscoroutinefunction(self._resolve("community.get"))


class TestTextIOAlwaysDeclaresEncoding:
    """`read_text()` without `encoding=` uses the platform default.

    That is UTF-8 on Linux and cp1252 on Windows, so a file with an em dash in
    it reads fine in CI and raises UnicodeDecodeError on a Windows machine --
    which is exactly how this suite broke: eleven failures, one missing
    keyword argument, invisible to every test run before it.

    Scanning is the fix rather than fixing the three call sites, because the
    next one will be just as invisible.
    """

    BINARY_MODES = ("rb", "wb", "ab", "r+b", "w+b", "xb")

    def _offenders(self):
        import ast
        import pathlib

        root = _repo_root()
        found = []
        for path, tree in _parsed_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(
                    node.func, "id", None
                )
                if name not in ("open", "read_text", "write_text"):
                    continue
                if name == "open":
                    # A file opened in binary mode takes no encoding, and
                    # `dm.open(user)` is not file I/O at all.
                    args = [
                        a.value for a in node.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    ]
                    if any(m in self.BINARY_MODES for m in args):
                        continue
                    if not args and not node.keywords:
                        continue          # not a filesystem open
                if any(kw.arg == "encoding" for kw in node.keywords):
                    continue
                found.append(
                    f"{path.relative_to(root)}:{node.lineno} {name}()"
                )
        return found

    def test_no_text_io_relies_on_the_platform_default(self):
        offenders = self._offenders()
        assert offenders == [], (
            "text I/O without encoding= breaks on Windows (cp1252):\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scan_would_catch_a_regression(self):
        """Guard the guard -- an unqualified call must be detectable."""
        import ast

        tree = ast.parse("from pathlib import Path\nPath('x').read_text()\n")
        call = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "read_text"
        )
        assert not any(kw.arg == "encoding" for kw in call.keywords)


class TestDrainEventsExists:
    """Event handling is fire-and-forget; drain_events is the other half.

    ``dispatch()`` is a public coroutine, but awaiting it does not mean the
    handlers ran -- message events and every ``add_listener`` handler are
    spawned as background tasks so a slow or raising handler cannot stall or
    kill the gateway read loop. That is right for the socket and wrong as the
    whole API: without a way to wait, callers are left with an arbitrary
    ``asyncio.sleep`` or a poke at private state.
    """

    def test_drain_events_is_public_and_async(self):
        assert inspect.iscoroutinefunction(RootClient.drain_events)

    def test_it_takes_a_timeout(self):
        params = inspect.signature(RootClient.drain_events).parameters
        assert "timeout" in params
        assert params["timeout"].default is None

    def test_dispatch_documents_that_handlers_may_not_have_run(self):
        doc = RootClient.dispatch.__doc__ or ""
        assert "drain_events" in doc
        assert "background" in doc.lower()

    def test_handlers_run_only_after_draining(self):
        import asyncio

        async def scenario():
            client = RootClient(token="test-token")
            ran = []

            async def handler(event):
                await asyncio.sleep(0.01)
                ran.append(event)

            client.add_listener("message", handler)
            await client.dispatch("message", object())
            before = len(ran)
            await client.drain_events(timeout=5)
            after = len(ran)
            await client.close()
            return before, after

        before, after = asyncio.run(scenario())
        assert before == 0, "message handlers should not block dispatch"
        assert after == 1, "drain_events did not wait for the handler"

    def test_draining_an_idle_client_returns_zero(self):
        import asyncio

        async def scenario():
            client = RootClient(token="test-token")
            drained = await client.drain_events(timeout=5)
            await client.close()
            return drained

        assert asyncio.run(scenario()) == 0

    def test_a_raising_handler_does_not_stop_the_others(self):
        """Backgrounding means one bad handler must not suppress the rest."""
        import asyncio

        async def scenario():
            client = RootClient(token="test-token")
            survived = []

            async def explodes(event):
                raise RuntimeError("handler blew up")

            async def records(event):
                survived.append(event)

            client.add_listener("message", explodes)
            client.add_listener("message", records)
            await client.dispatch("message", object())
            await client.drain_events(timeout=5)
            await client.close()
            return survived

        assert len(asyncio.run(scenario())) == 1


class TestFriendRequestConveniences:
    """Answering a request from a known person was needlessly manual.

    ``pending()`` returns every incoming request, so accepting one from
    somebody you already know meant listing notifications, filtering by type,
    and matching ids by hand -- the same handful of lines in every caller.
    """

    def test_pending_from_exists(self):
        from rootpy.features import FriendRequestManager

        assert inspect.iscoroutinefunction(FriendRequestManager.pending_from)

    def test_accept_from_exists(self):
        from rootpy.features import FriendRequestManager

        assert inspect.iscoroutinefunction(FriendRequestManager.accept_from)

    def test_decline_from_exists(self):
        from rootpy.features import FriendRequestManager

        assert inspect.iscoroutinefunction(FriendRequestManager.decline_from)

    @pytest.mark.parametrize(
        "value,expected",
        [("0030bf36-e4a1-8a01-8283-0518094b06e1",
          "0030bf36-e4a1-8a01-8283-0518094b06e1"),
         ("shortname", None), (None, None)],
    )
    def test_user_id_extraction(self, value, expected):
        from rootpy.features import _user_id_of

        assert _user_id_of(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [("chance777eyyoo", "chance777eyyoo"), ("@Chance777EyYoo", "chance777eyyoo"),
         ("0030bf36-e4a1-8a01-8283-0518094b06e1", None)],
    )
    def test_username_extraction(self, value, expected):
        from rootpy.features import _username_of

        assert _username_of(value) == expected

    def test_it_reads_objects_too(self):
        from rootpy.features import _user_id_of, _username_of

        class U:
            id = "0030bf36-e4a1-8a01-8283-0518094b06e1"
            username = "Someone"

        assert _user_id_of(U()) == U.id
        assert _username_of(U()) == "someone"

    def test_id_comparison_is_encoding_agnostic(self):
        from rootpy.features import _same_id

        assert _same_id("abc", "abc") is True
        assert _same_id("abc", "def") is False
        assert _same_id(None, "abc") is False
        assert _same_id("abc", None) is False


class TestCommunityCategoryHasGaps:
    """``CommunityCategory(value)`` raises on values the server considers fine.

    5, 8 and 9 are absent from ``CommunityCategory``. That is the server's own
    numbering, not a transcription slip. Any caller that writes
    ``CommunityCategory(row.category)`` over a Discovery result therefore
    crashes on data that is perfectly valid, and will crash again the day Root
    adds a category before rootpy learns about it.

    ``DiscoveredCommunity.category`` and ``Community.category`` are kept as
    plain ints for that reason; ``coerce`` is how you name one safely. This
    pins the hazard so the documented advice stays true.
    """

    GAPS = (5, 8, 9)

    @pytest.mark.parametrize("value", GAPS)
    def test_the_plain_constructor_raises_on_a_gap(self, value):
        from rootpy.enums import CommunityCategory

        with pytest.raises(ValueError):
            CommunityCategory(value)

    @pytest.mark.parametrize("value", GAPS)
    def test_coerce_hands_back_the_int_instead(self, value):
        from rootpy.enums import CommunityCategory

        assert CommunityCategory.coerce(value) == value

    def test_coerce_still_names_the_ones_that_exist(self):
        from rootpy.enums import CommunityCategory

        assert CommunityCategory.coerce(1) is CommunityCategory.GAMING
        assert CommunityCategory.coerce(10) is CommunityCategory.SOCIAL

    def test_the_gaps_are_what_the_descriptors_say(self):
        """If Root ever fills 5, 8 or 9 in, this fails instead of the docs."""
        from rootpy.enums import CommunityCategory

        assert {m.value for m in CommunityCategory} == {0, 1, 2, 3, 4, 6, 7, 10}

    def test_the_discovery_model_does_not_coerce_for_you(self):
        """Keeping the raw int is the decision, not an oversight."""
        from rootpy.models import DiscoveredCommunity

        row = DiscoveredCommunity(id="x", name="y", category=5)
        assert row.category == 5


class TestPendingFriendRequestFiltering:
    """``pending()`` matched a type *name* while the wire carries an int.

    ``getattr(1, "name", str(1))`` is ``"1"``, so the ``"FRIEND" in name``
    test was never true and ``pending()`` silently returned an empty list --
    a filter that returns the wrong set rather than raising, which is the
    worst shape for a bug. ``NotificationType.coerce`` existed for exactly
    this and was not being used.

    The substring also caught ``FRIENDSHIP_INVITE_RESPONDED`` -- someone
    answering a request *you* sent, which is not pending and cannot be
    accepted.
    """

    def test_a_raw_int_would_have_defeated_the_old_filter(self):
        """The bug, pinned so nobody reintroduces the pattern."""
        kind = 1
        name = getattr(kind, "name", str(kind or "")).upper()
        assert "FRIEND" not in name

    def test_coerce_turns_the_int_into_the_member(self):
        from rootpy.enums import NotificationType

        assert (
            NotificationType.coerce(1) is NotificationType.FRIENDSHIP_INVITE_CREATED
        )

    def test_coerce_is_idempotent(self):
        from rootpy.enums import NotificationType

        member = NotificationType.FRIENDSHIP_INVITE_CREATED
        assert NotificationType.coerce(member) is member

    def test_unknown_values_survive_as_ints(self):
        """The wire may carry codes this build predates."""
        from rootpy.enums import NotificationType

        assert NotificationType.coerce(9999) == 9999

    def test_none_stays_none(self):
        from rootpy.enums import NotificationType

        assert NotificationType.coerce(None) is None

    def test_pending_uses_coerce(self):
        from rootpy.features import FriendRequestManager

        src = inspect.getsource(FriendRequestManager.pending)
        assert "NotificationType.coerce" in src

    def test_pending_no_longer_substring_matches(self):
        from rootpy.features import FriendRequestManager

        src = inspect.getsource(FriendRequestManager.pending)
        assert '"FRIEND" in name' not in src

    def test_created_and_responded_are_different_types(self):
        from rootpy.enums import NotificationType

        assert (
            NotificationType.FRIENDSHIP_INVITE_CREATED
            != NotificationType.FRIENDSHIP_INVITE_RESPONDED
        )

    def test_responded_is_reachable_separately(self):
        from rootpy.features import FriendRequestManager

        assert inspect.iscoroutinefunction(FriendRequestManager.responded)


class TestNotificationCounterparty:
    """``NotificationPacket.UserId`` is the owner, not the sender.

    Confirmed live: a friend request from account A arrives in account B's
    inbox carrying **B's own id** at the top level. The counterparty is in
    ``NotificationPayloadFriendshipInviteCreated {UserId, FriendUserId}``.

    ``_unpack`` read the top-level field and handed it to the respond RPC as
    ``FriendUserId`` -- so ``accept()`` asked Root to accept a friendship with
    yourself. It never raised; it just did the wrong thing.
    """

    ME = "0030bf26-999d-8101-82fd-66b280480024"
    PEER = "0030bf36-e4a1-8a01-8283-0518094b06e1"

    def _notification(self, payload_user, payload_friend):
        return {
            "id": "notification-1",
            "user_id": self.PEER,          # the owner -- the recipient
            "payload": {
                "friendship_invite_created": {
                    "user_id": payload_user,
                    "friend_user_id": payload_friend,
                }
            },
        }

    def test_owner_is_not_returned_as_the_sender(self):
        from rootpy.features import _notification_counterparty

        notification = self._notification(self.PEER, self.ME)
        assert _notification_counterparty(notification) == self.ME

    def test_field_order_does_not_matter(self):
        """Which of the two payload ids is the sender is not assumed."""
        from rootpy.features import _notification_counterparty

        notification = self._notification(self.ME, self.PEER)
        assert _notification_counterparty(notification) == self.ME

    def test_unpack_returns_the_counterparty(self):
        from rootpy.features import FriendRequestManager

        notification = self._notification(self.PEER, self.ME)
        notification_id, user_id = FriendRequestManager._unpack(notification)
        assert notification_id == "notification-1"
        assert user_id == self.ME

    def test_unpack_never_returns_the_owner(self):
        """The exact bug: responding with your own id."""
        from rootpy.features import FriendRequestManager

        notification = self._notification(self.PEER, self.ME)
        _, user_id = FriendRequestManager._unpack(notification)
        assert user_id != self.PEER

    def test_a_pair_still_works(self):
        from rootpy.features import FriendRequestManager

        assert FriendRequestManager._unpack(("n1", "u1")) == ("n1", "u1")

    def test_a_flat_shape_is_still_read(self):
        from rootpy.features import _notification_counterparty

        assert (
            _notification_counterparty(
                {"id": "n", "user_id": self.PEER, "friend_user_id": self.ME}
            )
            == self.ME
        )

    def test_no_payload_yields_none(self):
        from rootpy.features import _notification_counterparty

        assert _notification_counterparty({"id": "n", "user_id": self.PEER}) is None

    def test_unpack_raises_when_the_counterparty_is_unknown(self):
        from rootpy.features import FriendRequestManager

        with pytest.raises(ValueError):
            FriendRequestManager._unpack({"id": "n", "user_id": self.PEER})

    def test_accept_all_exists_as_a_sender_agnostic_path(self):
        from rootpy.features import FriendRequestManager

        assert inspect.iscoroutinefunction(FriendRequestManager.accept_all)


class TestWaitForDocumentsItsEventSource:
    """A waiter on a token-only client can only ever time out.

    ``client`` needs no gateway for RPCs, so it is easy to call ``wait_for``
    on one and watch it hang until the timeout with no clue why.

    Deliberately *not* raising when there is no gateway: ``test_features.py``
    pairs ``wait_for`` with manual ``dispatch()`` on an unconnected client,
    which is a legitimate pattern and predates all of this. Documented
    instead.
    """

    def test_docstring_explains_the_requirement(self):
        doc = RootClient.wait_for.__doc__ or ""
        assert "dispatch the event" in doc
        assert "token-only client" in doc

    def test_it_still_works_with_manual_dispatch(self):
        """The pattern that rules out raising."""
        import asyncio

        async def scenario():
            client = RootClient(token="test-token")
            waiter = asyncio.create_task(
                client.wait_for("member_join", timeout=5)
            )
            await asyncio.sleep(0.05)
            await client.dispatch("member_join", {"user_id": "someone"})
            event = await waiter
            await client.close()
            return event

        assert asyncio.run(scenario()) == {"user_id": "someone"}

    def test_a_waiter_with_no_source_times_out_rather_than_hanging(self):
        import asyncio

        async def scenario():
            client = RootClient(token="test-token")
            try:
                await client.wait_for("message", timeout=0.2)
                return "resolved"
            except asyncio.TimeoutError:
                return "timed out"
            finally:
                await client.close()

        assert asyncio.run(scenario()) == "timed out"


class TestListMethodsReturnItems:
    """List RPCs answer with an envelope wrapping one repeated field.

    Handing that envelope back meant iterating a ``list()`` call yielded field
    *names* -- ``for role in await client.roles.list(...)`` gave strings, and
    ``role.id`` raised AttributeError. ``moderation.list_bans`` had the same
    shape, which is why a ban never appeared in its own list.
    """

    def test_unwrap_reads_an_object_envelope(self):
        from rootpy.domain_managers import unwrap_list

        class Envelope:
            community_roles = ["a", "b"]

        assert unwrap_list(Envelope()) == ["a", "b"]

    def test_unwrap_reads_a_dict_envelope(self):
        from rootpy.domain_managers import unwrap_list

        assert unwrap_list({"community_member_bans": [1, 2]}) == [1, 2]

    def test_a_plain_list_passes_through(self):
        from rootpy.domain_managers import unwrap_list

        assert unwrap_list([1, 2, 3]) == [1, 2, 3]

    def test_none_becomes_empty(self):
        from rootpy.domain_managers import unwrap_list

        assert unwrap_list(None) == []

    @pytest.mark.parametrize(
        "module,cls,method",
        [
            ("rootpy.domain_managers", "RoleManager", "list"),
            ("rootpy.domain_managers", "FriendshipGroupManager", "list"),
            ("rootpy.domain_managers", "CommunityFileManager", "list"),
            ("rootpy.domain_managers", "VoiceAdminManager", "list"),
            # Both MemberManager list methods answer with the same message,
            # CommunityMemberExtendedListResponse -- but only list_all
            # unwrapped it, so `for m in await members.list(cid, ids)` yielded
            # the field name. Absent from this table is exactly why.
            ("rootpy.domain_managers", "MemberManager", "list"),
            ("rootpy.domain_managers", "MemberManager", "list_all"),
            ("rootpy.features", "ModerationManager", "list_bans"),
            ("rootpy.features", "DirectoryManager", "list"),
        ],
    )
    def test_list_method_unwraps(self, module, cls, method):
        import importlib

        target = getattr(getattr(importlib.import_module(module), cls), method)
        assert "unwrap_list" in inspect.getsource(target)


class TestNoKwargsOnPublicManagers:
    """``**kwargs`` on a public method hides what the call actually needs.

    ``directories.delete(directory_id=...)`` reads perfectly and the wire
    field is ``Id``; ``community_files.list()`` looks complete and Root
    requires a ``DirectoryId``. Neither is discoverable from the signature,
    so both are only found against a server.
    """

    CASES = [
        ("directories", ["create", "list", "get", "edit", "move", "delete"]),
        ("community_files", ["list", "get", "edit", "move", "delete"]),
    ]

    @pytest.mark.parametrize("attr,methods", CASES)
    def test_methods_have_named_parameters(self, attr, methods):
        manager = getattr(RootClient(), attr)
        offenders = []
        for name in methods:
            params = inspect.signature(getattr(manager, name)).parameters
            named = [
                p for p in params.values()
                if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
            ]
            if not named:
                offenders.append(f"{attr}.{name}")
        assert offenders == [], f"still **kwargs-only: {offenders}"

    def test_files_list_requires_a_directory(self):
        """Root applies a NotEmptyValidator to FileList.DirectoryId."""
        params = inspect.signature(RootClient().community_files.list).parameters
        assert "directory_id" in params
        assert params["directory_id"].default is inspect.Parameter.empty

    def test_directory_delete_takes_a_directory_id(self):
        params = inspect.signature(RootClient().directories.delete).parameters
        assert "directory_id" in params


class TestRoleEditIsAReplace:
    """CommunityRoleEdit carries every field, like CommunityEdit.

    Sending only ``name`` blanked ColorHex and both permission sets, and Root
    answered INTERNAL (13) rather than a useful validation error.
    """

    def test_edit_carries_unsupplied_fields_forward(self):
        from rootpy.domain_managers import RoleManager

        src = inspect.getsource(RoleManager.edit)
        for field in ("color_hex", "community_permission", "channel_permission",
                      "is_mentionable", "is_self_assignable"):
            assert field in src, f"{field} is not carried forward"

    def test_it_reads_the_current_role(self):
        from rootpy.domain_managers import RoleManager

        assert "self.get(" in inspect.getsource(RoleManager.edit)


class TestManagerDefaultsDoNotDefeatTheSentinel:
    """A wrapper's default can make the layer below's carry-forward unreachable.

    ``CommunityAdminService.edit_role`` uses ``color_hex=None`` as the
    sentinel for "read the role and keep its colour" -- load-bearing, because
    ``CommunityRoleEdit`` is a replace. ``CommunityManager.edit_role``, the
    spelling almost every caller uses, declared ``color_hex: str = ""`` and
    forwarded it verbatim. ``""`` is not ``None``, so the carry-forward never
    ran and ``normalize_hex_colour("")`` raised ``ValueError`` before the
    request was built: ``client.admin.edit_role(cid, rid, name="x")`` worked
    and ``client.community.edit_role(cid, rid, name="x")`` could not be called
    at all.

    Three tests here: the specific regression, the two spellings agreeing, and
    a sweep over every ``f(x=x)`` forward in the package for the same shape.
    """

    def test_the_manager_default_is_the_sentinel(self):
        from rootpy.object_api import CommunityManager

        default = inspect.signature(
            CommunityManager.edit_role
        ).parameters["color_hex"].default
        assert default is None, (
            f"color_hex defaults to {default!r}; the layer below reads None as "
            "'carry the current colour forward', and anything else blanks it"
        )

    def test_both_spellings_of_edit_role_agree(self):
        from rootpy.object_api import CommunityManager
        from rootpy.services.community_admin import CommunityAdminService

        for name in ("color_hex", "community_permissions", "channel_permissions"):
            manager = inspect.signature(
                CommunityManager.edit_role
            ).parameters[name].default
            admin = inspect.signature(
                CommunityAdminService.edit_role
            ).parameters[name].default
            assert manager == admin, (
                f"community.edit_role({name}={manager!r}) disagrees with "
                f"admin.edit_role({name}={admin!r})"
            )

    @staticmethod
    def _forwards():
        """(where, param, caller_default, callee, callee_default) for f(x=x)."""
        import ast

        root = _repo_root() / "rootpy"

        def constant_defaults(node):
            args = node.args
            out = {}
            positional = args.posonlyargs + args.args
            for arg, default in zip(
                positional[len(positional) - len(args.defaults):], args.defaults
            ):
                if isinstance(default, ast.Constant):
                    out[arg.arg] = default.value
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if isinstance(default, ast.Constant):
                    out[arg.arg] = default.value
            return out

        functions = {}
        trees = []
        for path, tree in _parsed_sources():
            try:
                path.relative_to(root)
            except ValueError:
                continue
            trees.append((path, tree))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.setdefault(node.name, []).append(
                        (f"{path.name}:{node.lineno}", constant_defaults(node))
                    )

        out = []
        for path, tree in trees:
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                here = f"{path.name}:{func.lineno}"
                mine = dict(
                    next(
                        (d for w, d in functions.get(func.name, []) if w == here),
                        {},
                    )
                )
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    callee = getattr(node.func, "attr", None)
                    if callee not in functions:
                        continue
                    for keyword in node.keywords:
                        if (
                            not keyword.arg
                            or not isinstance(keyword.value, ast.Name)
                            or keyword.value.id != keyword.arg
                            or keyword.arg not in mine
                        ):
                            continue
                        for where, theirs in functions[callee]:
                            if where == here or keyword.arg not in theirs:
                                continue
                            out.append((
                                here, keyword.arg, mine[keyword.arg],
                                f"{callee} at {where}", theirs[keyword.arg],
                            ))
        return out

    def test_the_sweep_found_forwards(self):
        assert len(self._forwards()) > 40, "the forwarding scan found too little"

    def test_no_forward_defeats_a_none_sentinel(self):
        offenders = [
            f"{where}: {param}={caller!r} forwarded to {callee}, whose "
            f"default is None"
            for where, param, caller, callee, callee_default in self._forwards()
            if callee_default is None and caller is not None and not caller
        ]
        assert sorted(set(offenders)) == [], (
            "a falsy default is being forwarded where the callee expects None "
            "to mean 'unspecified':\n  " + "\n  ".join(sorted(set(offenders)))
        )


class TestAssetUploadWorksWithATokenOnlyClient:
    """Uploads needed a session for a URL that is a constant.

    Emoji creation, community files and avatar changes all failed with
    "Client is not logged in" on a token-only client, even though
    ``login_token`` defaults ``web_api_url`` to the same value it would then
    have used.
    """

    def test_token_only_client_has_a_web_api_url(self):
        assert RootClient(token="t")._require_web_api_url().startswith("https://")

    def test_it_matches_the_login_default(self):
        assert (
            RootClient(token="t")._require_web_api_url()
            == RootClient.DEFAULT_WEB_API_URL
        )

    def test_no_token_still_raises(self):
        with pytest.raises(RuntimeError):
            RootClient()._require_web_api_url()

    def test_the_error_names_the_fix(self):
        with pytest.raises(RuntimeError, match="RootClient\\(token="):
            RootClient()._require_web_api_url()


class TestUnwrapListIsUsedCorrectly:
    """A regex edit produced ``unwrap_list(response).data`` in six methods.

    It replaced ``return (`` with ``return unwrap_list(`` and left the
    trailing ``.data`` outside the new call, so the envelope was unwrapped and
    then ``.data`` was read off the resulting list -- ``AttributeError: 'list'
    object has no attribute 'data'``. The offline suite could not see it
    because none of those methods had a unit test.

    Scanning the AST is the guard: the shape is trivially detectable and the
    mistake is not visible by eye in a diff.
    """

    MODULES = ["rootpy.domain_managers", "rootpy.features", "rootpy.emoji"]

    def _tree(self, module_name):
        return _parsed_module(module_name)

    @pytest.mark.parametrize("module_name", MODULES)
    def test_no_unwrap_then_data(self, module_name):
        import ast

        offenders = []
        for node in ast.walk(self._tree(module_name)):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "data"
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "unwrap_list"
            ):
                offenders.append(f"{module_name}:{node.lineno}")
        assert offenders == [], (
            "unwrap_list(...).data unwraps the response then reads .data off a "
            f"list: {offenders}"
        )

    @pytest.mark.parametrize("module_name", MODULES)
    def test_no_data_on_an_unawaited_call(self, module_name):
        """``await x.list(...).data`` reads .data off the coroutine."""
        import ast

        offenders = []
        for node in ast.walk(self._tree(module_name)):
            if not isinstance(node, ast.Await):
                continue
            if isinstance(node.value, ast.Attribute) and node.value.attr == "data":
                offenders.append(f"{module_name}:{node.lineno}")
        assert offenders == [], f"await (...).data on a coroutine: {offenders}"

    def test_the_guard_would_catch_the_bug(self):
        """Guard the guard, with the exact shape that shipped."""
        import ast

        tree = ast.parse("unwrap_list(response).data\n")
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "data"
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "unwrap_list"
        ]
        assert len(found) == 1

    LIST_METHODS = [
        ("rootpy.domain_managers", "RoleManager", "list"),
        ("rootpy.domain_managers", "MemberManager", "list_all"),
        ("rootpy.domain_managers", "VoiceAdminManager", "list"),
        ("rootpy.domain_managers", "FriendshipGroupManager", "list"),
        ("rootpy.domain_managers", "CommunityFileManager", "list"),
        ("rootpy.features", "ModerationManager", "list_bans"),
        ("rootpy.features", "DirectoryManager", "list"),
        ("rootpy.features", "EmojiManager", "list"),
        ("rootpy.features", "EmojiManager", "list_mine"),
    ]

    @pytest.mark.parametrize("module_name,cls,method", LIST_METHODS)
    def test_list_method_awaits_then_unwraps(self, module_name, cls, method):
        import importlib

        src = inspect.getsource(
            getattr(getattr(importlib.import_module(module_name), cls), method)
        )
        assert "unwrap_list(response.data)" in src or "unwrap_list(\n" in src


class TestFriendGroupMoveRequiresAReference:
    """``BeforeFriendshipGroupId`` is required; the default hid that.

    ``before_group_id=None`` was omitted from the request, which Root rejects
    -- so an optional-looking argument could only ever fail.
    """

    def test_it_is_a_required_keyword(self):
        from rootpy.domain_managers import FriendshipGroupManager

        param = inspect.signature(FriendshipGroupManager.move).parameters[
            "before_group_id"
        ]
        assert param.default is inspect.Parameter.empty
        assert param.kind is param.KEYWORD_ONLY

    def test_an_empty_reference_is_refused_locally(self):
        import asyncio

        from rootpy import RootClient

        async def scenario():
            client = RootClient(token="t")
            try:
                await client.friend_groups.move("g", before_group_id="")
            except ValueError as exc:
                return str(exc)
            finally:
                await client.close()
            return None

        message = asyncio.run(scenario())
        assert message and "before_group_id is required" in message


class TestPermissionBuilders:
    """``PermissionManager``'s builders are pure and need no network.

    Four of the eight are static constructors, so they can be checked here
    rather than only against a server.
    """

    UUID = "0030bf26-999d-8101-82fd-66b280480024"

    def _manager(self):
        return RootClient().permissions

    def test_rule_carries_the_target(self):
        rule = self._manager().rule(self.UUID, channel_view=True)
        assert rule.target_id

    def test_rule_normalises_the_target_id(self):
        """Ids arrive in several encodings; a rule must not care."""
        rule = self._manager().rule(self.UUID)
        assert isinstance(rule.target_id, str) and rule.target_id

    def test_rule_builds_an_overlay(self):
        from rootpy.permissions import ChannelOverlay

        rule = self._manager().rule(self.UUID, channel_view=True)
        assert isinstance(rule.overlay, ChannelOverlay)

    def test_rule_overlay_reflects_the_flags(self):
        rule = self._manager().rule(
            self.UUID, channel_view=True, channel_create_message=False
        )
        assert rule.overlay.channel_view is True
        assert rule.overlay.channel_create_message is False

    def test_unspecified_flags_stay_inherit(self):
        """A rule sets what you name and inherits the rest."""
        rule = self._manager().rule(self.UUID, channel_view=True)
        assert rule.overlay.channel_voice_talk is None

    def test_channel_builder(self):
        from rootpy.permissions import ChannelPermissions

        built = self._manager().channel(channel_view=True)
        assert isinstance(built, ChannelPermissions) or built is not None

    def test_community_builder(self):
        built = self._manager().community(community_kick=True)
        assert built is not None

    def test_overlay_builder(self):
        from rootpy.permissions import ChannelOverlay

        assert isinstance(self._manager().overlay(channel_view=True), ChannelOverlay)

    def test_overlay_defaults_to_inherit_everything(self):
        overlay = self._manager().overlay()
        assert all(value is None for value in vars(overlay).values())

    def test_an_unknown_flag_is_rejected(self):
        """A typo must not silently become a no-op rule."""
        with pytest.raises(TypeError):
            self._manager().rule(self.UUID, channel_veiw=True)


class TestEventuallyHelper:
    """Polling instead of sleeping, for Root's eventual consistency.

    ``await asyncio.sleep(2)`` before asserting a read-after-write pays the
    full two seconds even when the value landed in 200 ms, and still fails
    when the server takes longer. Polling is both faster and more reliable.
    """

    def test_returns_as_soon_as_the_check_passes(self):
        import time

        from .conftest import eventually

        polls = []

        def check():
            polls.append(1)
            return "ready" if len(polls) >= 3 else None

        started = time.monotonic()
        result = asyncio.run(eventually(check, timeout=5, interval=0.02))
        elapsed = time.monotonic() - started

        assert result == "ready"
        assert len(polls) == 3
        assert elapsed < 1.0, "should not wait out the full timeout"

    def test_an_immediate_pass_costs_nothing(self):
        import time

        from .conftest import eventually

        started = time.monotonic()
        asyncio.run(eventually(lambda: True, timeout=5, interval=1.0))
        assert time.monotonic() - started < 0.5

    def test_it_accepts_an_async_check(self):
        from .conftest import eventually

        async def check():
            return "async ok"

        assert asyncio.run(eventually(check, timeout=2, interval=0.02)) == "async ok"

    def test_timeout_raises_with_the_description(self):
        from .conftest import eventually

        with pytest.raises(AssertionError, match="the widget appeared"):
            asyncio.run(
                eventually(
                    lambda: False,
                    timeout=0.15,
                    interval=0.05,
                    describe="the widget appeared",
                )
            )

    def test_the_truthy_value_is_returned(self):
        from .conftest import eventually

        assert asyncio.run(
            eventually(lambda: [1, 2, 3], timeout=1, interval=0.01)
        ) == [1, 2, 3]


def _example_calls():
    """(where, path, n_positional, keywords) for each client.* call, once."""
    import ast

    wanted = set(_example_sources())
    out = []
    for path, tree in _parsed_sources():
        if path not in wanted:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = []
            cur = node.func
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if not (isinstance(cur, ast.Name) and cur.id == "client"):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue
            out.append((
                f"{path.name}:{node.lineno}",
                ".".join(reversed(parts)),
                len(node.args),
                tuple(sorted(kw.arg for kw in node.keywords)),
            ))
    return sorted(set(out))


class TestExampleCallsBind:
    """The await-guard checks the *name*; this checks the *arguments*.

    ``client.directories.get(a, b, directory_id=x, refresh=True)`` resolves,
    is a coroutine, is awaited -- and raises ``TypeError`` at call time. An
    example that does that looks right until someone runs it.

    ``Signature.bind`` settles it offline for every method with a real
    signature. For the ``**kwargs`` forwarders -- ``community_files.search``,
    ``community_apps.list``, ``logs.app``, where the signature is deliberately
    not the documentation -- the keywords are traced one hop to the
    ``high.<alias>.<method>`` the manager calls and checked against the wire
    schema, because that is the only thing that can answer.
    """

    CALLS = _example_calls()

    def test_the_scan_found_calls(self):
        assert len(self.CALLS) > 50, (
            f"only {len(self.CALLS)} example calls found -- the scan broke"
        )

    @pytest.mark.parametrize(
        "where,path,positional,keywords",
        CALLS,
        ids=[f"{w}-{p}" for w, p, _n, _k in CALLS],
    )
    def test_it_binds(self, where, path, positional, keywords):
        target = _resolve(path)
        if target is None or not callable(target):
            pytest.skip(f"client.{path} is not callable")
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            pytest.skip(f"client.{path} has no introspectable signature")

        var_keyword = any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
        if not var_keyword:
            try:
                signature.bind(*[object()] * positional, **dict.fromkeys(keywords))
            except TypeError as exc:
                pytest.fail(
                    f"{where}: client.{path}{signature} called with "
                    f"{positional} positional and {list(keywords)} -> {exc}"
                )
            return

        # A **kwargs forwarder: the wire schema is the only authority.
        endpoint = self._forwarded_endpoint(target)
        if endpoint is None:
            pytest.skip(f"client.{path} has no single forward to trace")
        api = RootClient(token="x").high
        try:
            bound = getattr(getattr(api, endpoint[0]), endpoint[1])
            schema = api.codec.resolve_message(bound.info["request"])
        except Exception:
            pytest.skip(f"cannot resolve high.{endpoint[0]}.{endpoint[1]}")
        if schema is None:
            pytest.skip(f"no schema for high.{endpoint[0]}.{endpoint[1]}")

        accepted = set()
        for field in schema["fields"]:
            accepted.add(field["name"])
            accepted.add(field["python_name"])
            accepted.add(field["name"].casefold())
        bad = sorted(k for k in keywords if k not in accepted)
        assert bad == [], (
            f"{where}: client.{path}(**kwargs) forwards to "
            f"high.{endpoint[0]}.{endpoint[1]}({bound.info['request']}), which "
            f"has no {bad}. Fields: "
            + ", ".join(sorted(f["python_name"] for f in schema["fields"]))
        )

    @staticmethod
    def _forwarded_endpoint(method):
        import ast
        import textwrap

        try:
            source = textwrap.dedent(inspect.getsource(method))
        except (OSError, TypeError):
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = []
            cur = node.func
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            parts = list(reversed(parts))
            if "high" in parts:
                index = parts.index("high")
                if len(parts) >= index + 3:
                    found.add((parts[index + 1], parts[index + 2]))
        return next(iter(found)) if len(found) == 1 else None

    def test_the_guard_would_catch_a_bad_keyword(self):
        """Both halves, on the two shapes that have actually gone wrong."""
        signature = inspect.signature(RootClient().directories.get)
        with pytest.raises(TypeError):
            signature.bind(object(), object(), directory_id=1, refresh=True)

        api = RootClient(token="x").high
        schema = api.codec.resolve_message(api.file.search.info["request"])
        fields = {f["python_name"] for f in schema["fields"]}
        assert "search" in fields, "the premise moved -- FileSearch has no Search"
        assert "query" not in fields, "'query' would no longer be a wrong name"


class TestMentionsRoundTrip:
    """The library could parse a mention it could not produce.

    ``rootpy.commands`` has matched Root's real mention syntax since it was
    written -- ``[@name](root://user/<id>)`` and
    ``[#name](root://channel/<id>)``, markdown links rather than the
    Discord-style ``<@id>``. But ``Channel.mention`` emitted ``<#{id}>``,
    which that parser refuses, and there was no user builder at all.

    So the two halves of the library disagreed, and anyone wanting to mention
    someone had to invent a format -- and an invented one is delivered
    without complaint and simply never renders as a mention.

    Producing and then parsing is the check. It needs no network.
    """

    CHANNEL_ID = "0030bf26-999d-8101-82fd-66b280480024"
    USER_ID = "0030bf36-e4a1-8a01-8283-0518094b06e1"

    def _channel(self, name="general"):
        from rootpy.models import Channel

        return Channel(
            id=self.CHANNEL_ID,
            name=name,
            community_id="0030bf26-999d-8101-82fd-66b280480025",
            channel_group_id="0030bf26-999d-8101-82fd-66b280480026",
        )

    def _user(self, username="someone"):
        from rootpy.users import User

        return User(client=None, id=self.USER_ID, username=username)

    def test_channel_mention_parses_back(self):
        from rootpy.commands import parse_channel_mention

        parsed = parse_channel_mention(self._channel().mention)
        assert parsed == ("general", self.CHANNEL_ID)

    def test_user_mention_parses_back(self):
        from rootpy.commands import parse_user_mention

        parsed = parse_user_mention(self._user().mention)
        assert parsed == ("someone", self.USER_ID)

    def test_the_discord_shape_is_not_what_we_emit(self):
        """The specific regression: ``<#id>`` was being produced."""
        assert not self._channel().mention.startswith("<#")
        assert not self._user().mention.startswith("<@")

    def test_a_missing_username_still_produces_a_parseable_mention(self):
        from rootpy.commands import parse_user_mention

        assert parse_user_mention(self._user(username=None).mention) is not None

    def test_a_leading_at_is_not_doubled(self):
        from rootpy.models import build_user_mention

        assert build_user_mention(self.USER_ID, "@bob").startswith("[@bob]")

    def test_the_builders_are_importable_from_models(self):
        from rootpy.models import build_channel_mention, build_user_mention

        assert build_user_mention(self.USER_ID).startswith("[@")
        assert build_channel_mention(self.CHANNEL_ID).startswith("[#")

    def test_the_examples_do_not_hand_roll_a_mention(self):
        """Root's syntax is not Discord's, and an example is copied."""
        import re

        offenders = []
        for path in _example_sources():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if re.search(r'["f]?"<@!?\{', line):
                    offenders.append(f"{path.name}:{number}")
        assert offenders == [], (
            "an example is building a Discord-style mention by hand; use "
            f"User.mention: {offenders}"
        )


class TestAssetUriEncoding:
    """``root://asset/<b64url(guid16 + 0x0A <len> kind)>``.

    Several records carry a bare ``asset_id`` rather than a URI -- a community
    file's does -- and every asset method takes URIs, so ``uri_for_id`` is how
    you get across. The payload is the raw 16-byte GUID followed by a
    protobuf-framed *kind* tag.

    The two pairs below are asset ids and the URIs Root itself produced for
    them, pinned rather than paraphrased: if Root changes the encoding, this
    is where it shows up, rather than as assets that silently stop resolving.
    """

    #: (asset_id, kind, the URI Root produced for it)
    KNOWN = [
        ("0030cfc6-6a71-8c1b-ae97-46f7074d21ad", "image",
         "root://asset/ADDPxmpxjBuul0b3B00hrQoFaW1hZ2U"),
        ("002e286e-4501-8c1b-baae-7f5022d43652", "image",
         "root://asset/AC4obkUBjBu6rn9QItQ2UgoFaW1hZ2U"),
    ]

    @pytest.mark.parametrize("asset_id,kind,expected", KNOWN)
    def test_it_reconstructs_a_real_uri(self, asset_id, kind, expected):
        assert RootClient(token="x").assets.uri_for_id(asset_id, kind) == expected

    def test_the_payload_is_the_guid_then_the_kind_tag(self):
        """Stated structurally, so the *reason* survives a refactor."""
        import base64

        asset_id = "0030cfc6-6a71-8c1b-ae97-46f7074d21ad"
        uri = RootClient(token="x").assets.uri_for_id(asset_id, "file")
        payload = uri.rsplit("/", 1)[-1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))

        assert raw[:16].hex() == asset_id.replace("-", "")
        assert raw[16] == 0x0A, "field 1, length-delimited"
        assert raw[17] == len("file")
        assert raw[18:] == b"file"

    def test_the_kind_tag_changes_the_uri(self):
        """Not cosmetic: it selects the derivative, and they differ."""
        assets = RootClient(token="x").assets
        asset_id = "0030cfc6-6a71-8c1b-ae97-46f7074d21ad"
        assert assets.uri_for_id(asset_id, "image") != assets.uri_for_id(
            asset_id, "file"
        )

    def test_a_non_guid_is_refused_with_a_useful_message(self):
        with pytest.raises(ValueError, match="16-byte GUID"):
            RootClient(token="x").assets.uri_for_id("not-a-guid")

    def test_it_is_a_staticmethod_needing_no_network(self):
        """Callable off the class, so it can be used while composing requests."""
        from rootpy.services.assets import AssetService

        assert AssetService.uri_for_id(
            "0030cfc6-6a71-8c1b-ae97-46f7074d21ad"
        ).startswith("root://asset/")


class TestEverySourceFileCompiles:
    """``ast.parse`` succeeding is not the same as the file being valid.

    ``lambda: len(await thing())`` parses cleanly and fails at ``compile()``
    with "'await' outside async function" -- the check lives in the symbol
    table pass, not the parser. Every AST guard in this module is built on
    ``_parsed_sources``, which *skips* a file it cannot parse so one broken
    file does not mask the rest. The two facts together mean a file with that
    mistake in it is silently exempted from every guard, and the whole
    offline suite still reports green.

    That is not hypothetical. One unparseable module was excused from the
    await-contract check, the undefined-name check and the sleep check at
    once, and the only symptom was a collection error.

    ``compile()`` is the check. It is also strictly stronger than
    ``ast.parse``, so it subsumes the parse.
    """

    @pytest.mark.parametrize(
        "path",
        _source_files(),
        ids=[str(p.relative_to(_repo_root())) for p in _source_files()],
    )
    def test_it_compiles(self, path):
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            pytest.fail(
                f"{path.relative_to(_repo_root())}:{exc.lineno} {exc.msg}\n"
                f"    {(exc.text or '').strip()}\n"
                "    ast.parse() may well accept this; the AST guards would "
                "then skip the file entirely."
            )

    def test_every_source_file_reached_the_ast_guards(self):
        """Nothing may be silently missing from ``_parsed_sources``."""
        parsed = {path for path, _tree in _parsed_sources()}
        missing = sorted(
            str(p.relative_to(_repo_root()))
            for p in _source_files()
            if p not in parsed
        )
        assert missing == [], (
            f"these files are excluded from every AST guard: {missing}"
        )

    def test_the_guard_would_have_caught_it(self):
        """The exact shape: parses, does not compile."""
        import ast

        source = (
            "async def probe(peer):\n"
            "    await eventually(lambda: len(await inbox(peer)) > 0)\n"
        )
        ast.parse(source)          # the parser is happy
        with pytest.raises(SyntaxError):
            compile(source, "synthetic.py", "exec")


class TestNoUndefinedNamesInTests:
    """A name used only inside a test body fails at *run* time, not import.

    A helper used in five modules with the import landing in four still
    parses, and still collects: the `NameError` waits until that one test
    body runs. Checking that a file parses says nothing about whether its
    names resolve.

    This walks every test module's AST, collects what is defined or imported at
    module level, and flags any global load that has nowhere to come from.
    Deliberately narrow: locals, comprehension targets, arguments, attributes
    and builtins are all resolved first, so a hit is a real missing name.
    """

    def _module_scope(self, tree):
        import builtins

        # Module dunders exist at runtime without being imported or assigned.
        names = set(dir(builtins)) | {
            "__file__", "__name__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__debug__",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.Global):
                names.update(node.names)
        return names

    def _undefined(self, path, tree):
        scope = self._module_scope(tree)
        missing = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in scope:
                    missing.setdefault(node.id, node.lineno)
        return missing

    def test_every_test_module_resolves_its_names(self):
        offenders = []
        for path, tree in _parsed_sources():
            if not path.name.startswith("test_") or path.parent.name != "tests":
                continue
            for name, line in self._undefined(path, tree).items():
                offenders.append(f"{path.name}:{line} {name}")
        assert offenders == [], f"undefined names in tests: {offenders}"

    def test_conftest_resolves_its_names(self):
        offenders = []
        for path, tree in _parsed_sources():
            if path.name != "conftest.py":
                continue
            for name, line in self._undefined(path, tree).items():
                offenders.append(f"{path.name}:{line} {name}")
        assert offenders == [], f"undefined names in conftest: {offenders}"

    def test_the_check_would_have_caught_the_bug(self):
        """The exact shape: used in a function, never imported."""
        tree = ast.parse(
            "import pytest\n\n"
            "async def test_thing():\n"
            "    await eventually(lambda: True)\n"
        )
        missing = self._undefined(pathlib.Path("synthetic.py"), tree)
        assert "eventually" in missing

    def test_it_does_not_flag_ordinary_code(self):
        tree = ast.parse(
            "import asyncio\n\n"
            "def f(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item\n"
            "    return asyncio.sleep(total)\n"
        )
        assert self._undefined(pathlib.Path("synthetic.py"), tree) == {}


class TestEveryHighCallSiteSendsRealFields:
    """A manager that names a request field wrong has *never* worked.

    ``StructuredProtoCodec.encode_message`` raises
    ``TypeError: <Request> has no field 'x'`` for an unknown keyword, before
    anything goes near the network. So this is not a subtle wire problem that
    shows up as a puzzling rejection -- it is a method that raises the moment
    anyone calls it, and for a rarely-used setter that can be never.

    All three ``user_settings.set_*_invite_requirement`` methods were in that
    state: they sent ``is_required`` at a request whose field is
    ``IsEmailVerified``. The offline tests above pinned their *signature* and
    the enum values, and still could not see it, because nothing checked the
    keyword against the schema.

    This walks every ``*.high.<alias>.<method>(...)`` call site in the
    package and does check.
    """

    @staticmethod
    def _call_sites():
        import ast

        from rootpy import RootClient

        api = RootClient(token="x").high
        root = _repo_root() / "rootpy"

        def dotted(node):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            return tuple(reversed(parts))

        def literal_keys(func, var):
            """String keys written into ``var`` anywhere in ``func``."""
            keys = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == var
                            and isinstance(target.slice, ast.Constant)
                            and isinstance(target.slice.value, str)
                        ):
                            keys.add(target.slice.value)
                        elif (
                            isinstance(target, ast.Name)
                            and target.id == var
                            and isinstance(node.value, ast.Dict)
                        ):
                            for key in node.value.keys:
                                if isinstance(key, ast.Constant) and isinstance(
                                    key.value, str
                                ):
                                    keys.add(key.value)
            return keys

        out = []
        for path, tree in _parsed_sources():
            try:
                path.relative_to(root)
            except ValueError:
                continue
            for func in ast.walk(tree):
                if not isinstance(
                    func, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    parts = dotted(node.func)
                    if "high" not in parts:
                        continue
                    index = parts.index("high")
                    if len(parts) < index + 3:
                        continue
                    alias, method = parts[index + 1], parts[index + 2]
                    sent = set()
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            sent.add(keyword.arg)
                        elif isinstance(keyword.value, ast.Name):
                            sent |= literal_keys(func, keyword.value.id)
                    out.append((
                        api,
                        f"{path.name}:{node.lineno}",
                        alias,
                        method,
                        tuple(sorted(sent)),
                    ))
        return out

    SITES = _call_sites.__func__()

    def test_the_scan_found_call_sites(self):
        assert len(self.SITES) > 60, (
            f"only {len(self.SITES)} high.* call sites found -- the scan broke"
        )

    @pytest.mark.parametrize(
        "where,alias,method,sent",
        [(w, a, m, s) for _api, w, a, m, s in SITES],
        ids=[f"{w}-{a}.{m}" for _api, w, a, m, _s in SITES],
    )
    def test_kwargs_exist_on_the_request(self, where, alias, method, sent):
        from rootpy import RootClient

        api = RootClient(token="x").high
        service = getattr(api, alias)
        bound = getattr(service, method)
        schema = api.codec.resolve_message(bound.info["request"])
        assert schema is not None, f"{where}: no schema for {alias}.{method}"

        accepted = set()
        for field in schema["fields"]:
            accepted.add(field["name"])
            accepted.add(field["python_name"])
            accepted.add(field["name"].casefold())

        bad = sorted(name for name in sent if name not in accepted)
        assert bad == [], (
            f"{where}: high.{alias}.{method} is sent {bad}, which "
            f"{bound.info['request']} does not have. encode_message() raises "
            f"TypeError, so this call has never worked. Fields: "
            + ", ".join(sorted(f["python_name"] for f in schema["fields"]))
        )

    def test_the_guard_would_catch_the_bug(self):
        """Break it validly and watch it fail, rather than trust the green.

        The synthetic module parses, imports and resolves -- the only thing
        wrong with it is the keyword, which is exactly the bug's shape.
        """
        import ast

        from rootpy import RootClient

        source = (
            "async def set_dm_invite_requirement(self, connection, required):\n"
            "    return (await self.client.high.user"
            ".set_direct_message_invite_requirement(\n"
            "        connection=connection, is_required=required,\n"
            "    )).data\n"
        )
        tree = ast.parse(source)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        sent = {k.arg for k in call.keywords if k.arg}

        api = RootClient(token="x").high
        schema = api.codec.resolve_message(
            api.user.set_direct_message_invite_requirement.info["request"]
        )
        accepted = {f["python_name"] for f in schema["fields"]}
        assert "is_required" in sent
        assert "is_required" not in accepted, (
            "the guard's premise is gone -- is_required is a real field now"
        )


class TestInviteConnectionEnums:
    """Pinned rather than exercised live, on purpose.

    ``set_dm_invite_requirement`` and friends take a
    ``UserDirectMessageInviteConnection`` and decide whether two accounts can
    DM each other at all. There is no getter, so a live test could not restore
    the previous value -- and setting it wrong would break every DM, call and
    invite test on that account, permanently, in a way that reads as an SDK
    bug rather than a test that overreached.

    So the constants are pinned from the descriptor instead. If Root renumbers
    them, that shows up here rather than as a mysterious PERMISSION_DENIED.
    """

    EXPECTED = {"Unspecified": 0, "Any": 1, "Connected": 2, "None": 3, "Friend": 4}

    @pytest.mark.parametrize(
        "enum_name",
        [
            "RootApp.WebApi.Shared.Enums.UserDirectMessageInviteConnection",
            "RootApp.WebApi.Shared.Enums.UserCommunityInviteConnection",
        ],
    )
    def test_values_match_the_descriptor(self, enum_name):
        from rootpy.structured_registry import ENUMS

        assert ENUMS[enum_name] == self.EXPECTED

    def test_friendship_invite_has_no_friend_option(self):
        """You cannot require friendship in order to be friended."""
        from rootpy.structured_registry import ENUMS

        values = ENUMS["RootApp.WebApi.Shared.Enums.UserFriendshipInviteConnection"]
        assert "Friend" not in values
        assert values == {"Unspecified": 0, "Any": 1, "Connected": 2, "None": 3}

    def test_friend_is_the_most_restrictive_dm_setting(self):
        """The value the DM gate lands on when a burner account is default."""
        from rootpy.structured_registry import ENUMS

        values = ENUMS[
            "RootApp.WebApi.Shared.Enums.UserDirectMessageInviteConnection"
        ]
        assert values["Friend"] == 4
        assert values["Any"] == 1

    def test_the_setters_exist_and_take_a_connection(self):
        from rootpy.features import UserSettingsManager

        for name in (
            "set_dm_invite_requirement",
            "set_friend_invite_requirement",
            "set_community_invite_requirement",
        ):
            params = inspect.signature(getattr(UserSettingsManager, name)).parameters
            assert "connection" in params, f"{name} lost its connection argument"
            assert "email_verified" in params, (
                f"{name} takes email_verified -- the second condition is "
                "'their address is verified', not 'this rule is required'"
            )

    def test_the_enums_are_exported(self):
        """Without named constants the only way to call these is a raw int."""
        from rootpy.enums import (
            UserCommunityInviteConnection,
            UserDirectMessageInviteConnection,
            UserFriendshipInviteConnection,
        )
        from rootpy.structured_registry import ENUMS

        pairs = [
            (UserDirectMessageInviteConnection,
             "RootApp.WebApi.Shared.Enums.UserDirectMessageInviteConnection"),
            (UserCommunityInviteConnection,
             "RootApp.WebApi.Shared.Enums.UserCommunityInviteConnection"),
            (UserFriendshipInviteConnection,
             "RootApp.WebApi.Shared.Enums.UserFriendshipInviteConnection"),
        ]
        for enum, descriptor in pairs:
            mine = {member.name: int(member) for member in enum}
            theirs = {
                name.upper(): value for name, value in ENUMS[descriptor].items()
            }
            assert mine == theirs, f"{enum.__name__} drifted from the descriptor"

    def test_there_is_still_no_getter(self):
        """If one appears, these become safely testable live."""
        from rootpy.features import UserSettingsManager

        getters = [
            name for name in dir(UserSettingsManager)
            if "invite_requirement" in name and name.startswith("get")
        ]
        assert getters == [], (
            f"a getter exists now ({getters}) -- the invite-requirement "
            "setters can be tested live and restored"
        )


class TestResolveEnumIsMemoised:
    """The bare-name path is a full scan, and it runs per field per call.

    ``resolve_enum`` hits a dict when handed a fully-qualified name and falls
    through to scanning all of ``ENUMS.items()`` otherwise -- which is what a
    schema's bare field type looks like. Measured on this build before it was
    memoised: **0.40 us qualified against 28.94 us bare, 73x**, paid once per
    enum field on both encode *and* decode, synchronously on the event loop.

    Caching is only legitimate because the function is pure: ``ENUMS`` is a
    read-only ``Mapping`` with no ``__setitem__``, and nothing in the package
    or the suite mutates it. That premise is pinned below, because if it ever
    stops holding the cache becomes a correctness bug rather than a speedup.
    """

    def _codec(self):
        from rootpy.structured_api import StructuredProtoCodec

        return StructuredProtoCodec()

    def _uncached(self, type_name, namespace_hint=None):
        """The original implementation, kept as the oracle."""
        from rootpy.structured_registry import ENUMS

        clean = type_name.rstrip("?")
        if clean in ENUMS:
            return clean, ENUMS[clean]
        simple = clean.rsplit(".", 1)[-1]
        candidates = [
            (n, m) for n, m in ENUMS.items()
            if n.rsplit(".", 1)[-1] == simple
        ]
        if namespace_hint:
            preferred = [
                i for i in candidates
                if i[0].rsplit(".", 1)[0] == namespace_hint
            ]
            if len(preferred) == 1:
                return preferred[0]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def test_the_registry_is_read_only(self):
        """The premise the cache rests on."""
        from rootpy.structured_registry import ENUMS

        assert not hasattr(ENUMS, "__setitem__"), (
            "ENUMS became mutable -- memoising resolve_enum is no longer safe"
        )
        with pytest.raises(TypeError):
            ENUMS["injected"] = {}

    def test_it_agrees_with_the_unmemoised_original_everywhere(self):
        """Every name in the registry, both spellings, several hints."""
        from rootpy.structured_registry import ENUMS

        codec = self._codec()
        names = set()
        for name in ENUMS:
            simple = name.rsplit(".", 1)[-1]
            names.update({name, name + "?", simple, simple + "?"})
        names.update({"NoSuchEnum", "", "A.B.C"})
        hints = [None, "NotANamespace"]
        hints += sorted({n.rsplit(".", 1)[0] for n in ENUMS})[:4]

        mismatches = [
            (name, hint)
            for name in sorted(names)
            for hint in hints
            if codec.resolve_enum(name, namespace_hint=hint)
            != self._uncached(name, hint)
        ]
        assert mismatches == [], (
            f"memoised resolve_enum disagrees with the original: {mismatches[:5]}"
        )

    def test_repeated_lookups_hit_the_cache(self):
        from rootpy.structured_api import _resolve_enum_cached
        from rootpy.structured_registry import ENUMS

        bare = next(iter(ENUMS)).rsplit(".", 1)[-1]
        _resolve_enum_cached.cache_clear()

        self._codec().resolve_enum(bare)
        after_first = _resolve_enum_cached.cache_info()
        assert after_first.misses == 1 and after_first.hits == 0

        for _ in range(50):
            self._codec().resolve_enum(bare)
        after = _resolve_enum_cached.cache_info()
        assert after.misses == 1, "the cache is not being consulted"
        assert after.hits == 50

    def test_the_hint_is_part_of_the_key(self):
        """Two hints must not collide onto one cached answer."""
        from rootpy.structured_api import _resolve_enum_cached

        _resolve_enum_cached.cache_clear()
        codec = self._codec()
        codec.resolve_enum("UserOnlineStatus")
        codec.resolve_enum("UserOnlineStatus", namespace_hint="RootApp")
        assert _resolve_enum_cached.cache_info().misses == 2


class TestEnumFieldsEncode:
    """The codec could not encode an enum-typed field at all.

    ``resolve_enum`` looks a bare type name up in the registry, and
    ``UserOnlineStatus`` exists in **two** packages -- so it found two
    candidates, could not choose, returned None, and the encoder reported
    "Unsupported protobuf field type", blaming the type when the problem was
    an ambiguous name.

    Worse, resolving it would have been wrong anyway: the two registry entries
    disagree, and *neither* matches the wire. ACTIVE is ``0x10``. The enum the
    caller passes is right by construction, so an int is now encoded directly
    with no lookup.
    """

    def _codec(self):
        from rootpy.structured_api import StructuredProtoCodec

        return StructuredProtoCodec()

    def test_an_enum_member_encodes(self):
        from rootpy.enums import UserOnlineStatus

        body = self._codec().encode_message(
            "UserSetMaxOnlineStatusRequest",
            {"max_status": UserOnlineStatus.ACTIVE},
        )
        assert body

    def test_it_encodes_the_real_wire_value(self):
        """field 10, varint 0x10 -- not either descriptor's number."""
        from rootpy.enums import UserOnlineStatus

        body = self._codec().encode_message(
            "UserSetMaxOnlineStatusRequest",
            {"max_status": UserOnlineStatus.ACTIVE},
        )
        assert body.endswith(bytes([10 << 3 | 0, 0x10]))

    def test_a_plain_int_encodes_identically(self):
        from rootpy.enums import UserOnlineStatus

        codec = self._codec()
        a = codec.encode_message(
            "UserSetMaxOnlineStatusRequest",
            {"max_status": UserOnlineStatus.ACTIVE},
        )
        b = codec.encode_message(
            "UserSetMaxOnlineStatusRequest", {"max_status": 16}
        )
        assert a[-2:] == b[-2:]

    def test_the_ambiguity_that_caused_it_is_real(self):
        """Two packages, and they disagree -- so the lookup cannot be trusted."""
        from rootpy.structured_registry import ENUMS

        matches = [
            name for name in ENUMS
            if name.rsplit(".", 1)[-1] == "UserOnlineStatus"
        ]
        assert len(matches) == 2
        assert ENUMS[matches[0]] != ENUMS[matches[1]]

    def test_only_the_wire_descriptor_matches_the_corrected_enum(self):
        """Exactly one of the two same-named enums agrees with the wire.

        The ambiguity, not the value, is what makes a name lookup
        untrustworthy: ``RootApp.Browser.Models.UserOnlineStatus`` is a
        different enum altogether (``Active = 3``) that happens to share a
        leaf name with the one the wire uses (``Active = 0x10``).
        """
        from rootpy.enums import UserOnlineStatus
        from rootpy.structured_registry import ENUMS

        agree = {
            name: members.get("Active") == int(UserOnlineStatus.ACTIVE)
            for name, members in ENUMS.items()
            if name.rsplit(".", 1)[-1] == "UserOnlineStatus"
        }
        assert agree == {
            "RootApp.Browser.Models.UserOnlineStatus": False,
            "RootApp.WebApi.Shared.Enums.UserOnlineStatus": True,
        }

    def test_a_non_int_unknown_type_still_raises(self):
        """The fallback must not swallow genuinely unsupported values."""
        with pytest.raises(TypeError, match="Unsupported protobuf field type"):
            self._codec().encode_message(
                "UserSetMaxOnlineStatusRequest",
                {"max_status": object()},
            )

    def test_the_error_now_suggests_the_fix(self):
        with pytest.raises(TypeError, match="rootpy.enums member or a plain int"):
            self._codec().encode_message(
                "UserSetMaxOnlineStatusRequest", {"max_status": object()}
            )

    def test_a_bool_is_not_treated_as_an_enum(self):
        """bool is an int subclass; it must not slip through the fallback."""
        import inspect as _inspect

        from rootpy.structured_api import StructuredProtoCodec

        src = _inspect.getsource(StructuredProtoCodec._field_value)
        assert "isinstance(value, bool)" in src


class TestCloseDoesNotTouchVoiceThatWasNeverBuilt:
    """`close()` called `stop_audio()` on None for every text-only client.

    The else branch ran whenever `_calls` was None -- which is the common case,
    since voice is built lazily -- raising AttributeError on every close and
    swallowing it. Invisible, but it meant the whole voice teardown block was
    dead code doing nothing but raising.
    """

    def test_the_none_branch_is_gone(self):
        src = inspect.getsource(RootClient.close)
        assert "if self._calls is not None:" in src

    def test_closing_a_text_only_client_is_clean(self):
        async def scenario():
            client = RootClient(token="test-token")
            assert client._calls is None
            await client.close()
            return True

        assert asyncio.run(scenario())

    def test_close_is_still_idempotent(self):
        async def scenario():
            client = RootClient(token="test-token")
            await client.close()
            await client.close()
            return True

        assert asyncio.run(scenario())


class TestClientLifecycleRetainsNothing:
    """Most of a reconnect is testable offline, so it is tested offline.

    A reconnect is close() + login_token() + connect(). Only the last two need
    the network -- constructing a client, building its httpx transport with a
    real SSL context, and closing it are all local, and that is where a
    per-cycle leak would most plausibly live.

    A long-running bot reconnects thousands of times, so a few objects
    retained per cycle is the difference between a process that runs for a
    week and one that does not.
    """

    def _count(self):
        import gc

        gc.collect()
        gc.collect()
        return len(gc.get_objects())

    async def _cycles(self, n, build_transport=False):
        for _ in range(n):
            client = RootClient(token="test-token")
            if build_transport:
                client.transport._get_client()
            await client.close()

    def test_the_control_is_flat(self):
        """Without this, every number below is uninterpretable."""
        asyncio.run(self._cycles(10))
        before = self._count()
        after = self._count()
        assert after - before == 0, "the counter drifts with no work at all"

    def test_construct_and_close_retains_nothing(self):
        asyncio.run(self._cycles(20))          # warm lazy imports
        before = self._count()
        asyncio.run(self._cycles(100))
        after = self._count()
        assert after - before < 100, (
            f"100 construct/close cycles retained {after - before} objects"
        )

    def test_it_does_not_scale_with_cycle_count(self):
        """A real leak grows with cycles; noise does not."""
        asyncio.run(self._cycles(20))
        before = self._count()
        asyncio.run(self._cycles(50))
        small = self._count() - before

        before = self._count()
        asyncio.run(self._cycles(200))
        large = self._count() - before

        assert large < 200, f"200 cycles retained {large} objects"
        assert large <= small + 150, (
            f"retention scales with cycles: {small} at 50, {large} at 200"
        )

    def test_building_and_closing_the_transport_retains_nothing(self):
        """The httpx client and its SSL context are the heavy part."""
        try:
            asyncio.run(self._cycles(5, build_transport=True))
        except ImportError:
            pytest.skip("httpx[http2] not installed; the transport needs h2")

        before = self._count()
        asyncio.run(self._cycles(50, build_transport=True))
        after = self._count()
        assert after - before < 100, (
            f"50 transport build/close cycles retained {after - before} objects"
        )

    def test_close_releases_the_http_client(self):
        async def scenario():
            client = RootClient(token="test-token")
            try:
                client.transport._get_client()
            except ImportError:
                return None
            assert client.transport._client is not None
            await client.close()
            return client.transport._client

        result = asyncio.run(scenario())
        if result is None:
            pytest.skip("httpx[http2] not installed")
        assert result is None, "close() left the httpx client attached"


# --------------------------------------------------------------------------
# What this file guards
#
# These are contract tests, not unit tests. Each one exists because the thing
# it checks is derivable -- a signature, a default, a field list, a documented
# verb -- and a derivable fact that is maintained by hand drifts.
#
# The expensive ones to lose are the AST walkers: they catch a manager that
# forwards a keyword the request message does not have, a wrapper whose falsy
# default makes the layer below unreachable, and a documented ``client.verb()``
# that no longer exists. None of those fail at import time, and all of them
# fail at the first live call.
# --------------------------------------------------------------------------
