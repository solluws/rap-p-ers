"""Root's field format rules, checked before a request goes out.

Root validates a lot of string fields and answers a violation with a generic
INVALID_ARGUMENT plus a ``RequestValidatorList`` naming the field. That is
diagnosable, but it costs a round trip to learn something the client already
knew, so checking them here saves a round trip.

Each rule lives here rather than next to whichever service happens to use it,
so there is one place to look when Root rejects a value and one place to fix
when a rule turns out to be wrong.

Every function raises :class:`ValueError` with the actual rule in the message,
never a bare "invalid".
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# colours -- PictureHex, role ColorHex
# --------------------------------------------------------------------------

#: Root validates PictureHex with an ExactLengthValidator of 7: ``#rrggbb``,
#: leading hash included. Established live -- "must be 7 characters in length.
#: You entered 6."
PICTURE_HEX_LENGTH = 7

#: Used when a caller does not pick a colour. Communities cannot be created
#: without one (NotEmptyValidator), so there has to be a usable default.
DEFAULT_PICTURE_HEX = "#3f51b5"

_HEX_DIGITS = frozenset("0123456789abcdef")


def normalize_hex_colour(value: str, *, field_name: str = "picture_hex") -> str:
    """Validate and normalise a Root hex colour to ``#rrggbb``.

    Accepts ``"3f51b5"``, ``"#3F51B5"`` and surrounding whitespace, and always
    returns the 7-character form with the leading hash. Eight-digit (alpha)
    values are rejected: Root wants exactly seven characters.

    Applies to community ``picture_hex`` and role ``color_hex`` alike -- the
    shared convention was confirmed live for both.
    """
    if value is None:
        raise ValueError(f"{field_name} must not be None")
    digits = str(value).strip().lstrip("#").lower()
    if not digits:
        raise ValueError(
            f"{field_name} must not be empty -- Root rejects the request "
            f"without one. Pass a colour, e.g. {DEFAULT_PICTURE_HEX!r}."
        )
    if len(digits) != 6 or any(c not in _HEX_DIGITS for c in digits):
        raise ValueError(
            f"{field_name} must be 6 hex digits (Root wants the "
            f"{PICTURE_HEX_LENGTH}-character '#rrggbb' form), got {value!r}"
        )
    return "#" + digits


# --------------------------------------------------------------------------
# usernames and nicknames
# --------------------------------------------------------------------------

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 20

#: The rule as Root states it in its own sign-up form:
#: "Must be 3-20 characters using only letters, numbers, underscores and
#: periods. Underscores and periods can't be at the start or end, or next to
#: each other."
USERNAME_RULE = (
    "must be 3-20 characters using only letters, numbers, underscores and "
    "periods; underscores and periods cannot be at the start or end, or next "
    "to each other"
)

_USERNAME_CHARSET = re.compile(r"^[A-Za-z0-9_.]+$")
_ADJACENT_PUNCTUATION = re.compile(r"[_.]{2}")


def validate_username(value: str, *, field_name: str = "username") -> str:
    """Check a username against Root's documented rule; return it unchanged.

    Root's own wording is in :data:`USERNAME_RULE`. Checking here turns a
    round trip and a generic INVALID_ARGUMENT into an immediate error that
    says which part of the rule was broken.

    Note this deliberately does not "fix up" the value. Silently rewriting
    somebody's chosen username would be worse than refusing it.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")

    if len(value) < USERNAME_MIN_LENGTH or len(value) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"{field_name} {USERNAME_RULE}; got {len(value)} characters "
            f"({value!r})"
        )
    if not _USERNAME_CHARSET.match(value):
        bad = sorted({c for c in value if not re.match(r"[A-Za-z0-9_.]", c)})
        raise ValueError(
            f"{field_name} {USERNAME_RULE}; {value!r} contains "
            f"{', '.join(repr(c) for c in bad)}"
        )
    if value[0] in "_." or value[-1] in "_.":
        raise ValueError(
            f"{field_name} {USERNAME_RULE}; {value!r} starts or ends with an "
            f"underscore or period"
        )
    if _ADJACENT_PUNCTUATION.search(value):
        raise ValueError(
            f"{field_name} {USERNAME_RULE}; {value!r} has two underscores or "
            f"periods next to each other"
        )
    return value


def validate_nickname(value: str, *, field_name: str = "nickname") -> str:
    """Check a community nickname against Root's RegularExpressionValidator.

    Root gives no pattern for Nickname, so this is the username rule, which
    the evidence supports rather than proves. A nickname of letters and
    digits is accepted, one containing a space or a hyphen is rejected, and
    that matches the username charset exactly -- letters, digits,
    underscores, periods and nothing else.

    Underscores and periods in nicknames are inferred from the shared charset,
    not separately confirmed. If Root turns out to be looser here, loosen this
    rather than working around it at the call site.
    """
    return validate_username(value, field_name=field_name)


__all__ = [
    "PICTURE_HEX_LENGTH",
    "DEFAULT_PICTURE_HEX",
    "normalize_hex_colour",
    "USERNAME_MIN_LENGTH",
    "USERNAME_MAX_LENGTH",
    "USERNAME_RULE",
    "validate_username",
    "validate_nickname",
]
