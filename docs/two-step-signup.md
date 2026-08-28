# Two-step account creation

Signup is a three-legged process and the legs are easy to get wrong. This
splits it so you control the middle part -- solving the challenge -- however
you like, without having to know the protocol details.

```python
challenge_url = await factory.create_return_turnstile(username="bob")
token         = my_own_solver(challenge_url)          # your code, your call
account       = await factory.create_with_turnstile(
                    turnstile_token=token, challenge=challenge_url)
```

---

## Why it's three steps and not one

Root won't create an account without a Cloudflare Turnstile token. But you
can't get a token first, because the challenge doesn't exist until you ask:

1. You send a signup with **no token**.
2. Root refuses, and in refusing it **mints a challenge** tied to that exact
   request, handing you a URL.
3. You solve *that* challenge and send the signup **again**, same details,
   now carrying the token.

The second signup isn't a retry in the loose sense -- it has to look like the
*same* signup, or Root treats it as a new one and mints a new challenge.

---

## The two rules that break everything

Almost every failure here is one of these. They're worth reading twice.

### 1. The token goes in field 3

`connect.ConnectService/PasswordSignUp` has both:

| field | contents |
|---|---|
| 3 | **Turnstile token** |
| 7 | Access token — an unrelated thing |

Put the captcha token in field 7 and the server receives a signup with **no
token at all**, so it issues another challenge. From the outside this is
indistinguishable from "my token was rejected", and you can burn a lot of
challenges chasing it.

The SDK handles this. It matters if you ever build the request by hand.

### 2. The device id must be identical across both calls

Field 5 is a device id, and it is *optional* -- the reference desktop client
never sends it. `ConnectGrpcClient.PasswordSignUpAsync` sets `DeviceId` only
when `deviceId.HasValue()`, and its one production caller,
`RootService.SignUpAsync`, passes only the first six arguments, so the
optional seventh parameter keeps
its `default(DeviceGuid)` value, `HasValue()` is false, and the field never
reaches the wire. rootpy takes the other path: it puts a device id in field 5 of
every signup, minting a fresh desktop guid when you don't pass one.

So this is a rule for *you*, not for the desktop client. Root binds the
challenge to the request that produced it, and the device id -- when you send
one -- is part of that request. It is the field that catches people out,
because it's the one nobody typed: generate it per attempt and every attempt is
a different signup. The desktop client sidesteps the whole problem by reusing
the *same* request object for the retry and mutating only the turnstile token.

```
attempt 1   device A   -> challenge with cdata=AAA
attempt 2   device B   -> NEW challenge with cdata=BBB   (token never checked)
attempt 2   device A   -> validates the token against cdata=AAA   ✓
```

Generate the device id **once** and use it for both calls. Same for username,
password and email -- change any of them and it's a different signup.

The telltale sign you've got this wrong: the `cdata` in the challenge URL
changes on every attempt, and no token is ever accepted.

---

## Step 1 — get the challenge

```python
from rootpy import AccountFactory

factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")

challenge_url = await factory.create_return_turnstile(
    username="bob",            # optional -- generated if omitted
    password=None,             # optional -- generated if omitted
    device_id=None,            # optional -- generated if omitted
)
```

Returns a **`TurnstileChallenge`**, which *is* a string:

```python
print(challenge_url)
# https://infrastructure.rootapp.com/turnstile-challenge-2.2.4.min.html
#   ?sitekey=0x4AAAAAACO-GyLLPADCnCJu&action=password_signup&cdata=AY__g2rl...
```

So anything expecting a URL string just works. It also carries the details of
the attempt, which is what makes step 3 safe:

```python
challenge_url.username      # 'bob'
challenge_url.password      # generated
challenge_url.email         # 'hello-a1b2c3d4@solluw.com'
challenge_url.device_id     # the guid that must be reused
challenge_url.context()     # all of the above as a dict
challenge_url.describe()    # "challenge for 'bob' <...> | action=... | cdata=..."
```

**If no challenge is required** (it can happen), the account is created right
there and `AlreadyCreatedError` is raised with the result attached:

```python
from rootpy import AlreadyCreatedError

try:
    challenge_url = await factory.create_return_turnstile(username="bob")
except AlreadyCreatedError as done:
    account = done.account        # already made; nothing to solve
```

---

## Step 2 — solve it (your code)

This part is yours. The SDK doesn't care how it happens, as long as a human
solves the challenge:

```python
def my_own_solver(challenge_url: str) -> str:
    webbrowser.open(challenge_url)
    return input("paste the token> ").strip()

token = my_own_solver(challenge_url)
```

