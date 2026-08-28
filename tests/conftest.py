"""Shared fixtures, including the self-bootstrapping live sandbox.

Live tests need exactly one thing: ``ROOT_TOKEN``. They do not take channel,
role or user IDs, because they create everything they touch. The client
account owns the community it makes, so it always has full permissions, and
the community is deleted again on the way out.

    export ROOT_TOKEN="..."
    pytest -m live

Without the variable every live test is skipped, so the offline suite still
runs clean in CI.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid

import pytest
import pytest_asyncio

# Per-test timing in milliseconds, correlated with the SDK's transport
# counters. Off unless --timing is passed.
pytest_plugins = ["tests.timing_plugin"]

import rootpy
from rootpy import RootClient
from rootpy.responses import field as _field, items as _items

#: Everything this suite creates is named with this prefix, so anything left
#: behind by a crashed run is obvious and easy to find.
#:
#: Note the shape: words and spaces, no hyphens, short. The first live run
#: sent "rootpy-selftest-community-<8 hex>" (34 chars) and Root rejected
#: CommunityCreate with INVALID_ARGUMENT. The known-good name from
#: example.py is "rootpy demo" -- 11 chars, lowercase, one space.
SANDBOX_PREFIX = "rootpy test"


def _token() -> str | None:
    return os.environ.get("ROOT_TOKEN")


def _token2() -> str | None:
    """Second account, for anything that needs a counterparty.

    DMs, calls, friend requests, blocks and every pushed event need someone on
    the other end. One account can create a community and talk to itself, but
    it cannot prove that a message *arrives*.
    """
    return os.environ.get("ROOT_TOKEN2")


requires_live = pytest.mark.skipif(
    _token() is None,
    reason="set ROOT_TOKEN to run live tests (they create and delete their own community)",
)

#: Opt-in for the two tests that change a profile image.
#:
#: These are the only tests in the suite that mutate durable account state
#: they cannot reliably put back. The set works; the *restore* shares Root's
#: profile-write quota, and that quota outlasts a 95-second backoff -- twice,
#: on two separate runs, leaving a real account wearing a test image with the
#: original already discarded.
#:
#: The same reasoning that keeps the invite-requirement setters untested
#: applies here: unrestorable state is not worth two methods of coverage by
#: default. The tests are correct and stay runnable, they just need asking
#: for, and they save the previous image to disk before touching anything.
#:
#:     set ROOTPY_TEST_PROFILE_IMAGES=1
#:     pytest -m live -k ProfileImages -v
requires_profile_image_optin = pytest.mark.skipif(
    os.environ.get("ROOTPY_TEST_PROFILE_IMAGES", "").strip() not in {"1", "true", "yes"},
    reason=(
        "changes the account's avatar/banner and cannot reliably restore it "
        "(Root's profile-write quota outlasts the retry window). "
        "Set ROOTPY_TEST_PROFILE_IMAGES=1 to run these."
    ),
)

requires_two_accounts = pytest.mark.skipif(
    _token() is None or _token2() is None,
    reason=(
        "set both ROOT_TOKEN and ROOT_TOKEN2 to run two-account tests "
        "(sending and receiving needs a counterparty)"
    ),
)


async def eventually(
    check,
    *,
    timeout: float = 20.0,
    interval: float = 0.4,
    describe: str = "condition",
):
    """Poll ``check`` until it returns something truthy, or give up.

    Root is eventually consistent for reads-after-writes, which the suite used
    to handle with a fixed ``asyncio.sleep(2)`` before asserting. That is both
    slow and fragile: it pays the full two seconds even when the value landed
    in 200 ms, and it still fails if the server happens to take longer.

    ``--timing`` put 33% of a live run in "sleeps and client work", most of it
    here, so polling is worth the few lines. ``check`` may be sync or async;
    the truthy value it returns is passed back.
    """
    import inspect as _inspect

    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = check()
        if _inspect.isawaitable(last):
            last = await last
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{describe} did not become true within {timeout:.0f}s "
                f"(last value: {last!r})"
            )
        await asyncio.sleep(interval)


async def never(
    check,
    *,
    window: float = 8.0,
    interval: float = 0.25,
    describe: str = "condition",
):
    """Assert ``check`` stays false for a whole window.

    The mirror of :func:`eventually`, and the only honest way to write "this
    does not arrive". A negative needs real elapsed time -- there is nothing to
    poll *for* -- so the wait is unavoidable; what this adds is failing on the
    first sign of life rather than at the end, and saying what was being
    watched when it does.

    It lives here rather than in a suite so the "no fixed multi-second sleeps"
    guard stays meaningful: a bare ``asyncio.sleep(8)`` in a test is almost
    always an ``eventually`` waiting to happen, and this is the exception,
    named.
    """
    import inspect as _inspect

    deadline = time.monotonic() + window
    while True:
        last = check()
        if _inspect.isawaitable(last):
            last = await last
        if last:
            raise AssertionError(
                f"{describe} happened, and should not have "
                f"(value: {last!r})"
            )
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(interval)


def word_tag() -> str:
    """A short random suffix of letters only, for names that reject digits.

    Channel *group* names are the strictest thing found so far: ``test area``
    is accepted, ``rootpy-ag-1a2b`` was rejected (hyphens), and then
    ``rootpy group 7269`` was rejected too -- so it is not hyphens, it is
    digits. ``tag()`` is hex and therefore unusable here.

    Four letters from a 26-letter alphabet is ~457k combinations, which is
    plenty for a suite that creates a handful of groups per run.
    """
    import random
    import string

    return "".join(random.choice(string.ascii_lowercase) for _ in range(4))


#: Candidate channel-group names, narrowest first.
#:
#: **The rule is settled now** -- laddered against a throwaway community,
#: 40 candidates varying length, word count and character set independently:
#:
#:     ASCII letters, digits and apostrophes, in AT MOST TWO whitespace-
#:     separated words, with no leading or trailing whitespace.
#:
#: Length is not a constraint (65 chars accepted at one word, 40 at two).
#: Refused: three or more words, hyphen, underscore, period, leading or
#: trailing space, accented letters, emoji, empty. Accepted: digits, mixed
#: case, apostrophe, and multiple interior spaces (still two words).
#: Root answers `Name: 'Name' is not in the correct format.
#: [RegularExpressionValidator]` for a format failure and
#: `[NotEmptyValidator]` for empty.
#:
#: That explains all three earlier wrong guesses at once: "rootpy-ag-1a2b"
#: (hyphens), "rootpy group 7269" and "rootpy group bbcy" (three words, not
#: the character set as suspected), against "test area" which is two words of
#: letters. The ladder stays because it costs nothing and reports what worked,
#: but every rung below is now known-good rather than a guess.
GROUP_NAME_CANDIDATES = [
    lambda: f"grp {word_tag()}",        # 8 chars, 2 words -- closest to "test area"
    lambda: word_tag(),                 # 4 chars, 1 word
    lambda: f"{word_tag()} {word_tag()}",  # 9 chars, 2 words -- "test area" shape
    lambda: f"rootpy {word_tag()}",     # 11 chars, 2 words
    lambda: "test area",                # the known-good literal
]


async def create_channel_group(manager, community_id, *, access_rules=None):
    """Create a group, reporting which name shape Root accepted.

    Same approach that settled PictureHex in one run: walk candidates and say
    which rung worked, rather than guess again.

    ``access_rules`` is passed straight through when given, so a caller can
    exercise the rules-at-creation path without reimplementing the name
    ladder. Only ``CommunityManager`` accepts it; ``CommunityAdminService``
    takes the same keyword, so both work.
    """
    from rootpy.exceptions import GrpcInvalidArgument

    extra = {} if access_rules is None else {"access_rules": access_rules}

    attempts = []
    for index, build in enumerate(GROUP_NAME_CANDIDATES, start=1):
        name = build()
        try:
            group = await manager.create_channel_group(
                community_id, name, **extra
            )
        except GrpcInvalidArgument as exc:
            attempts.append(
                f"  {index}. {name!r} ({len(name)} chars, "
                f"{len(name.split())} words) -> rejected"
            )
            last = exc
            continue
        if index > 1:
            print(
                "\n   channel-group name rule: rejected\n"
                + "\n".join(attempts)
                + f"\n   accepted: {name!r} ({len(name)} chars, "
                f"{len(name.split())} words). Narrow "
                "GROUP_NAME_CANDIDATES to this shape.\n"
            )
        return group

    raise AssertionError(
        "Root rejected every channel-group name shape:\n" + "\n".join(attempts)
        + f"\nLast error: {last}"
    )


#: A 1x1 transparent PNG. Emoji, file, avatar and banner uploads all want real
#: image bytes, and generating them beats shipping a binary fixture nobody can
#: review. Lives here rather than in one live module because four of them need
#: it now.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def make_png(rgb=None) -> bytes:
    """A valid 1x1 PNG of a given (or random) colour.

    **Root content-addresses assets**: uploading identical bytes yields the
    identical asset id and therefore the identical ``root://`` URI. Observed
    directly -- a community file and a profile picture uploaded from the same
    source file came back with the same asset id.

    That makes a fixed test image unusable for any "did it change?" assertion.
    The profile-picture test set ``PNG_1X1``, and on the next run the account
    was *already* wearing ``PNG_1X1``, so the URI did not move and the check
    could never pass. The test looked broken; the constant was.

    Hand-rolled rather than pulled from Pillow: the suite has no image
    dependency and should not grow one to make a 70-byte file.
    """
    import random
    import struct
    import zlib

    if rgb is None:
        rgb = (random.randrange(256), random.randrange(256), random.randrange(256))

    def chunk(tag_bytes: bytes, data: bytes) -> bytes:
        body = tag_bytes + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    # 1x1, 8 bits per channel, colour type 2 (truecolour), no interlace.
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    # One scanline: a filter byte, then the pixel.
    pixels = zlib.compress(b"\x00" + bytes(rgb))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def png(tmp_path) -> str:
    """A throwaway PNG on disk; returns its path."""
    path = tmp_path / f"probe-{tag()}.png"
    path.write_bytes(PNG_1X1)
    return str(path)


@pytest.fixture
def unique_png(tmp_path) -> str:
    """A PNG whose bytes have never been uploaded before.

    Use this wherever the test asserts that an asset *changed*. See
    :func:`make_png` for why a constant cannot work there.
    """
    path = tmp_path / f"unique-{tag()}.png"
    path.write_bytes(make_png())
    return str(path)


async def ladder(label, candidates, *, required: bool = True):
    """Try each candidate call and report which shape the server accepted.

    The generalisation of ``create_channel_group`` below, which settled the
    channel-group name rule in one run after three guesses had failed. The
    same pattern settled ``picture_hex``. Guessing a server-side rule has cost
    four runs; measuring it has never cost more than one.

    ``candidates`` is a sequence of ``(description, coro_factory)``. Every
    rung is tried in order. On success the accepted description is printed
    (along with everything that was rejected, and why) so the next edit can
    narrow the list to the one that works. On total failure the report is the
    assertion message, so one run tells you every rejection at once instead of
    one per run.

    ``required=False`` returns ``(None, report)`` instead of failing, for a
    probe where "the server refuses all of these" is itself the finding.
    """
    attempts = []
    for index, (description, factory) in enumerate(candidates, start=1):
        try:
            result = await factory()
        except Exception as exc:
            detail = _root_detail(exc) or f" {type(exc).__name__}: {exc}"
            attempts.append(f"  {index}. {description} -> rejected{detail}")
            continue

        report = (
            f"\n   {label}: accepted {description!r}"
            + (
                "\n   rejected first:\n" + "\n".join(attempts)
                if attempts
                else " (first candidate -- the list is already narrow)"
            )
            + "\n"
        )
        if attempts:
            print(report)
        return result, report

    report = (
        f"{label}: the server refused every candidate:\n" + "\n".join(attempts)
    )
    if required:
        raise AssertionError(report)
    print("\n   " + report + "\n")
    return None, report


def _ids_match(left, right) -> bool:
    """Compare Root ids without caring how they were encoded."""
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        from rootpy.identifiers import normalize_root_guid

        return normalize_root_guid(left) == normalize_root_guid(right)
    except Exception:
        return False


def tag() -> str:
    """A short random suffix, so repeat runs never collide."""
    return uuid.uuid4().hex[:4]


def unique(label: str) -> str:
    """A short, collision-proof name for something the sandbox creates.

    Kept deliberately close to the shape example.py is known to succeed with:
    lowercase words separated by spaces, no punctuation, well under 32 chars.
    """
    return f"{SANDBOX_PREFIX} {label} {tag()}"


#: The first live run failed here and the ladder proved the name was never
#: the problem: Root rejected CommunityCreate because PictureHex was empty.
#: A single name is enough now; _root_detail still surfaces any future
#: field-level validation error directly in the failure message.
#: Root validates PictureHex with an ExactLengthValidator of 7, so the
#: leading hash is required -- "#rrggbb", not "rrggbb".
SANDBOX_PICTURE_HEX = "#3f51b5"

#: Root validates role names with a RegularExpressionValidator and rejected
#: "tester 3a8f". devscripts/fullrun.py -- which runs against an existing
#: community and so never hit the create_community bug -- uses
#: "rootpy-role-<tag>": lowercase, hyphens, no spaces. Matching that shape.
#: Community names are looser (spaces are fine there); channels and roles
#: are not.
SANDBOX_ROLE_COLOR = "#3498db"


def _root_detail(exc) -> str:
    """Pull Root's structured error detail out of a gRPC exception.

    ``GrpcWebError`` already decodes the ``RootGrpcException`` header into
    ``exc.root_exception``, and that payload can carry per-field validation
    errors (``property_name`` / ``error_message``). Root did not populate it
    for the first CommunityCreate rejection, but if it ever does, this puts
    the offending field name straight in the test output instead of leaving
    a generic "one or more request values were rejected".
    """
    info = getattr(exc, "root_exception", None)
    if info is None:
        return ""
    bits = []
    summary = info.summary()
    if summary:
        bits.append(summary)
    for err in info.validation_errors:
        field_name = getattr(err, "property_name", None)
        reason = getattr(err, "error_message", None)
        bits.append(f"{field_name}: {reason}")
    return ("\n       " + "\n       ".join(bits)) if bits else ""


async def create_sandbox_community(manager):
    """Create the throwaway community, surfacing Root's field-level detail."""
    from rootpy.exceptions import GrpcWebError

    name = f"{SANDBOX_PREFIX} {tag()}"
    try:
        # No picture_hex: the SDK supplies a valid default. Passing one here
        # would hide a regression in that default.
        return await manager.create(name)
    except GrpcWebError as exc:
        raise AssertionError(
            f"Root rejected CommunityCreate for {name!r}:{_root_detail(exc)}"
            f"\n  {exc}"
        ) from exc


