# API reference

A map of the surface. Everything is async unless noted.

## Client

```python
client = RootClient(
    token=None,                    # bearer token; enough on its own for RPCs
    command_prefix="!",            # for @client.command
    auto_reconnect=True,
    preload_caches=True,           # load identity + community list at login
    expand_communities=False,      # fetch every community's detail at login
    preload_direct_messages=False,
)
```

### Session

| method | notes |
|---|---|
| `login_token(token=None)` | authenticate; no websocket |
| `connect()` | open the gateway; returns once up |
| `start_token(token=None)` | login + connect + **blocks forever** |
| `close()` | shut down cleanly |
| `whoami()` | current user |
| `RootClient.send_once(token, container, content, community_id=...)` | classmethod; one request, no login |

### Messages

| method | notes |
|---|---|
| `messages.send(container, content, community_id=..., parent_message_ids=[...])` | |
| `messages.list(container, community_id=..., direction="both", after=None, limit=None)` | one page |
| `messages.history(container, limit=200, page_size=50)` | async iterator, paginates |
| `messages.set_view_time(container, community_id=...)` | mark read |
| `messages.pin_list(container, community_id=...)` | pinned messages |
| `reply(message, content)` · `edit_message(message, content)` · `delete_message(message)` | |
| `react(message, emoji)` · `unreact(message, emoji)` · `pin(message)` · `unpin(message)` | |

Direction values: `"newer"`, `"older"`, `"both"`.

### Objects

```python
channel.send(content)              channel.history()
channel.history_iter(limit=500)    channel.mark_read()
channel.is_text                    channel.mention
channel.edit(**kwargs)             channel.delete()

message.reply(content)             message.edit(content)
message.react(emoji)               message.unreact(emoji)
message.pin()                      message.unpin()
message.delete()                   message.is_dm

dm.send(content)                   dm.history()

member.add_role(role_id)           member.remove_role(role_id)
member.kick()                      member.ban()

role.edit(**kwargs)                role.delete()   role.move(...)
```

### Communities

| method | notes |
|---|---|
| `list_communities()` | your communities |
| `community_detail(id, refresh=False)` | full detail, cached |
| `fetch_community(id)` | always fetches |
| `refresh_communities(expand=True)` | refresh the list |
| `get_members(community, refresh=False)` | every member (cached) |
| `get_member(community, user)` · `member_count(community)` | |
| `members_with_role(community, role)` | filter by role |
| `member_names(community)` | `{user_id: username or None}` (cache-only) |
| `get_members_detailed(community, batch_size=100)` | members + profiles, batched |
| `get_profile(user)` · `get_profiles([ids])` | username, pictures, about-me, status |
| `get_random_member(community, with_profile=True, exclude_self=True)` | one at random with username/pictures, or None |
| `get_random_members(community, n)` | n distinct, capped at available |

### UserProfile / DetailedMember

```python
profile.username           profile.about_me          # == .description
profile.avatar_url         # == .profile_picture_uri
profile.banner_uri         profile.custom_status
profile.online_status      profile.is_deleted

# DetailedMember exposes all of the above directly, plus:
member.user_id             member.role_ids
await member.add_role(id)  await member.kick()
```
| `extended.channels` · `.text_channels` · `.member(id)` · `.role(id)` | on CommunityExtended |
| `invites.join(code)` · `leave_community(id)` | |
| `community_admin.create_channel(community, group, name, channel_type=1)` | |
| `community_admin.create_role(community, name, ...)` | |
| `community_admin.delete_channel/delete_role/edit_channel/move_channel` | |
| `roles.add_to_members(community, role, [user_ids])` · `remove_from_members(...)` | |

### Friends, DMs, blocks

| method | notes |
|---|---|
| `friend_requests.send(username)` · `.accept(n)` · `.decline(n)` · `.pending()` | |
| `list_friends()` · `remove_friend(user)` | |
| `open_dm(user)` · `direct_message(user, content)` | |
| `block(user)` · `unblock(user)` · `list_blocked()` | |

### Account

| method | notes |
|---|---|
| `go_online()` · `go_idle()` · `go_invisible()` · `set_online_status(s)` | presence |
| `update_status(text)` | custom status; `None` clears |
| `change_profile_picture(source)` · `remove_profile_picture()` | path / Path / bytes / file object / URL / None |
| `change_banner(source)` | same source types |
| `update_username/description/profile` | |
| `assets.upload_file(path)` · `assets.upload_bytes(data, filename=...)` | returns an asset URI |

### Events

| method | notes |
|---|---|
| `@client.event` | one handler per event, named `on_<event>` |
| `add_listener(name, handler)` · `remove_listener(name, handler)` | many handlers |
| `add_message_listener(handler)` | every message |
| `wait_for(event, check=None, timeout=None)` | returns the event |
| `watch_unread(interval=3.0, include_dms=True)` | see every channel's new messages |
| `watch_channel(id)` · `unwatch_all()` | |

### Timings

```python
client.timing_report()     # readable table
client.timings()           # {"totals": {...}, "endpoints": {...}}
client.reset_timings()     # clear the counters
```

`roundtrip_ms` is network + server; `waiting_ms` is rate-limit/retry holds we
imposed; `overhead_ms` is framing and bookkeeping.

### Cache

```python
client.cache.username(user_id)     # str or None -- never fetches
client.cache.member(community_id, user_id)
client.cache.stats()               # {'users': .., 'hit_rate': .., ...}
```
