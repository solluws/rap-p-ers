#!/usr/bin/env python3
"""
rootpy by example -- a guided tour of everything the SDK does.

Logs in (or creates an account), builds its own sandbox community, and then
demonstrates channels, roles, members, messages, DMs, friends, presence,
assets and real-time events inside it -- before deleting the whole thing.

Nothing touches your existing servers: every destructive demonstration happens
in a community this script creates and removes.

    python example.py                    # menu
    python example.py --token            # log in from tokens.txt, run it all
    python example.py --token --only messages roles
    python example.py --create           # create an account first
    python example.py --list             # list the sections
    python example.py --token --keep     # leave the sandbox community behind
"""

import argparse
import asyncio
import logging
from pathlib import Path

from rootpy import AccountFactory, AlreadyCreatedError, RootClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
for _noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("example")

TOKENS_FILE = Path(__file__).with_name("tokens.txt")
ACCOUNTS_FILE = Path(__file__).with_name("accounts.json")
EMAIL_PATTERN = "hello-{tag}@solluw.com"
SANDBOX_NAME = "rootpy demo"


def heading(text: str) -> None:
    log.info("")
    log.info("=" * 70)
    log.info(text)
    log.info("=" * 70)


def step(text: str) -> None:
    log.info("  %s", text)


def detail(text: str) -> None:
    log.info("      %s", text)


# ===================================================================== #
# Getting a client
# ===================================================================== #
def load_token() -> str:
    if TOKENS_FILE.exists():
        for line in TOKENS_FILE.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip().lower() == "token" and value.strip():
                return value.strip()
    raise SystemExit(
        f"No token found. Create {TOKENS_FILE.name} with:\n    token=<your token>"
    )


async def client_from_token() -> RootClient:
    heading("LOGGING IN")
    client = RootClient(token=load_token())
    await client.login_token()
    me = await client.whoami()
    step(f"logged in as {me.username} ({me.id})")
    detail(f"email verified: {getattr(me, 'is_email_verified', '?')}")
    return client


async def client_from_new_account() -> RootClient:
    """Create an account, then use it. See docs/two-step-signup.md."""
    heading("CREATING AN ACCOUNT")
    factory = AccountFactory(email_pattern=EMAIL_PATTERN)
    factory.load(ACCOUNTS_FILE)

    step("step 1: asking Root for a Turnstile challenge")
    try:
        challenge = await factory.create_return_turnstile()
    except AlreadyCreatedError as done:
        step("no challenge was needed -- account created")
        factory.save(ACCOUNTS_FILE)
        client = RootClient(token=done.account.token)
        await client.login_token()
        return client

    detail(challenge.describe())
    log.info("")
    log.info("  Solve this, then paste the token:")
    log.info("    %s", challenge)
    log.info("")
    log.info("    DevTools:")
    log.info("      document.querySelector('[name=\"cf-turnstile-response\"]').value")
    token = input("  token> ").strip()
    if not token:
        raise SystemExit("no token -- nothing created")

    step("step 3: finishing signup (same username/email/device id)")
    account, client = await factory.create_with_turnstile(
        turnstile_token=token, challenge=challenge, keep_client=True,
        note="created by example.py",
    )
    factory.save(ACCOUNTS_FILE)
    step(f"created {account.username} <{account.email}>")
    detail(f"user id : {account.user_id}")
    detail(f"saved to {ACCOUNTS_FILE.name} -- keep it private")

    step("email verification")
    try:
        await factory.send_verification(account)
        code = input("  code from the email (blank to skip)> ").strip()
        if code:
            await factory.verify(account, code)
            detail(f"verified: {account.verified}")
        else:
            detail("skipped -- factory.verify(account, code) later")
    except Exception as exc:
        detail(f"verification unavailable: {type(exc).__name__}: {exc}")

    return client