@pytest_asyncio.fixture(scope="session")
async def client():
    """A logged-in client for the whole live session."""
    token = _token()
    if token is None:
        pytest.skip("ROOT_TOKEN not set")
    c = RootClient(token=token)
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture(scope="session")
async def me(client):
    """The account behind ROOT_TOKEN."""
    return await client.whoami()


@pytest_asyncio.fixture(scope="session")
async def sandbox(client):
    """A throwaway community owned by the test account.

    Every setup step is attempted even when an earlier one fails, and all
    failures are reported together. That matters: with a fail-fast fixture,
    each broken call costs a separate live run to discover, and Root's
    generic INVALID_ARGUMENT gives nothing to work from until the request
    actually goes out. One run should surface every problem at once.

    The community itself is the exception -- nothing can proceed without it,
    so that failure aborts immediately.
    """
    manager = client.community
    community = await create_sandbox_community(manager)

    class Sandbox:
        pass

    box = Sandbox()
    box.community = community
    box.community_id = community.id
    box.group = None
    box.channel = None
    box.role = None
    box.failures = []

    async def step(label, coro_factory, *, needs=()):
        """Run one setup step, recording rather than raising on failure."""
        for required in needs:
            if getattr(box, required, None) is None:
                box.failures.append(f"  {label}: skipped ({required} missing)")
                return None
        try:
            return await coro_factory()
        except Exception as exc:
            box.failures.append(f"  {label}: {exc}")
            return None

    try:
        box.group = await step(
            "create_channel_group",
            lambda: create_channel_group(manager, community.id),
        )
        box.channel = await step(
            "create_text_channel",
            lambda: manager.create_text_channel(
                community.id, box.group.id, f"rootpy-text-{tag()}"
            ),
            needs=("group",),
        )
        # Deliberately minimal: name shape and mentionable only, matching the
        # call fullrun.py is known to succeed with. Anything less certain --
        # colours, permission sets -- belongs in an individual test, where a
        # rejection isolates instead of taking the whole sandbox down.
        box.role = await step(
            "create_role",
            lambda: manager.create_role(
                community.id, f"rootpy-role-{tag()}", mentionable=True
            ),
        )

        if box.failures:
            raise AssertionError(
                "Sandbox setup did not complete. Every step was attempted so "
                "this lists all of them at once:\n" + "\n".join(box.failures)
            )

        yield box
    finally:
        try:
            await manager.delete(community.id)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            print(
                f"\n!! sandbox cleanup failed for {community.id}: {exc}\n"
                f"   delete communities named {SANDBOX_PREFIX}* by hand."
            )


