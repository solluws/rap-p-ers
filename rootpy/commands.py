from __future__ import annotations

import inspect
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, get_type_hints, Union

from .models import Channel, Message, MessageAttachment, MessageSendResult
from .identifiers import normalize_root_guid
from .users import User

if TYPE_CHECKING:
    from .client import RootClient


CommandCallback = Callable[..., Awaitable[Any]]


USER_MENTION_RE = re.compile(
    r"^\[@(?P<name>[^\]]+)\]\(root://user/"
    r"(?P<user_id>[^)]+)\)$"
)

def parse_user_mention(value: str) -> Optional[tuple[str, str]]:
    match = USER_MENTION_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        user_id = normalize_root_guid(match.group("user_id"))
    except (TypeError, ValueError):
        return None

    return match.group("name"), user_id


def parse_user_id(value: str) -> Optional[str]:
    try:
        return normalize_root_guid(value)
    except (TypeError, ValueError):
        return None


CHANNEL_MENTION_RE = re.compile(
    r"^\[#(?P<name>[^\]]+)\]\(root://channel/"
    r"(?P<channel_id>[^)]+)\)$"
)


def parse_channel_mention(value: str) -> Optional[tuple[str, str]]:
    match = CHANNEL_MENTION_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        channel_id = normalize_root_guid(match.group("channel_id"))
    except (TypeError, ValueError):
        return None

    return match.group("name"), channel_id



def parse_channel_id(value: str) -> Optional[str]:
    try:
        return normalize_root_guid(value)
    except (TypeError, ValueError):
        return None


class CommandError(Exception):
    """Base command-processing error."""


class CommandNotFound(CommandError):
    pass


class CommandCheckFailure(CommandError):
    pass


class CommandArgumentError(CommandError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    callback: CommandCallback
    aliases: tuple[str, ...] = ()
    owner_only: bool = True
    description: str = ""


@dataclass
class Context:
    client: RootClient
    message: Message
    prefix: str
    invoked_with: str
    command: Command
    args: tuple[str, ...]
    raw_arguments: str

    @property
    def author_id(self) -> str:
        return self.message.user_id

    @property
    def author(self) -> User:
        return self.client.get_user(self.author_id)

    def get_user(self, user_id: str) -> User:
        mention = parse_user_mention(user_id)
        if mention is not None:
            username, user_id = mention
            return self.client.get_user(
                user_id,
                username=username,
            )

        raw_user_id = parse_user_id(user_id)
        if raw_user_id is None:
            raise CommandArgumentError(
                "Expected a Root user mention or user ID"
            )
        return self.client.get_user(raw_user_id)

    @property
    def mentioned_users(self) -> tuple[User, ...]:
        users = []
        for argument in self.args:
            mention = parse_user_mention(argument)
            if mention is None:
                continue
            username, user_id = mention
            users.append(
                self.client.get_user(user_id, username=username)
            )
        return tuple(users)

    @property
    def mentioned_user(self) -> Optional[User]:
        users = self.mentioned_users
        return users[0] if users else None

    @property
    def container_id(self) -> str:
        return self.message.container_id

    @property
    def community_id(self) -> Optional[str]:
        return self.message.community_id

    async def send(
        self,
        content: str,
        *,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        return await self.client.messages.send(
            self.container_id,
            content,
            community_id=self.community_id,
            delete_after=delete_after,
        )

    async def reply(
        self,
        content: str,
        *,
        delete_after: Optional[float] = None,
    ) -> MessageSendResult:
        return await self.client.messages.send(
            self.container_id,
            content,
            community_id=self.community_id,
            parent_message_ids=[self.message.id],
            needs_parent_notification=True,
            delete_after=delete_after,
        )

    def get_channel(self, channel_id: str) -> Channel:
        mention = parse_channel_mention(channel_id)
        if mention is not None:
            _, channel_id = mention

        channel = self.client.get_channel(channel_id)
        if channel is None:
            raise CommandArgumentError(
                f"Unknown or uncached channel: {channel_id}"
            )
        return channel

    @property
    def mentioned_channels(self) -> tuple[Channel, ...]:
        channels = []
        for argument in self.args:
            mention = parse_channel_mention(argument)
            if mention is None:
                continue
            _, channel_id = mention
            channel = self.client.get_channel(channel_id)
            if channel is not None:
                channels.append(channel)
        return tuple(channels)

    @property
    def mentioned_channel(self) -> Optional[Channel]:
        channels = self.mentioned_channels
        return channels[0] if channels else None

    async def call_user(self, user: User):
        return await self.client.calls.call_user(user)

    async def join_call(self, channel: Channel):
        return await self.client.calls.join_channel(channel)

    async def mute(self) -> None:
        container_id, community_id = (
            self.client.calls.require_active_target()
        )
        await self.client.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            muted=True,
        )

    async def unmute(self) -> None:
        container_id, community_id = (
            self.client.calls.require_active_target()
        )
        await self.client.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            muted=False,
        )

    async def deafen(self) -> None:
        container_id, community_id = (
            self.client.calls.require_active_target()
        )
        await self.client.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            deafened=True,
        )

    async def undeafen(self) -> None:
        container_id, community_id = (
            self.client.calls.require_active_target()
        )
        await self.client.calls.set_mute_and_deafen(
            container_id=container_id,
            community_id=community_id,
            deafened=False,
        )

    async def play_audio(
        self,
        source: Optional[Union[str, MessageAttachment]] = None,
        *,
        loop: bool = False,
    ):
        if source is None:
            source = self.message.first_audio_attachment
            if source is None:
                source = self.message.first_attachment
        if source is None:
            raise CommandArgumentError(
                "Attach an audio file or provide a local path/URL"
            )
        return await self.client.calls.play_audio(source, loop=loop)

    async def stop_audio(self) -> None:
        await self.client.calls.stop_audio()

    async def hangup(self) -> None:
        await self.client.calls.disconnect()


