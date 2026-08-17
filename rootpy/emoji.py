from __future__ import annotations

import re
from typing import Optional, Tuple

try:
    import emoji as _emoji
except ImportError:
    _emoji = None


_ROOT_EMOJI_MENTION_RE = re.compile(
    r"^\[:(?P<name>[^\]:]+):\]"
    r"\(root://emoji/(?P<shortcode>[^)]+)\)$"
)

_SHORTCODE_RE = re.compile(r"^:[A-Za-z0-9_+\-]+:$")
_BARE_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_+\-]+$")

_UNICODE_FALLBACK = {
    "😀": ":grinning:",
    "😁": ":grin:",
    "😂": ":joy:",
    "🤣": ":rofl:",
    "😊": ":blush:",
    "😍": ":heart_eyes:",
    "😘": ":kissing_heart:",
    "😭": ":sob:",
    "😢": ":cry:",
    "😎": ":sunglasses:",
    "👍": ":thumbsup:",
    "👎": ":thumbsdown:",
    "❤️": ":heart:",
    "❤": ":heart:",
    "🔥": ":fire:",
    "🎉": ":tada:",
    "💀": ":skull:",
    "🤡": ":clown:",
    "🤔": ":thinking:",
    "🙏": ":pray:",
}


def parse_emoji_mention(value: str) -> Optional[Tuple[str, str]]:
    """Parse Root's rich emoji mention.

    Examples:
        [:Lebron:](root://emoji/ADAEfhAHgCOOK1LQtg3fJg)
            -> ("Lebron", "ADAEfhAHgCOOK1LQtg3fJg")

        [:grinning:](root://emoji/:grinning:)
            -> ("grinning", ":grinning:")

    The URI target is the value Root uses as the reaction Shortcode.
    """
    if not isinstance(value, str):
        return None

    match = _ROOT_EMOJI_MENTION_RE.fullmatch(value.strip())
    if match is None:
        return None

    return match.group("name"), match.group("shortcode")


def normalize_reaction(value: str) -> str:
    """Normalize a reaction into the string sent as Root's Shortcode.

    Supported forms:
        Root custom mention:
            [:Lebron:](root://emoji/ADAEfhAHgCOOK1LQtg3fJg)
            -> ADAEfhAHgCOOK1LQtg3fJg

        Root standard mention:
            [:grinning:](root://emoji/:grinning:)
            -> :grinning:

        Standard shortcode:
            :grin:
            -> :grin:

        Bare standard shortcode:
            grin
            -> :grin:

        Unicode:
            😁
            -> :grin:
    """
    if not isinstance(value, str):
        raise TypeError("reaction must be a string")

    value = value.strip()
    if not value:
        raise ValueError("reaction cannot be empty")

    mention = parse_emoji_mention(value)
    if mention is not None:
        _name, shortcode = mention
        return shortcode

    if _SHORTCODE_RE.fullmatch(value):
        return value.lower()

    if _BARE_SHORTCODE_RE.fullmatch(value):
        return ":" + value.lower() + ":"

    fallback = _UNICODE_FALLBACK.get(value)
    if fallback is not None:
        return fallback

    if _emoji is not None:
        shortcode = _emoji.demojize(value, language="alias")
        if _SHORTCODE_RE.fullmatch(shortcode):
            return shortcode.lower()

    raise ValueError(
        "reaction must be a Root emoji mention, a single emoji, "
        "or a shortcode such as :grin:"
    )