# ===================================================================== #
# Sections
# ===================================================================== #
async def profile(client: RootClient, state: dict) -> None:
    """Your account: name, status, presence, pictures."""
    heading("PROFILE AND PRESENCE")

    me = await client.whoami()
    step(f"whoami() -> {me.username}")

    full = await client.get_profile(me.id)
    if full:
        detail(f"about me : {(full.about_me or '(none)')[:50]}")
        detail(f"avatar   : {full.avatar_url or '(none)'}")
        detail(f"banner   : {full.banner_uri or '(none)'}")
        detail(f"status   : {full.custom_status or '(none)'}")

    step("presence: idle -> invisible -> online")
    detail("others see min(ceiling, device) -- set_presence() sets both halves")
    for label, call in (
        ("idle", client.go_idle),
        ("invisible", client.go_invisible),
        ("online", client.go_online),
    ):
        await call()
        detail(f"now {label}; client.presence reads back {client.presence.name}")
        await asyncio.sleep(0.4)
    detail("client.users.set_online_status() sets the ceiling ALONE -- an "
           "account that never announced a device stays invisible to "
           "everybody while that call reports success")

    step("custom status")
    await client.update_status("touring rootpy")
    detail("set")
    await asyncio.sleep(0.4)
    await client.update_status(None)
    detail("cleared")

    step("profile pictures accept a path, Path, bytes, file object, or URL")
    detail("await client.change_profile_picture('avatar.png')")
    detail("await client.change_banner(image_bytes)")
    detail("await client.remove_profile_picture()")


async def community(client: RootClient, state: dict) -> None:
    """Create a community, channel groups and channels; then manage them."""
    heading("COMMUNITY, CHANNEL GROUPS AND CHANNELS")

    existing = await client.list_communities()
    step(f"you're in {len(existing)} communities")
    for entry in existing[:3]:
        detail(f"{entry.id[:8]}  {entry.name}")

    step(f"creating a sandbox community: {SANDBOX_NAME!r}")
    created = await client.community_admin.create_community(
        SANDBOX_NAME, description="temporary -- created by example.py",
    )
    state["community_id"] = created.id
    detail(f"id: {created.id}")

    step("creating a channel group")
    group = await client.community_admin.create_channel_group(
        created.id, "Demo Section",
    )
    state["group_id"] = group.id
    detail(f"{group.name} ({group.id[:8]})")

    step("creating channels (channel_type: 1 = TEXT, 4 = VOICE)")
    text_channel = await client.community_admin.create_channel(
        created.id, group.id, "general-demo", channel_type=1,
        description="made by example.py",
    )
    state["channel_id"] = text_channel.id
    detail(f"text  : #{text_channel.name} ({text_channel.id[:8]})")

    voice_channel = await client.community_admin.create_channel(
        created.id, group.id, "Voice Demo", channel_type=4,
    )
    detail(f"voice : {voice_channel.name} ({voice_channel.id[:8]})")

    step("reading it back")
    detailed = await client.community_detail(created.id, refresh=True)
    detail(f"{len(detailed.channels)} channels, "
           f"{len(detailed.text_channels)} text-capable")
    for channel in detailed.channels:
        detail(f"  {'text ' if channel.is_text else 'other'}  #{channel.name}")

    step("editing a channel")
    await client.community_admin.edit_channel(
        created.id, text_channel.id, name="general-renamed",
        description="renamed by example.py",
    )
    detail("renamed to #general-renamed")

    fresh = await client.community_detail(created.id, refresh=True)
    obj = next((c for c in fresh.channels if c.id == text_channel.id), None)
    if obj is not None:
        detail(f"channel.mention -> {obj.mention}")
        detail(f"channel.is_text -> {obj.is_text}")

    step("deleting the voice channel")
    await client.community_admin.delete_channel(created.id, voice_channel.id)
    detail("deleted")