def parse_command_content(
    content: str,
    prefix: str,
) -> Optional[tuple[str, tuple[str, ...], str]]:
    if not content.startswith(prefix):
        return None

    remainder = content[len(prefix):].lstrip()
    if not remainder:
        return None

    try:
        parts = shlex.split(remainder, posix=True)
    except ValueError as exc:
        raise CommandArgumentError(str(exc)) from exc

    if not parts:
        return None

    invoked_with = parts[0].casefold()
    raw_arguments = remainder[len(parts[0]):].lstrip()
    return invoked_with, tuple(parts[1:]), raw_arguments


async def invoke_command(
    command: Command,
    context: Context,
) -> Any:
    callback = command.callback
    signature = inspect.signature(callback)
    parameters = list(signature.parameters.values())

    if not parameters:
        raise CommandArgumentError(
            f"Command {command.name!r} must accept a context argument"
        )

    try:
        type_hints = get_type_hints(callback)
    except (NameError, TypeError):
        type_hints = {}

    converted = []
    keyword_args = {}
    argument_index = 0
    command_parameters = parameters[1:]

    for parameter in command_parameters:
        annotation = type_hints.get(
            parameter.name,
            parameter.annotation,
        )

        # *args
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            converted.extend(
                context.args[argument_index:]
            )
            argument_index = len(context.args)
            continue

        # *, text: str
        #
        # Keyword-only arguments consume all remaining command arguments.
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            remaining = context.args[argument_index:]

            if not remaining:
                if parameter.default is not inspect.Parameter.empty:
                    continue

                raise CommandArgumentError(
                    f"Missing required argument: {parameter.name}"
                )

            value = " ".join(remaining)
            argument_index = len(context.args)

            keyword_args[parameter.name] = value
            continue

        if argument_index >= len(context.args):
            if parameter.default is not inspect.Parameter.empty:
                continue

            raise CommandArgumentError(
                f"Missing required argument: {parameter.name}"
            )

        value = context.args[argument_index]
        argument_index += 1

        if annotation is User:
            mention = parse_user_mention(value)

            if mention is not None:
                username, user_id = mention

                value = context.client.get_user(
                    user_id,
                    username=username,
                )
            else:
                user_id = parse_user_id(value)

                if user_id is None:
                    raise CommandArgumentError(
                        f"{parameter.name} must be a Root "
                        f"user mention or user ID"
                    )

                value = context.client.get_user(user_id)

        elif annotation is Channel:
            mention = parse_channel_mention(value)

            if mention is not None:
                _, channel_id = mention
            else:
                channel_id = value

            channel = context.client.get_channel(
                channel_id
            )

            if channel is None:
                raise CommandArgumentError(
                    f"Unknown or uncached channel: "
                    f"{channel_id}"
                )

            value = channel

        converted.append(value)

    if argument_index < len(context.args):
        raise CommandArgumentError(
            "Too many command arguments"
        )

    try:
        signature.bind(
            context,
            *converted,
            **keyword_args,
        )
    except TypeError as exc:
        raise CommandArgumentError(
            str(exc)
        ) from exc

    return await callback(
        context,
        *converted,
        **keyword_args,
    )