@pytest_asyncio.fixture
async def message(client, sandbox):
    """A freshly posted message, deleted afterwards."""
    result = await client.message(
        sandbox.channel.id, f"self-test {time.time():.0f}"
    )
    sent = getattr(result, "message", result)
    try:
        yield sent
    finally:
        try:
            await client.delete_message(sent)
        except Exception:
            pass


@pytest_asyncio.fixture(scope="session")
async def gateway_client(sandbox):
    """A second client with the websocket gateway actually connected.

    The main ``client`` fixture is token-only, which is enough for every RPC
    but deliberately not for the gateway -- that needs the hub URL and device
    id, which only a login produces. So this one does ``login_token()`` then
    ``connect()``, and is kept separate so a gateway problem cannot take the
    RPC tests down with it.

    Depends on ``sandbox`` so the community exists and teardown ordering puts
    the socket down before the community is deleted.
    """
    token = _token()
    if token is None:
        pytest.skip("ROOT_TOKEN not set")

    connected = RootClient(token=token)
    try:
        await connected.login_token(token)
        await connected.connect()
        yield connected
    finally:
        await connected.close()


# --------------------------------------------------------------------------
# second account -- the counterparty for send/receive tests
# --------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session")
async def peer(gateway_client):
    """The second account, with its gateway connected so it can receive.

    Everything interesting about DMs, calls, friend requests and pushed events
    needs someone on the other end: one account can send, but only a second one
    proves the message arrived. Connected rather than token-only because
    receiving is the whole point.

    Depends on ``gateway_client`` so the first account is up first and teardown
    unwinds in the right order.
    """
    token = _token2()
    if token is None:
        pytest.skip("ROOT_TOKEN2 not set")

    client = RootClient(token=token)
    try:
        await client.login_token(token)
        await client.connect()
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture(scope="session")
async def peer_identity(peer):
    """Who the second account is."""
    return await peer.whoami()


