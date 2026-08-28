"""Run the live suite with credentials loaded from ``tokens.txt``.

The live fixtures read ``ROOT_TOKEN`` and ``ROOT_TOKEN2`` from the
environment. Putting them on a command line works and is a bad habit: the
argv of a running process is readable by other processes, it lands in shell
history, and it ends up quoted in whatever transcript or log is capturing the
session. ``tokens.txt`` is already the repo's convention for credentials and
is already gitignored, so this reads from there instead and passes them to
pytest through the environment only.

Put this in ``tokens.txt``, in the project root or beside this script::

    root_token=<first burner account>
    root_token2=<second burner account>

Then::

    python devscripts/livetest.py -m "live or live2" -v
    python devscripts/livetest.py -m live tests/test_live_files.py -v
    python devscripts/livetest.py -m "live or live2" --timing --timing-json=t.json

Every argument is forwarded to pytest untouched.

Values already in the environment win, so an existing ``ROOT_TOKEN`` export
still works and this changes nothing for anyone using it that way.

Nothing here ever prints a token. It prints a fingerprint -- length and last
four characters -- which is enough to tell "loaded" from "not loaded" and
"two different accounts" from "the same one twice" without putting the secret
anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import read_token, tokens_file  # noqa: E402


def fingerprint(value: str) -> str:
    """Enough to identify a token, not enough to use it."""
    if not value:
        return "missing"
    return f"{len(value)} chars, ...{value[-4:]}"


def main() -> int:
    env = dict(os.environ)

    loaded = {}
    for variable, key in (("ROOT_TOKEN", "root_token"),
                          ("ROOT_TOKEN2", "root_token2")):
        existing = env.get(variable, "").strip()
        if existing:
            loaded[variable] = (existing, "environment")
            continue
        value = read_token(key)
        if value:
            env[variable] = value
            loaded[variable] = (value, str(tokens_file()))

    print("credentials:")
    for variable in ("ROOT_TOKEN", "ROOT_TOKEN2"):
        if variable in loaded:
            value, source = loaded[variable]
            print(f"  {variable:12s} {fingerprint(value):24s} from {source}")
        else:
            print(f"  {variable:12s} missing")

    if "ROOT_TOKEN" not in loaded:
        print(
            f"\nNo ROOT_TOKEN. Create {tokens_file()} with:\n"
            "    root_token=...\n"
            "    root_token2=...\n"
            "(that filename is already in .gitignore)"
        )
        return 2

    first = loaded["ROOT_TOKEN"][0]
    second = loaded.get("ROOT_TOKEN2", ("", ""))[0]
    if second and first == second:
        # conftest's distinct_accounts fixture also catches this, but only
        # after a login. Saying so here costs nothing and saves the round trip.
        print(
            "\nBoth tokens are identical. Two-account tests would pass "
            "trivially and prove nothing; use two different accounts."
        )
        return 2
    if not second:
        print("\nNo ROOT_TOKEN2 -- live2 tests will skip.")

    args = sys.argv[1:] or ["-m", "live or live2", "-v"]
    print(f"\npytest {' '.join(args)}\n", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "pytest", *args],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
