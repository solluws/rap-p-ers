# Creating test accounts

Root gates signup behind a **Cloudflare Turnstile** challenge. That's the
platform's control over automated signup, and this library doesn't try to get
around it — you solve the challenge and hand the token to `AccountFactory`.

Tokens are **single-use and short-lived** (a few minutes), so it's one
challenge per account. That's a natural brake on volume, which is the point.

## Before you start

Get the platform's agreement, and follow whatever conditions come with it. In
practice that usually means:

- **One recognisable email domain/pattern**, so every account you create can be
  found and removed in a single sweep if it's ever needed.
- **Never publish the tokens or passwords.** A token is full account access.
- **Keep the count to what you actually need.** Test accounts for a test
  harness is a very different thing from a pile of accounts.

## How signup actually works on the wire

Worth writing down, because getting it wrong produces a very misleading
failure: the server keeps issuing challenges and it looks like your tokens are
being rejected, when in fact it never received one.

`connect.ConnectService/PasswordSignUp` takes:

| field | contents |
|---|---|
| 1 | username |
| 2 | password |
| **3** | **Turnstile token** |
| 4 | device description (hostname, OS, app version) |
| 5 | device id (guid) |
| 6 | email |
| 7 | access token — a *different* thing, not the captcha |

Two rules that matter:

**The Turnstile token goes in field 3.** Field 7 is `AccessToken`. Put the
captcha token there and the server sees a signup with no token at all, so it
issues another challenge — indistinguishable from rejection.

**Reuse the device id across the retry.** Root binds the challenge to the
request it was issued for. Generate the device id once, use it for the
unauthenticated attempt *and* the retry that carries the token. A fresh id on
the retry reads as a new signup, and you get a new `cdata` instead of a
verdict.

The flow is therefore:

1. `signup(...)` with no token → `TurnstileRequired`, carrying the challenge URL
2. Solve that challenge → token
3. `signup(...)` again, **same device id**, token in field 3 → session

`AccountFactory` handles the device id for you.

## Getting a Turnstile token

There is no signup page to open ahead of time. As the section above says, the
challenge doesn't exist until you ask: Root mints one per request and names it
in a URL, so the only token worth having is the one solved at *that* URL.

The real client works exactly this way.
`ConnectGrpcClient.ExecuteWithTurnstileRetryAsync` sends the call, catches the
`FAILED_PRECONDITION`, reads the `turnstile-challenge-url` response header, and
hands **that URL** to `ITurnstileTokenProvider.GetTokenAsync`; the desktop
implementation, `AvaloniaTurnstileTokenProvider`, opens the URL it was given
and appends only `&bridge=desktop`. Nothing in it goes looking for a signup
page of its own.

So the flow is:

```python
challenge_url = await factory.create_return_turnstile(username="bob")
print(challenge_url)                          # the URL Root just minted
token = solve_it_yourself(challenge_url)      # a person, in a browser
account = await factory.create_with_turnstile(
    turnstile_token=token, challenge=challenge_url,
)
```

`create(turnstile_token=None)` reaches the same place the blunt way: it raises
`TurnstileRequired`, and `exc.challenge_url` is that header's value.

Someone still has to sit and solve it. This library won't do that part, and
the split into two calls is what keeps that step yours and visible.
[two-step-signup.md](two-step-signup.md) walks the whole thing.

## Creating accounts

```python
from rootpy import AccountFactory

factory = AccountFactory(
    email_pattern="hello-{tag}@solluw.com",   # must contain {tag}
    username_prefix="test",                   # optional, keeps them obvious
)

account = await factory.create(
    turnstile_token=TOKEN,
    note="fullrun test account",
)

print(account.username, account.email, account.user_id)
factory.save("accounts.json")                 # keep this file private
```

`create(keep_client=True)` returns `(account, client)` with a live, logged-in
client if you want to use it immediately.

## Email verification

New accounts start unverified. The code is emailed to the address you chose --
with `hello-{tag}@solluw.com` that's your own mail server, so reading it is
your side of the job (IMAP, a catch-all webhook, whatever you already run).

Two ways to do it:

```python
# 1. manually, when you have the code
#    signup already emailed it -- don't ask for another (see below)
await factory.verify(account, "123456")

# 2. or hand it a reader and let it wait
async def read_code(email: str):
    """Return the verification code for this address, or None if it hasn't
    arrived yet."""
    return await my_mailbox.latest_code(email)

account = await factory.create_and_verify(
    turnstile_token=TOKEN,
    code_provider=read_code,
    timeout=120,
)
print(account.verified)
```

`code_provider` is deliberately yours — the mail side is your infrastructure
and this library shouldn't guess at it. If no code arrives before `timeout`,
you still get the account back (unverified, and listed by
`factory.unverified()`), so nothing is lost — call `factory.verify(account, code)`
whenever the mail turns up.

`create_and_verify` does **not** ask for a second code. It creates the account
and then polls `code_provider` every `poll_interval` seconds (5 by default) for
the code signup already sent, until `timeout` (120s by default). The keyword
`resend: bool = False` is there for the case where that first email genuinely
never arrived — set it and the call does one `send_verification()` first, with
no token, which on a fresh account normally just raises `TurnstileRequired` and
strands credentials that have already been minted. Leave it alone unless you
mean it.

## Verification: don't ask for a second code

**Creating an account already emails a verification code.** You only need to
read it and submit it:

```python
code = await read_from_your_mailbox(account.email)   # signup sent it
await factory.verify(account, code)
```

`send_verification()` exists for genuinely resending, but calling it triggers
a *separate* Turnstile challenge — `action=resend_verification`, which is the
literal string the client passes in
`UserInfoRepository.ResendEmailVerificationAsync` — and solving the signup one
doesn't satisfy it. So on a fresh account, skip it; polling for the code that
already arrived avoids a challenge entirely. `create_and_verify` defaults to
`resend=False` for the same reason.

If you do need a resend, pass a token for that challenge:

```python
await factory.send_verification(account, turnstile_token=token)
```

## Keeping track

```python
factory.emails()                              # every address you've used
factory.save("accounts.json")                 # credentials (private!)
factory.save("accounts.public.json", include_secrets=False)   # shareable
factory.load("accounts.json")                 # read them back
```

`accounts.json` holds real passwords and tokens. It's in `.gitignore` — leave
it that way, and set sensible file permissions if the machine is shared.

## Cleaning up

If you're asked to remove them, `factory.emails()` gives the exact list. Deleting
an account you own is a normal account operation; the email pattern is what
makes the whole set findable.