async def roles(client: RootClient, state: dict) -> None:
    """Create, edit, assign, remove and delete roles."""
    heading("ROLES")
    community_id = state.get("community_id")
    if not community_id:
        step("(needs the community section first)")
        return

    me = await client.whoami()

    step("creating a role")
    role = await client.community_admin.create_role(
        community_id, "Demo Role", color_hex="7C3AED",
    )
    state["role_id"] = role.id
    detail(f"{role.name} ({role.id[:8]})")

    step("listing roles")
    detailed = await client.community_detail(community_id, refresh=True)
    for entry in detailed.roles:
        detail(f"{entry.id[:8]}  {entry.name}")

    step("assigning it to myself")
    await client.roles.add_to_members(community_id, role.id, [me.id])
    detail("assigned")
    holders = await client.members_with_role(community_id, role.id, refresh=True)
    detail(f"members with that role: {len(holders)}")

    step("editing the role")
    await client.community_admin.edit_role(
        community_id, role.id, name="Demo Role (edited)", color_hex="EF4444",
    )
    detail("renamed and recoloured")

    step("removing it from myself")
    await client.roles.remove_from_members(community_id, role.id, [me.id])
    detail("removed")

    step("deleting the role")
    await client.community_admin.delete_role(community_id, role.id)
    detail("deleted")


async def members(client: RootClient, state: dict) -> None:
    """Member lists, profiles, random picks, member actions."""
    heading("MEMBERS")
    community_id = state.get("community_id")
    if not community_id:
        step("(needs the community section first)")
        return

    people = await client.get_members(community_id, refresh=True)
    step(f"get_members() -> {len(people)}")
    step(f"member_count() -> {await client.member_count(community_id)}")
    for member in people[:5]:
        detail(f"{member.user_id[:8]}  roles={len(member.role_ids)}")

    step("with profiles attached (batched, 100 per request)")
    detailed = await client.get_members_detailed(community_id)
    for member in detailed[:5]:
        detail(f"{(member.username or member.user_id[:8]):20} "
               f"{(member.about_me or '')[:30]}")

    step("a random member (excludes you; profile included)")
    pick = await client.get_random_member(community_id)
    if pick is None:
        detail("no one else here -- it's a brand new community")
    else:
        detail(f"{pick.username or pick.user_id[:8]}")
        detail(f"avatar: {(pick.avatar_url or 'none')[:50]}")

    step("member objects can act on themselves")
    detail("await member.add_role(role_id)")
    detail("await member.remove_role(role_id)")
    detail("await member.kick() / await member.ban()")


async def messages(client: RootClient, state: dict) -> None:
    """Send, read, reply, react, edit, pin, and page through history."""
    heading("MESSAGES")
    community_id = state.get("community_id")
    channel_id = state.get("channel_id")
    if not (community_id and channel_id):
        step("(needs the community section first)")
        return

    detailed = await client.community_detail(community_id, refresh=True)
    channel = next((c for c in detailed.channels if c.id == channel_id), None)
    if channel is None:
        step("channel not found")
        return

    step("channel.send()")
    sent = await channel.send("hello from example.py")
    detail(f"id {sent.id[:8]}")

    step("waiting for it to become readable")
    found = None
    for _ in range(10):
        await asyncio.sleep(0.5)
        history = await channel.history()
        found = next((m for m in history if m.id == sent.id), None)
        if found is not None:
            break
    detail("visible in history" if found else "not visible yet")
    if found is None:
        return

    step("message.reply() -- threaded")
    await found.reply("...and a reply")

    step("react / unreact")
    await found.react("\U0001F44D")
    await asyncio.sleep(0.4)
    await found.unreact("\U0001F44D")
    detail("added then removed")

    step("edit, then re-read to confirm it stuck")
    await found.edit("edited by example.py")
    await asyncio.sleep(1.0)
    again = await channel.history()
    updated = next((m for m in again if m.id == found.id), None)
    detail(f"content now: {(updated.content if updated else '?')[:40]!r}")

    step("pin / unpin")
    await found.pin()
    await asyncio.sleep(0.4)
    await found.unpin()
    detail("pinned then unpinned")

    step("history_iter() -- pages the cursor for you, newest first")
    collected = [m async for m in channel.history_iter(limit=20)]
    detail(f"{len(collected)} message(s)")

    step("mark_read()")
    await channel.mark_read()

    step("deleting the message")
    await found.delete()


