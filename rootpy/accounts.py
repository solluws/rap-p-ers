"""Creating test accounts on a whitelisted email domain.

Root gates signup behind a Cloudflare Turnstile challenge. That challenge is
the platform's control over automated signup, so this module doesn't try to
defeat it: you solve it yourself and pass the resulting token in. Everything
around it -- naming, the signup call, verification, bookkeeping -- is handled
here.

    from rootpy.accounts import AccountFactory

    factory = AccountFactory(email_pattern="hello-{tag}@example.com")
    account = await factory.create(turnstile_token=token)
    print(account.username, account.email)
    factory.save("accounts.json")          # keep the credentials somewhere safe

Because the accounts share one email pattern, whoever runs the platform can
find and remove them in one sweep -- which is usually the condition attached
to permission for this sort of thing.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import RootClient
from .exceptions import RootError

log = logging.getLogger("rootpy.accounts")


class AlreadyCreatedError(RootError):
    """Raised when a signup succeeded without needing a challenge.

    Not really an error -- the account exists and is recorded; this just tells
    the two-step flow there's nothing to solve. ``.account`` is the result.
    """

    def __init__(self, account) -> None:
        super().__init__(
            f"account {account.username!r} was created without a challenge"
        )
        self.account = account

_USERNAME_ALPHABET = string.ascii_lowercase + string.digits
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"


class TurnstileChallenge(str):
    """The challenge URL -- and the signup it belongs to.

    This is a ``str``, so it behaves exactly like the URL everywhere you'd
    use one::

        challenge_url = await factory.create_return_turnstile(username="bob")
        token = my_solver(challenge_url)          # just a string

    But it also remembers the details of the attempt that produced it
    (``username``, ``password``, ``email``, ``device_id``). Those have to be
    identical on the retry, so passing this object back to
    :meth:`AccountFactory.create_with_turnstile` is the safe way to finish:

        account = await factory.create_with_turnstile(
            turnstile_token=token, challenge=challenge_url,
        )
    """

    username: str = ""
    password: str = ""
    email: str = ""
    device_id: str = ""
    tag: str = ""
    action: str = ""

    def __new__(cls, url: str, **context):
        obj = super().__new__(cls, url or "")
        for key, value in context.items():
            setattr(obj, key, value)
        return obj

    @property
    def challenge_url(self) -> str:
        """The URL itself, if you'd rather be explicit."""
        return str(self)

    def context(self) -> dict:
        """The signup details that must be reused on the retry."""
        return {
            "username": self.username,
            "password": self.password,
            "email": self.email,
            "device_id": self.device_id,
            "tag": self.tag,
        }

    def describe(self) -> str:
        parts = [f"challenge for {self.username!r} <{self.email}>"]
        from urllib.parse import parse_qs, urlparse

        try:
            params = {k: v[0] for k, v in parse_qs(urlparse(str(self)).query).items()}
            if "action" in params:
                parts.append(f"action={params['action']}")
            if "cdata" in params:
                parts.append(f"cdata={params['cdata'][:16]}...")
        except Exception:
            pass
        return " | ".join(parts)


@dataclass
class CreatedAccount:
    """Credentials and identifiers for an account this factory made."""

    username: str
    password: str
    email: str
    user_id: Optional[str] = None
    token: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    verified: bool = False
    device_id: Optional[str] = None
    note: str = ""

    def redacted(self) -> dict:
        """A copy safe to print or log -- no password, no token."""
        data = asdict(self)
        data["password"] = "***"
        data["token"] = "***" if self.token else None
        return data