@pytest_asyncio.fixture(scope="session")
async def distinct_accounts(me, peer_identity):
    """Guard: the two tokens must be different accounts.

    Pointing ROOT_TOKEN2 at the same account would make every send/receive
    test pass trivially and prove nothing, so fail loudly instead.
    """
    if me.id == peer_identity.id:
        pytest.fail(
            "ROOT_TOKEN and ROOT_TOKEN2 are the same account "
            f"({me.username}). Two-account tests need two accounts."
        )
    return me, peer_identity


@pytest_asyncio.fixture
async def peer_inbox(peer):
    """Collect everything the second account receives, for later assertion.

    Handlers are fire-and-forget, so anything reading this should
    ``await peer.drain_events()`` first -- or use ``peer.wait_for``.
    """
    received: dict[str, list] = {}

    async def record(event, name):
        received.setdefault(name, []).append(event)

    handlers = {}
    for name in ("message", "notification", "packet_direct_message_ring",
                 "packet_direct_message_ring_declined", "typing_packet"):
        async def handler(event, _name=name):
            await record(event, _name)
        handlers[name] = handler
        peer.add_listener(name, handler)

    try:
        yield received
    finally:
        for name, handler in handlers.items():
            peer.remove_listener(name, handler)


@pytest_asyncio.fixture(scope="session")
async def joined_peer(client, peer, sandbox, peer_identity):
    """Put the second account inside the sandbox community.

    Membership is what makes member events, mentions and moderation testable
    at all. Done through a real invite rather than a back door, so the invite
    path gets exercised too.

    Leaves on the way out, though deleting the community would remove the
    membership anyway -- the explicit leave is so a failure here is visible
    rather than masked by teardown.
    """
    invite = await client.invites.create(sandbox.community_id, max_uses=5)
    code = getattr(invite, "code", None) or getattr(invite, "id", None)
    if not code:
        pytest.skip(f"could not read an invite code from {invite!r}")

    try:
        await peer.invites.join(code)
    except Exception as exc:
        # Already a member is fine; anything else is worth failing on.
        if "already" not in str(exc).lower():
            raise

    try:
        yield code
    finally:
        try:
            await peer.community_service.leave(sandbox.community_id)
        except Exception:
            pass