async def dms(client: RootClient, state: dict) -> None:
    """Direct messages."""
    heading("DIRECT MESSAGES")

    conversations = await client.direct_messages.list()
    step(f"direct_messages.list() -> {len(conversations)} conversation(s)")
    for conversation in conversations[:3]:
        detail(f"{conversation.id[:8]}  "
               f"members={len(conversation.member_user_ids)}")

    step("opening and using one")
    detail("dm = await client.open_dm(user_id)")
    detail("await dm.send('hey')")
    detail("history = await dm.history()")
    detail("one-liner: await client.direct_message(user_id, 'hey')")

    if conversations:
        first = conversations[0]
        history = await client.messages.list(first.id, direction="both")
        detail(f"newest conversation has {len(history)} recent message(s)")


async def friends(client: RootClient, state: dict) -> None:
    """Friend requests, friends, blocks."""
    heading("FRIENDS AND BLOCKS")

    friend_list = await client.list_friends()
    step(f"list_friends() -> {len(friend_list)}")

    pending = await client.friend_requests.pending()
    step(f"friend_requests.pending() -> {len(pending)}")
    detail("await client.friend_requests.send('someusername')")
    detail("await client.friend_requests.accept(notification)")
    detail("await client.friend_requests.decline(notification)")
    detail("await client.remove_friend(user_id)")

    blocked = await client.list_blocked()
    step(f"list_blocked() -> {len(blocked)}")
    detail("await client.block(user_id) / await client.unblock(user_id)")


async def events(client: RootClient, state: dict) -> None:
    """Gateway, typed events, wait_for, attach, watch_unread."""
    heading("REAL-TIME EVENTS")

    @client.event
    async def on_message(event):
        log.info("      [MSG] %s", event.message.content[:50])

    @client.event
    async def on_channel_create(event):
        log.info("      [CHANNEL +] #%s", event.name)

    @client.event
    async def on_role_add(event):
        log.info("      [ROLE +] %s -> %s",
                 event.role_name or event.role_id, ", ".join(event.user_ids))

    step("connecting the gateway")
    await client.connect()
    detail("connected")

    step("typed events")
    for name in ("on_message", "on_friend_request", "on_member_join",
                 "on_member_leave", "on_role_add", "on_role_remove",
                 "on_channel_create", "on_channel_edit", "on_channel_delete",
                 "on_block_add", "on_block_remove"):
        detail(name)
    detail("add_listener(name, fn) registers extra handlers for one event")

    step("community.hold() -- what makes channel messages push, and keeps it")
    community_id = state.get("community_id")
    if community_id:
        await client.community.hold(community_id)
        detail("attached and held; channel posts now arrive as on_message "
               "in ~0.2s")
        detail("a bare community.attach() would decay on its own -- the "
               "subscription lives with the hub connection, and this hub "
               "closes each batch. Measured gone by +31s with no gateway, "
               "+48s when the socket was killed under it, nothing raised")
        detail("hold() re-attaches every time the socket comes back; "
               "release(id) stops. 'async with client.community.held(id)' "
               "is the same thing scoped to a block")
    detail("UnreadReader(client) does this for every community and reads "
           "what arrives -- see examples/ and the README")

    step("watch_unread() -- the polling alternative, no presence")
    client.watch_unread(interval=3.0, include_dms=True)

    channel_id = state.get("channel_id")
    if channel_id:
        detailed = await client.community_detail(state["community_id"])
        channel = next(
            (c for c in detailed.channels if c.id == channel_id), None
        )
        if channel is not None:
            step("posting something for the watcher to find")
            await channel.send("event demo message")

    step("wait_for('message', timeout=10)")
    try:
        event = await client.wait_for("message", timeout=10)
        detail(f"got: {event.message.content[:40]!r}")
    except asyncio.TimeoutError:
        detail("timed out -- nothing arrived (normal on a quiet account)")

    if community_id:
        step("community.release() -- stop holding, and detach")
        await client.community.release(community_id)
        detail("released")

    client.unwatch_all()


