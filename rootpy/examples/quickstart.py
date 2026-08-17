"""rootpy quickstart -- driving your own Root account with one client.

This walks through the high-level convenience verbs on ``RootClient``. It's
written for automating *your own* account: sending your own messages, tidying
your own profile, calling your own friends. Fill in the IDs at the top with
values from your account and run it.

    python -m rootpy.examples.quickstart

Two patterns are shown:
  * `one_shot()`  -- log in, do a batch of actions, log out. No event loop.
  * `live_bot()`  -- stay connected and react to incoming events.
"""

from __future__ import annotations

import asyncio
import os

from rootpy import RootClient

# --- credentials -----------------------------------------------------------
# Prefer env vars over hard-coding. Set ROOT_USERNAME / ROOT_PASSWORD, or edit.
USERNAME = os.environ.get("ROOT_USERNAME", "your_username")
PASSWORD = os.environ.get("ROOT_PASSWORD", "your_password")

# --- fill these in with real ids/handles from your account -----------------
# Anything left as a placeholder is skipped, so the script is safe to run
# before you've filled everything in.
CHANNEL_ID = "PASTE-A-CHANNEL-ID"          # a text channel you can post in
VOICE_CHANNEL_ID = "PASTE-A-VOICE-CHANNEL-ID"
FRIEND_USER_ID = "PASTE-A-USER-ID"         # someone to DM / call
FRIEND_USERNAME = "some_username"          # someone to friend-request
AUDIO_FILE = "clip.mp3"                    # a local audio file for playback


def _set(value: str) -> bool:
    """True if a placeholder constant has actually been filled in."""
    return bool(value) and not value.startswith(("PASTE-", "your_", "some_"))


async def one_shot() -> None:
    """Log in, run a batch of account actions, then log out."""
    client = RootClient()
    await client.login(USERNAME, PASSWORD)  # REST actions don't need connect()

    # --- who am I -----------------------------------------------------------
    me = await client.whoami()
    print(f"Signed in as {me.username} ({me.id})")

    # --- profile ------------------------------------------------------------
    # Update several fields at once. Pass only what you want to change.
    await client.update_profile(
        status="tinkering with rootpy",
        description="automating my own account",
    )
    # ...or one field at a time:
    await client.update_status("back later")
    # await client.update_username("new_name")     # uncomment to rename
    # await client.update_avatar("avatar.png")     # local path or URL
    # await client.update_banner("banner.png")

    # --- messaging ----------------------------------------------------------
    if _set(CHANNEL_ID):
        sent = await client.message(CHANNEL_ID, "hello from rootpy")
        print("sent message:", sent.id)

        # message() also takes a User (DMs them) or a received Message (replies).
        # React to / edit / delete your own message via the returned object's
        # channel, or hold onto Message objects you receive from events.

    # --- direct messages ----------------------------------------------------
    if _set(FRIEND_USER_ID):
        friend = client.get_user(FRIEND_USER_ID)
        await client.direct_message(friend, "hey, testing my automation")

    # --- friends & blocks ---------------------------------------------------
    if _set(FRIEND_USERNAME):
        await client.add_friend(FRIEND_USERNAME)
    print("friends:", len(await client.list_friends()))
    print("blocked:", len(await client.list_blocked()))

    # --- communities & notifications ---------------------------------------
    communities = await client.list_communities()
    print("in", len(communities), "communities")
    print("unread notifications:", await client.unread_count())
    # await client.mark_all_read()

    # --- voice --------------------------------------------------------------
    # Join a voice channel, optionally play a clip, then leave.
    if _set(VOICE_CHANNEL_ID):
        await client.join_voice(VOICE_CHANNEL_ID)
        await client.mute(False)                 # make sure you're unmuted
        if os.path.exists(AUDIO_FILE):
            await client.play(AUDIO_FILE)
            await asyncio.sleep(5)
            await client.stop_playing()
        await client.leave_voice()

    # You can also call a friend directly (rings your 1:1 DM):
    # if _set(FRIEND_USER_ID):
    #     await client.call(client.get_user(FRIEND_USER_ID))
    #     await asyncio.sleep(10)
    #     await client.leave_voice()

    await client.close()


def live_bot() -> None:
    """Stay connected and respond to events on your own account."""
    client = RootClient(command_prefix=">")

    @client.event
    async def on_ready(_event) -> None:
        me = await client.whoami()
        print(f"connected as {me.username}")

    @client.event
    async def on_message(event) -> None:
        message = event.message
        # Ignore your own messages to avoid loops.
        if message.user_id == client.user_id:
            return
        if message.content.strip() == "!ping":
            await client.reply(message, "pong")

    # A command via the built-in prefix router (owner-only by default):
    @client.command(name="hello")
    async def hello(ctx) -> None:
        await ctx.reply("hi!")

    # run() logs in, connects, and blocks until you Ctrl-C.
    client.run(USERNAME, PASSWORD)


if __name__ == "__main__":
    asyncio.run(one_shot())
    # Swap the line above for the streaming example:
    # live_bot()