@pytest_asyncio.fixture(scope="session")
async def friendship(client, peer, me, peer_identity):
    """Make the two accounts friends before anything that needs it.

    Root's default privacy setting only accepts DMs from friends, so DMs and
    calls (which open a DM first) fail with PERMISSION_DENIED on a fresh pair
    of accounts. The SDK already diagnoses that precisely -- see
    ``DMMemberService._diagnose_dm_gate`` -- so this is a missing prerequisite
    rather than a bug to work around.

    Establishing it here means the request/accept round trip is exercised on
    every run instead of being a test nobody depends on: the request is sent
    from one account and accepted from the other, both over the wire.

    Idempotent, because burner accounts stay friends between runs.
    """
    already = await client.friends.friend_ids()
    if any(_ids_match(peer_identity.id, existing) for existing in already):
        yield True
        return

    from rootpy.exceptions import RootError

    try:
        await client.add_friend(peer_identity.username)
    except RootError as exc:
        # A request already sitting in the peer's inbox is fine -- accept it
        # below. Anything else is a real failure.
        if "exist" not in str(exc).lower():
            raise

    accepted = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if await peer.friend_requests.accept_from(me.id):
            accepted = True
            break
        if await peer.friend_requests.accept_from(me.username):
            accepted = True
            break
        # Last resort: these are burner accounts with a single request in
        # flight, so anything pending is the one just sent. Keeps a
        # sender-matching bug from blocking every downstream test.
        if await peer.friend_requests.accept_all():
            accepted = True
            break
        await asyncio.sleep(2)

    if not accepted:
        # Report what the peer's inbox actually held. pending() silently
        # returned nothing once already (it matched on a type *name* while the
        # wire carries an int), so "no request arrived" and "the request
        # arrived and we failed to recognise it" must be distinguishable
        # without another run.
        raw = await peer.notifications.list()
        items = _items(raw, "notifications")
        from rootpy.enums import NotificationType

        lines = []
        for item in items[:10]:
            kind = NotificationType.coerce(_field(item, "notification_type"))
            name = getattr(kind, "name", kind)
            sender = _field(item, "user_id")
            lines.append(f"    type={name} user_id={sender}")

        pytest.fail(
            f"{peer_identity.username} never accepted a friend request from "
            f"{me.username} within 30s.\n"
            f"  pending(): {len(await peer.friend_requests.pending())} item(s)\n"
            f"  raw notifications: {len(items)} item(s)"
            + ("\n" + "\n".join(lines) if lines else " (inbox empty)")
            + f"\n  looking for a FRIENDSHIP_INVITE_CREATED from {me.id}"
        )

    # Give Root a moment to settle the friendship before the DM gate is tested.
    await asyncio.sleep(2)
    yield True