class AccountFactory:
    """Creates accounts on one email domain and remembers what it made.

    email_pattern:
        Must contain ``{tag}``, e.g. ``"hello-{tag}@example.com"``. Keeping
        every account on one recognisable pattern is what makes them easy to
        find and remove later.
    username_prefix:
        Optional prefix so the accounts are identifiable in the UI too.
    """

    def __init__(
        self,
        email_pattern: str,
        *,
        username_prefix: str = "",
        transport=None,
        proxy: Optional[str] = None,
    ) -> None:
        if "{tag}" not in email_pattern:
            raise ValueError(
                "email_pattern must contain {tag}, e.g. 'hello-{tag}@example.com'"
            )
        self.email_pattern = email_pattern
        self.username_prefix = username_prefix
        self.transport = transport
        self.proxy = proxy
        self.accounts: list[CreatedAccount] = []

    # ------------------------------------------------------------------ #
    def _routing(self) -> dict:
        """How this factory reaches Root -- forwarded only when it is set.

        These two are classmethods with no client behind them, so without this
        they build a bare transport and go direct: the one request that proves
        you own an address, sent around the proxy the signup used. Passing the
        keys only when they have values keeps a factory with no proxy calling
        exactly as it always did.
        """
        routing = {}
        if self.proxy is not None:
            routing["proxy"] = self.proxy
        if self.transport is not None:
            routing["transport"] = self.transport
        return routing

    @staticmethod
    def _tag(length: int = 8) -> str:
        return "".join(secrets.choice(_USERNAME_ALPHABET) for _ in range(length))

    @staticmethod
    def make_password(length: int = 24) -> str:
        """A random password. Long, because nobody types these."""
        return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))

    def make_username(self, tag: Optional[str] = None) -> str:
        tag = tag or self._tag()
        name = f"{self.username_prefix}{tag}" if self.username_prefix else tag
        return name[:32]

    def make_email(self, tag: str) -> str:
        return self.email_pattern.format(tag=tag)

    # ------------------------------------------------------------------ #
    async def create(
        self,
        *,
        turnstile_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tag: Optional[str] = None,
        note: str = "",
        keep_client: bool = False,
        device_id: Optional[str] = None,
    ):
        """Create one account.

        turnstile_token:
            The Cloudflare Turnstile token for this signup. Solve the
            challenge yourself (see the Account creation guide) and pass it here --
            this library does not and will not solve it for you, and tokens
            are single-use and short-lived, so one per account.

            Pass None to find out what the server wants: signup is attempted
            without one and Root raises
            :class:`~rootpy.exceptions.TurnstileRequired` carrying the real
            challenge URL.

        Returns ``(CreatedAccount, client)`` when ``keep_client`` is True,
        otherwise just the :class:`CreatedAccount`.
        """
        # No local gate on the token: let Root answer. If it wants a challenge
        # it raises TurnstileRequired with the real challenge URL, which is far
        # more useful than a guess -- and if a whitelisted domain ever doesn't
        # need one, this just works.
        turnstile_token = (turnstile_token or "").strip() or None

        tag = tag or self._tag()
        username = username or self.make_username(tag)
        password = password or self.make_password()
        email = self.make_email(tag)
        # One device id per account, reused if a Turnstile retry is needed --
        # Root binds the challenge to the request, and a new device id makes
        # the retry look like a different signup.
        if device_id is None:
            from .identifiers import create_desktop_device_guid

            device_id = create_desktop_device_guid()

        log.info("creating account %s <%s>", username, email)
        client = await RootClient.create_account(
            username=username,
            password=password,
            email=email,
            turnstile_token=turnstile_token,
            device_id=device_id,
            transport=self.transport,
            proxy=self.proxy,
        )

        account = CreatedAccount(
            username=username,
            password=password,
            email=email,
            user_id=getattr(client.user, "id", None) or client.user_id,
            token=getattr(client.session, "token", None),
            device_id=device_id,
            note=note,
        )
        self.accounts.append(account)
        log.info("created %s (%s)", account.username, account.user_id)

        if keep_client:
            return account, client
        await client.close()
        return account

    # ------------------------------------------------------------------ #
    # Two-step signup: get the challenge, solve it however you like, finish.
    # ------------------------------------------------------------------ #
    async def create_return_turnstile(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tag: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> TurnstileChallenge:
        """STEP 1 -- attempt a signup and return the challenge URL.

            challenge_url = await factory.create_return_turnstile(username="bob")

        This deliberately sends a signup WITHOUT a token so Root issues a
        challenge, and hands you back the URL it named. Solve it however you
        like, then pass the token to :meth:`create_with_turnstile`.

        The returned value is a plain string (the URL) that also carries the
        username, password, email and device id of this attempt. Those must be
        identical on the retry, so hand the object straight back rather than
        re-deriving them.

        Raises :class:`AlreadyCreatedError` if the account was created without
        a challenge (some accounts don't get one), and the account is recorded
        as normal.
        """
        tag = tag or self._tag()
        username = username or self.make_username(tag)
        password = password or self.make_password()
        email = self.make_email(tag)
        if device_id is None:
            from .identifiers import create_desktop_device_guid

            device_id = create_desktop_device_guid()

        from .exceptions import TurnstileRequired

        log.info("requesting challenge for %s <%s>", username, email)
        try:
            client = await RootClient.create_account(
                username=username,
                password=password,
                email=email,
                device_id=device_id,
                transport=self.transport,
                proxy=self.proxy,
            )
        except TurnstileRequired as exc:
            challenge = TurnstileChallenge(
                exc.challenge_url or "",
                username=username, password=password, email=email,
                device_id=device_id, tag=tag,
                action=getattr(exc, "action", "") or "",
            )
            log.info("challenge issued: %s", challenge.describe())
            return challenge

        # No challenge was required -- the account already exists now.
        account = self._record(client, username, password, email, device_id,
                               note="created without a challenge")
        await client.close()
        raise AlreadyCreatedError(account)

    async def create_with_turnstile(
        self,
        *,
        turnstile_token: str,
        challenge: Optional[TurnstileChallenge] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        email: Optional[str] = None,
        device_id: Optional[str] = None,
        note: str = "",
        keep_client: bool = False,
    ):
        """STEP 3 -- finish the signup with a solved token.

            account = await factory.create_with_turnstile(
                turnstile_token=token, challenge=challenge_url,
            )

        Pass the object from :meth:`create_return_turnstile` as ``challenge``
        and everything else is filled in for you. If you'd rather be explicit,
        supply ``username``/``password``/``email``/``device_id`` yourself --
        but they MUST match the attempt that produced the challenge, or Root
        issues a fresh one and your token is never checked.
        """
        if not turnstile_token:
            raise ValueError("turnstile_token is required")

        if challenge is not None and hasattr(challenge, "context"):
            context = challenge.context()
            username = username or context["username"]
            password = password or context["password"]
            email = email or context["email"]
            device_id = device_id or context["device_id"]

        missing = [
            name for name, value in (
                ("username", username), ("password", password),
                ("email", email), ("device_id", device_id),
            ) if not value
        ]
        if missing:
            raise ValueError(
                "missing " + ", ".join(missing) + " -- pass challenge=<the "
                "object from create_return_turnstile()>, or supply them "
                "explicitly. They must match the attempt the challenge came "
                "from."
            )

        log.info("completing signup for %s with a %d-char token",
                 username, len(turnstile_token))
        client = await RootClient.create_account(
            username=username,
            password=password,
            email=email,
            turnstile_token=turnstile_token,
            device_id=device_id,
            transport=self.transport,
            proxy=self.proxy,
        )
        account = self._record(client, username, password, email, device_id,
                               note=note)
        if keep_client:
            return account, client
        await client.close()
        return account

    def _record(self, client, username, password, email, device_id, note=""):
        account = CreatedAccount(
            username=username,
            password=password,
            email=email,
            user_id=getattr(client.user, "id", None) or client.user_id,
            token=getattr(client.session, "token", None),
            device_id=device_id,
            note=note,
        )
        self.accounts.append(account)
        log.info("created %s (%s)", account.username, account.user_id)
        return account

    async def send_verification(self, account, *,
                                turnstile_token: Optional[str] = None) -> None:
        """Ask Root to email a verification code to this account.

        The code goes to the account's address -- with a pattern like
        ``service-{tag}@example.com`` that's your own mail server, so how you
        read it is up to you (IMAP, a catch-all webhook, an API).

        This can raise :class:`~rootpy.exceptions.TurnstileRequired`: Root
        gates the resend behind its own challenge (``action=resend_verification``),
        separate from the one you solved at signup. Solve it and call again
        with ``turnstile_token=``.
        """
        await RootClient.send_email_verification(
            token=account.token, turnstile_token=turnstile_token,
            **self._routing(),
        )
        log.info("verification code sent to %s", account.email)

    async def verify(self, account, code: str) -> bool:
        """Complete verification with the code from the email.

        Returns True on success; the account's ``verified`` flag is updated.
        """
        code = (code or "").strip()
        if not code:
            raise ValueError("a verification code is required")
        await RootClient.verify_email(
            code, token=account.token, **self._routing())
        account.verified = True
        log.info("verified %s", account.email)
        return True

    async def create_and_verify(
        self,
        *,
        turnstile_token: Optional[str] = None,
        code_provider,
        timeout: float = 120.0,
        poll_interval: float = 5.0,
        resend: bool = False,
        **kwargs,
    ):
        """Create an account, then verify it once the code arrives.

        ``code_provider`` is your own function that returns the verification
        code for an address (or None if it hasn't arrived yet)::

            async def read_code(email: str):
                # however you read your mail -- IMAP, a webhook store, an API
                return await my_mailbox.latest_code(email)

            account = await factory.create_and_verify(
                turnstile_token=token, code_provider=read_code,
            )

        It's deliberately your function: the mail side is your infrastructure,
        and this library shouldn't guess at it.

        resend:
            Signup itself already emails the code, so by default this just
            waits for that one. Asking for another is a *separate* Turnstile
            challenge (``action=resend_verification``) -- the signup token
            doesn't satisfy it -- so on a fresh account the resend usually
            just raises :class:`~rootpy.exceptions.TurnstileRequired` and
            throws away credentials that were already minted. Pass
            ``resend=True`` only if the first email genuinely never arrived,
            and expect to solve that challenge.
        """
        import asyncio
        import inspect

        account = await self.create(turnstile_token=turnstile_token, **kwargs)
        if resend:
            await self.send_verification(account)

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = code_provider(account.email)
            if inspect.isawaitable(result):
                result = await result
            if result:
                await self.verify(account, result)
                return account
            await asyncio.sleep(poll_interval)

        log.warning(
            "no verification code for %s within %.0fs -- account created but "
            "unverified; call factory.verify(account, code) later",
            account.email, timeout,
        )
        return account

    # ------------------------------------------------------------------ #
    def unverified(self) -> list:
        """Accounts that were created but never verified."""
        return [a for a in self.accounts if not a.verified]

    # ------------------------------------------------------------------ #
    def save(self, path, *, include_secrets: bool = True) -> str:
        """Write the created accounts to a JSON file.

        These are real credentials -- keep the file out of version control
        and off anything public. ``include_secrets=False`` writes a redacted
        copy suitable for sharing.
        """
        target = Path(path).expanduser()
        payload = [
            asdict(account) if include_secrets else account.redacted()
            for account in self.accounts
        ]
        target.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        if include_secrets:
            log.warning(
                "%s contains passwords and tokens -- keep it private", target
            )
        return str(target)

    def load(self, path) -> list:
        """Read accounts back from a file written by :meth:`save`."""
        target = Path(path).expanduser()
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8"))
        self.accounts = [CreatedAccount(**entry) for entry in data]
        return self.accounts

    def emails(self) -> list:
        """Every email this factory has used -- useful for cleanup requests."""
        return [account.email for account in self.accounts]
