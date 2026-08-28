#!/usr/bin/env python3
"""
Show exactly what a signup sends and what comes back.

Signup failures are opaque -- "one or more request values were rejected"
doesn't say which. This prints the whole exchange: the endpoint, the headers,
every protobuf field in the body, and the full response including Root's
structured error detail (which usually names the offending field).

    python signupdebug.py                          # dry run, sends nothing
    python signupdebug.py --send                   # actually attempt it
    python signupdebug.py --send --token 0.ABC...  # with a captcha token
    python signupdebug.py --send --username foo --email me@example.com

Nothing is sent unless you pass --send.
"""

import argparse
import asyncio
import logging
import secrets
import string
import sys
import time

# Run from anywhere, installed or not: put the project root on sys.path.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from rootpy.auth import PASSWORD_SIGNUP, AuthClient
from rootpy.identifiers import create_desktop_device_guid
from rootpy.exceptions import TurnstileRequired
from rootpy.protocol import (
    decode_root_guid_message,
    iter_fields,
    unwrap_grpc_web,
)
from rootpy.transport import GrpcWebTransport

logging.basicConfig(level=logging.INFO, format="%(message)s")
for noisy in ("httpx", "httpcore", "hpack", "h2", "websockets", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("signupdebug")

EMAIL_PATTERN = "hello-{tag}@solluw.com"
SIGNUP_PAGE = "https://rootapp.com/signup"
MAX_ATTEMPTS = 3


def random_tag(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def describe_fields(payload: bytes, indent: str = "    ") -> None:
    """Print each protobuf field, decoding what we can."""
    for number, wire, value in iter_fields(payload):
        if wire == 0:
            log.info("%sfield %-2d varint  %s", indent, number, value)
            continue
        if not isinstance(value, (bytes, bytearray)):
            log.info("%sfield %-2d wire=%d  %r", indent, number, wire, value)
            continue

        raw = bytes(value)
        try:
            guid = decode_root_guid_message(raw)
        except Exception:
            guid = None          # not a guid; it raises on arbitrary bytes
        if guid:
            log.info("%sfield %-2d guid    %s", indent, number, guid)
            continue
        try:
            text = raw.decode("utf-8")
            if text.isprintable():
                shown = text if len(text) <= 60 else f"{text[:57]}... ({len(text)} chars)"
                log.info("%sfield %-2d string  %r", indent, number, shown)
                continue
        except UnicodeDecodeError:
            pass
        log.info("%sfield %-2d bytes   %d bytes: %s", indent, number, len(raw),
                 raw[:24].hex())
        # one level of nesting, which is where device info lives
        try:
            for sub, sub_wire, sub_value in iter_fields(raw):
                if sub_wire == 2 and isinstance(sub_value, (bytes, bytearray)):
                    try:
                        inner = bytes(sub_value).decode("utf-8")
                        if inner.isprintable():
                            log.info("%s    .%-2d %r", indent, sub, inner)
                    except UnicodeDecodeError:
                        pass
        except Exception:
            pass


def parse_challenge(url: str) -> dict:
    """sitekey / action / cdata out of the challenge URL."""
    from urllib.parse import parse_qs, urlparse

    try:
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    except Exception:
        return {}


def describe_error(exc: BaseException) -> None:
    log.info("  type        : %s", type(exc).__name__)
    for attr in ("status_code", "grpc_message", "operation"):
        value = getattr(exc, attr, None)
        if value:
            log.info("  %-11s : %s", attr, value)
    status = getattr(exc, "status", None)
    if status is not None:
        log.info("  status      : %s", getattr(status, "name", status))
    code = getattr(exc, "error_code", None)
    if code:
        log.info("  root code   : %s", code)
    kind = getattr(exc, "payload_kind", None)
    if kind:
        log.info("  payload     : %s", kind)
    challenge = getattr(exc, "challenge_url", None)
    if challenge:
        log.info("  challenge   : %s", challenge)
    detail = getattr(exc, "validation_errors", None)
    if detail:
        log.info("  VALIDATION ERRORS (this names the bad field):")
        for item in detail:
            log.info("      %s", item)
    else:
        log.info("  (no structured detail in the response)")
    headers = getattr(exc, "response_headers", None) or {}
    interesting = {
        k: v for k, v in headers.items()
        if k.lower() in (
            "grpc-status", "grpc-message", "root-exception-bin",
            "turnstile-challenge-url", "content-type",
        )
    }
    if interesting:
        log.info("  response headers:")
        for key, value in interesting.items():
            shown = value if len(str(value)) <= 80 else f"{str(value)[:77]}..."
            log.info("      %-24s %s", key, shown)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true",
                        help="actually send the request (otherwise dry run)")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--email")
    parser.add_argument("--token", default="",
                        help="Turnstile token, if you have one")
    args = parser.parse_args()

    tag = random_tag()
    username = args.username or f"dbg{tag}"
    password = args.password or ("Pw" + secrets.token_urlsafe(16))
    email = args.email or EMAIL_PATTERN.format(tag=tag)

    log.info("=" * 72)
    log.info("SIGNUP REQUEST")
    log.info("=" * 72)
    log.info("  endpoint : %s", PASSWORD_SIGNUP)
    log.info("  username : %s", username)
    log.info("  email    : %s", email)
    log.info("  password : %s (%d chars)", password[:4] + "...", len(password))
    log.info("  token    : %s",
             f"{args.token[:24]}... ({len(args.token)} chars)"
             if args.token else "(none)")

    # ONE device id for every attempt. Root binds the challenge to the
    # request; a fresh device id on the retry reads as a new signup, so the
    # server issues a new challenge and never checks the token you solved.
    device_id = create_desktop_device_guid()
    log.info("  device id: %s  (reused across retries)", device_id)

    transport = GrpcWebTransport()
    auth = AuthClient(transport)

    # Intercept the request so we can see the exact bytes that go out.
    captured = {}
    original = transport.unary

    async def spy(*, endpoint, body, headers, operation):
        captured.update(endpoint=endpoint, body=body, headers=headers,
                        operation=operation)
        payload = unwrap_grpc_web(body) or body
        log.info("")
        log.info("  headers sent:")
        for key, value in headers.items():
            shown = value
            if key.lower() == "authorization" and value:
                shown = value[:16] + "...(redacted)"
            log.info("    %-22s %s", key, shown)
        log.info("")
        log.info("  body: %d bytes framed, %d bytes protobuf",
                 len(body), len(payload))
        describe_fields(payload)
        if not args.send:
            raise SystemExit("\n(dry run -- pass --send to actually send it)")
        return await original(endpoint=endpoint, body=body, headers=headers,
                              operation=operation)

    transport.unary = spy

    token = args.token or None
    session = None
    previous_cdata = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            session = await auth.signup(
                username, password, email, turnstile_token=token,
                device_id=device_id,
            )
            break
        except SystemExit as exc:
            log.info("%s", exc)
            await transport.close()
            return
        except TurnstileRequired as exc:
            log.info("")
            log.info("=" * 72)
            log.info("TURNSTILE CHALLENGE REQUIRED  (attempt %d/%d)",
                     attempt, MAX_ATTEMPTS)
            log.info("=" * 72)
            headers = getattr(exc, "response_headers", None) or {}
            status = headers.get("grpc-status") or headers.get("Grpc-Status")
            if status is not None:
                note = " (FailedPrecondition -- a real challenge)" if str(status) == "9" else \
                       " (NOT 9: the official client only treats 9 as a challenge, so this may be a different error carrying the header)"
                log.info("  grpc-status  : %s%s", status, note)
            challenge = exc.challenge_url or ""
            if challenge:
                log.info("  challenge url:")
                log.info("    %s", challenge)
                params = parse_challenge(challenge)
                for key in ("sitekey", "action", "cdata"):
                    if key in params:
                        value = params[key]
                        shown = value if len(value) <= 50 else value[:47] + "..."
                        log.info("    %-8s = %s", key, shown)
                if previous_cdata and params.get("cdata") != previous_cdata:
                    log.info("")
                    log.info("    NOTE: cdata changed since the last attempt.")
                    log.info("    Each request mints its own binding, and a token")
                    log.info("    solved for the previous one can't satisfy this.")
                previous_cdata = params.get("cdata")
            else:
                log.info("  The server asked for a challenge but didn't name a")
                log.info("  URL. Use Root's signup page:")
                log.info("    %s", SIGNUP_PAGE)
            if token:
                log.info("")
                log.info("  (the token we just sent was rejected -- expired,")
                log.info("   already used, or issued for a different action)")
            log.info("")
            log.info("  Solve it, then paste the token below.")
            log.info("  Blank input gives up.")
            log.info("=" * 72)
            if attempt == MAX_ATTEMPTS:
                log.info("  out of attempts")
                await transport.close()
                return
            asked_at = time.perf_counter()
            try:
                token = input("turnstile_token> ").strip()
            except (EOFError, KeyboardInterrupt):
                token = ""
            solve_seconds = time.perf_counter() - asked_at
            if not token:
                log.info("  cancelled")
                await transport.close()
                return
            log.info("")
            log.info("  solve took %.1fs", solve_seconds)
            if solve_seconds > 30:
                log.info("  (the official client retries on a ~1s backoff, so it")
                log.info("   expects a token within seconds -- if the binding is")
                log.info("   short-lived, a slow solve may be the whole problem)")
            log.info("retrying with a %d-character token...", len(token))
            continue
        except Exception as exc:
            log.info("")
            log.info("=" * 72)
            log.info("FAILED")
            log.info("=" * 72)
            describe_error(exc)
            log.info("")
            log.info("Reading the result:")
            log.info("  * a validation error naming a field -> that field's"
                     " value or format is wrong")
            log.info("  * no structured detail at all       -> the request"
                     " didn't parse; check the field numbers above")
            await transport.close()
            return

    if session is None:
        await transport.close()
        return

    log.info("")
    log.info("=" * 72)
    log.info("SUCCESS")
    log.info("=" * 72)
    log.info("  user id  : %s", getattr(session, "user_id", "?"))
    log.info("  token    : %s...", str(getattr(session, "token", ""))[:24])
    log.info("  hub      : %s", getattr(session, "hub_url", "?"))
    log.info("\n  Account created -- record these credentials somewhere safe.")
    await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
