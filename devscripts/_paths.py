"""Shared path helper for the dev scripts.

These moved into devscripts/, so credentials may live either beside a script
or in the project root above it. Check both rather than making you keep two
copies.

    from _paths import tokens_file
    path = tokens_file()
"""

from pathlib import Path


def _find(name: str) -> Path:
    here = Path(__file__).with_name(name)
    if here.exists():
        return here
    above = Path(__file__).resolve().parent.parent / name
    return above if above.exists() else here


def tokens_file() -> Path:
    """tokens.txt, beside the script or in the project root."""
    return _find("tokens.txt")


def accounts_file() -> Path:
    return _find("accounts.json")


def provisioned_file() -> Path:
    """provisioned.json, the ledger provisioner/provision.py writes."""
    return _find("provisioned.json")


def read_token(name: str = "token") -> str:
    """The named credential from tokens.txt, or ''."""
    path = tokens_file()
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip().lower() == name and value.strip():
            return value.strip()
    return ""