Solve the URL you were handed, not a signup page you found yourself — the
challenge is bound to this attempt. The desktop client does the same thing and
no more: `AvaloniaTurnstileTokenProvider.GetTokenAsync` opens exactly the URL
`ConnectGrpcClient` read out of the `turnstile-challenge-url` header, appending
only `&bridge=desktop`, and collects the token over that bridge.

Without the bridge, the token is the hidden input Turnstile writes into the
same page:

```js
document.querySelector('[name="cf-turnstile-response"]').value
```

Tokens are **single-use and short-lived** (a few minutes). One challenge, one
token, one account.

---

## Step 3 — finish the signup

```python
account = await factory.create_with_turnstile(
    turnstile_token=token,
    challenge=challenge_url,       # supplies username/password/email/device_id
    note="test account",           # optional
)

print(account.username, account.email, account.user_id)
```

Add `keep_client=True` to get a live logged-in client back too:

```python
account, client = await factory.create_with_turnstile(
    turnstile_token=token, challenge=challenge_url, keep_client=True,
)
await client.messages.send(channel_id, "hello", community_id=community_id)
await client.close()
```

### Doing it without the challenge object

If you're storing state yourself (a queue, a database, across processes), pass
the four values explicitly:

```python
account = await factory.create_with_turnstile(
    turnstile_token=token,
    username=saved["username"],
    password=saved["password"],
    email=saved["email"],
    device_id=saved["device_id"],     # ← the one people forget
)
```

Miss any of them and you get a `ValueError` naming what's absent, rather than
a mystery challenge loop.

---

## Step 4 — verify, without solving a second challenge

Signup already emailed a verification code. Reading it and submitting it is
all that's left:

```python
code = await read_from_your_mailbox(account.email)   # signup sent it
await factory.verify(account, code)
```

Do **not** call `send_verification()` here. Root gates the resend behind its
own Turnstile challenge — `action=resend_verification`, the literal string the
client passes in `UserInfoRepository.ResendEmailVerificationAsync` — and the
token you solved for the signup doesn't satisfy it. So a reflexive resend puts
you back at step 2, solving a fresh challenge for a code that was already in
the mailbox.

If the first email genuinely never arrived, solve that second challenge and
pass its token:

```python
await factory.send_verification(account, turnstile_token=resend_token)
```

`create_and_verify()` follows the same rule: `resend` defaults to `False`, so
it creates the account and polls your `code_provider` for the code signup
already sent. See [accounts.md](accounts.md#email-verification).

---

## Full example

```python
import asyncio
import webbrowser
from rootpy import AccountFactory, AlreadyCreatedError

async def make_account(username: str):
    factory = AccountFactory(email_pattern="hello-{tag}@solluw.com")

    # 1. ask for the challenge
    try:
        challenge_url = await factory.create_return_turnstile(username=username)
    except AlreadyCreatedError as done:
        return done.account                      # no challenge needed

    # 2. solve it however you like
    print(challenge_url.describe())
    webbrowser.open(str(challenge_url))
    token = input("paste the token> ").strip()
    if not token:
        return None

    # 3. finish
    account = await factory.create_with_turnstile(
        turnstile_token=token, challenge=challenge_url,
    )

    # 4. verify -- signup already sent the code, so no resend and no
    #    second challenge
    code = input("code from the email> ").strip()
    if code:
        await factory.verify(account, code)

    factory.save("accounts.json")                 # private!
    return account

asyncio.run(make_account("bob"))
```

---

## When it goes wrong

| what you see | what it means |
|---|---|
| `cdata` differs between attempts | the device id (or username/email) changed — reuse the challenge object |
| token accepted nowhere, no errors | the token is landing in field 7; use the SDK's signup, not a hand-built one |
| `TurnstileRequired` again after sending a token | either the above, or the token expired before you sent it |
| `ValueError: missing device_id...` | you called step 3 without the context from step 1 |
| `AlreadyCreatedError` | not an error — the account exists, `.account` has it |

`devscripts/signupdebug.py` prints the whole exchange field by field, including the
`sitekey`/`action`/`cdata` of each challenge and whether `cdata` changed
between attempts. It's the fastest way to see which of these you're hitting.

---

## Bookkeeping

Every account the factory makes is recorded:

```python
factory.accounts          # [CreatedAccount, ...]
factory.emails()          # every address used -- hand this over if asked to prune
factory.unverified()      # created but not yet verified
factory.save("accounts.json")                        # includes passwords/tokens
factory.save("public.json", include_secrets=False)   # redacted, shareable
factory.load("accounts.json")
```

`accounts.json` holds real credentials. It's in `.gitignore`; keep it that way,
and set sensible file permissions if anyone else uses the machine.