async def assets(client: RootClient, state: dict) -> None:
    """Asset URIs -> signed URLs -> bytes on disk."""
    heading("ASSETS")

    me = await client.whoami()
    profile_data = await client.get_profile(me.id)
    uri = (getattr(profile_data, "avatar_url", None)
           or getattr(profile_data, "banner_uri", None))
    if not uri:
        step("no avatar or banner set on this account")
        detail("set one with client.change_profile_picture('avatar.png')")
        return

    step(f"asset uri: {uri}")
    asset = await client.get_asset(uri)
    if asset is None:
        detail("could not resolve")
        return

    step(f"{len(asset.links)} size variant(s)")
    for link in asset.links:
        detail(f"{link.max_dimension:>5}px  {link.url[:56]}...")

    step("picking a size")
    detail(f"best_url          : {(asset.best_url or '')[:56]}...")
    detail(f"url_for_size(128) : {(asset.url_for_size(128) or '')[:56]}...")

    step("these links are signed and expire")
    detail(f"expires_at: {asset.expires_at}")
    detail("store the bytes for anything lasting:")
    path = await client.save_asset(uri, "example-avatar")
    detail(f"save_asset() -> {path}")


async def timings(client: RootClient, state: dict) -> None:
    """Where the time went."""
    heading("TIMINGS AND CACHE")
    for line in client.timing_report().splitlines():
        log.info("  %s", line)
    log.info("")
    step(f"cache: {client.cache.stats()}")


# ===================================================================== #
SECTIONS = {
    "profile": profile,
    "community": community,
    "roles": roles,
    "members": members,
    "messages": messages,
    "dms": dms,
    "friends": friends,
    "events": events,
    "assets": assets,
    "timings": timings,
}

NEEDS_COMMUNITY = {"roles", "members", "messages", "events"}


async def cleanup(client: RootClient, state: dict) -> None:
    community_id = state.get("community_id")
    if not community_id:
        return
    heading("CLEANUP")
    step("deleting the sandbox community and everything in it")
    try:
        await client.community_admin.delete_community(community_id)
        detail("deleted")
    except Exception as exc:
        detail(f"couldn't delete: {type(exc).__name__}: {exc}")
        detail(f"remove it by hand: {community_id}")


def choose_login() -> str:
    print()
    print("How would you like to run this?")
    print("  1. use an existing token (tokens.txt)")
    print("  2. create a new account first")
    print("  q. quit")
    return {"1": "token", "2": "create"}.get(input("> ").strip().lower(), "quit")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", action="store_true",
                        help="log in with tokens.txt")
    parser.add_argument("--create", action="store_true",
                        help="create an account first")
    parser.add_argument("--only", nargs="*", metavar="SECTION",
                        help="run only these sections")
    parser.add_argument("--list", action="store_true",
                        help="list the sections and exit")
    parser.add_argument("--keep", action="store_true",
                        help="don't delete the sandbox community afterwards")
    args = parser.parse_args()

    if args.list:
        print("\nSections:\n")
        for name, fn in SECTIONS.items():
            print(f"  {name:10} {(fn.__doc__ or '').strip().splitlines()[0]}")
        print("\n  python example.py --token --only messages roles\n")
        return

    mode = "token" if args.token else "create" if args.create else choose_login()
    if mode == "quit":
        return

    client = (await client_from_token() if mode == "token"
              else await client_from_new_account())

    wanted = args.only or list(SECTIONS)
    unknown = [name for name in wanted if name not in SECTIONS]
    if unknown:
        raise SystemExit(f"unknown section(s): {', '.join(unknown)}")

    if any(name in NEEDS_COMMUNITY for name in wanted) and "community" not in wanted:
        wanted = ["community"] + wanted

    state: dict = {}
    try:
        for name in wanted:
            try:
                await SECTIONS[name](client, state)
            except Exception as exc:
                log.error("  section %r failed: %s: %s",
                          name, type(exc).__name__, exc)
                for item in getattr(exc, "validation_errors", None) or []:
                    log.error("      %s", item)
    finally:
        if not args.keep:
            await cleanup(client, state)
        elif state.get("community_id"):
            log.info("")
            log.info("  --keep: sandbox community left in place: %s",
                     state["community_id"])
        await client.close()
        log.info("")
        log.info("done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
