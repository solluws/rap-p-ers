# rootpy-client

[![tests](https://github.com/solluws/rap-p-ers/actions/workflows/tests.yml/badge.svg)](https://github.com/solluws/rap-p-ers/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/rootpy-client/)
[![license](https://img.shields.io/badge/license-MIT-green)](https://github.com/solluws/rap-p-ers/blob/main/LICENSE)

An async Python client for the [Root](https://rootapp.com) chat platform. It
drives one account — yours — from a script or a bot, and covers messaging,
communities, channels, roles, direct messages, friends, presence, voice
signalling and the real-time gateway.

Unofficial. Not affiliated with Root, and not endorsed by them.

**Guides, examples and the full API reference: <https://rootapp.gay>**

## Install

```bash
pip install rootpy-client
```

You install `rootpy-client` and you import `rootpy`. The two names differ
because an unrelated project already holds `rootpy` on PyPI. This is not that
project.

Python 3.10 or later. Playing audio into a voice call needs an opt-in extra:
`pip install "rootpy-client[voice]"`.

## Use it

```python
import asyncio
import os

from rootpy import RootClient


async def main():
    client = RootClient(token=os.environ["ROOT_TOKEN"])
    await client.login_token()

    await client.messages.send(CHANNEL_ID, "hello", community_id=COMMUNITY_ID)

    await client.close()


asyncio.run(main())
```

Receiving needs the gateway, and a community delivers nothing until you attach
to it:

```python
@client.event
async def on_message(event):
    print(event.message.content)


async with client.community.held(COMMUNITY_ID):
    await client.connect()
    await client.wait_until_ready()
    await asyncio.sleep(3600)
```

## What is in it

- **Messages** — send, reply in a thread, edit, delete, react, pin, and
  paginated history.
- **Communities** — create, join by invite, channels and channel groups, roles
  and permissions, members, invites, emoji and assets.
- **Discovery** — search the public directory, and list your own community.
- **Direct messages** — conversations, group conversations, friends and blocks.
- **Gateway** — a websocket connection with typed events, `wait_for()` and
  reconnection.
- **Voice** — call signalling, mute and deafen, and audio playback behind the
  `[voice]` extra.
- **Auth** — token login, email and password login, and signup.

## Notes worth knowing before you start

- **Membership is not a subscription.** Until you call
  `client.community.attach(id)`, the hub sends no packet for that community at
  all, while direct messages and mentions keep arriving on the same socket.
  That asymmetry is what a broken listener looks like.
- **The gateway is a resync cycle, not a persistent stream.** The hub sends a
  batch, closes with code 1000, and expects a reconnect. That is normal. An
  attach does not survive it, so hold it with `client.community.held(id)`.
- **Presence is two calls, and the lower one wins.** Others see
  `min(ceiling, device)`. Both halves succeed on their own, so setting only the
  ceiling leaves your account invisible to everybody and reports success.
  `client.set_presence(...)` sets both.

## Develop

```bash
git clone https://github.com/solluws/rap-p-ers
cd rap-p-ers
pip install -e ".[dev]"
python -m pytest -q
```

The suite is offline. It pins the wire format, so a change to an endpoint, a
field number or a default fails a test rather than a live request.

Pull requests are welcome. Keep the tests green, and add one for whatever you
change.

## Licence

MIT. See [LICENSE](https://github.com/solluws/rap-p-ers/blob/main/LICENSE).

Use this on accounts you control, and read Root's terms before you automate
anything on their service.
