"""Uniform readers for structured API responses.

Root's structured API hands a payload back in more than one shape: an
``AttrDict`` (attribute *and* key access), a plain ``dict`` once something has
converted it, a decoded message object, or ``None`` when a field is absent.
Every layer that read one of these grew its own little accessor -- ``features``
had two ``_field`` definitions, the live tests had ``_get``/``_shape``, and two
more sites inlined ``getattr(raw, "notifications", raw)`` -- and they drifted.

This is the one place that knows how to read a field off whatever shape a
response arrived in. Nothing here makes a request or imports anything heavy; it
is pure shape handling, safe to import from anywhere (including tests) without a
cycle.

The list-RPC envelope has a name-agnostic unwrapper too --
``rootpy.domain_managers.unwrap_list`` -- for callers that do not know which
field holds the items. Use :func:`items` when you do know the name and want it
read exactly.
"""

from __future__ import annotations

from typing import Any

__all__ = ["field", "first", "as_sequence", "items", "shape"]


def field(payload: Any, name: str) -> Any:
    """Read ``name`` off a structured payload, whatever shape it arrived in.

    Handles a ``dict`` (``.get``), an object or ``AttrDict`` (``getattr``), and
    ``None``. Returns ``None`` when the field is absent -- it never raises,
    because a response's shape is the server's business and a reader should
    degrade rather than crash when a field it expected is missing.
    """
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def first(payload: Any, *names: str) -> Any:
    """The first *truthy* field among ``names``, or ``None``.

    For a record that carries the same value under more than one spelling --
    ``id`` on the record itself or nested under ``directory``/``file`` -- take
    whichever is present and non-empty.
    """
    for name in names:
        value = field(payload, name)
        if value:
            return value
    return None


def as_sequence(value: Any) -> list:
    """Normalise a repeated field to a list (``None`` and scalars included)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def items(payload: Any, name: str) -> list:
    """The repeated field ``name`` off a list-RPC envelope, as a list.

    Root's list RPCs wrap their single repeated field in an envelope --
    ``NotificationListResponse.notifications``,
    ``CommunityRoleListResponse.community_roles`` -- and iterating the envelope
    itself yields field *names*, not records (a trap documented in HANDOFF).
    This reads the named field out and normalises it.

    When ``payload`` has no such field, a ``list``/``tuple`` payload is taken to
    be the sequence already (some helpers hand back the unwrapped list) and any
    other payload reads as empty. Root omits an empty repeated field entirely,
    so "no such field" is overwhelmingly "no rows" -- and a bare envelope must
    never be normalised into one phantom row.
    """
    if payload is None:
        return []
    inner = field(payload, name)
    if inner is None:
        # Only a payload that *is* the sequence may stand in for the field.
        #
        # Root omits an empty repeated field from the response entirely, so an
        # empty ``NotificationListResponse`` arrives as a bare envelope with no
        # ``notifications`` attribute at all. Falling back to the envelope
        # unconditionally then handed it to ``as_sequence``, which wraps a
        # non-sequence in a list -- so *zero* rows read back as ``[<envelope>]``
        # and ``len(...) == 1``. An empty inbox could never compare equal to
        # zero, which is what failed
        # ``TestNotificationLifecycle::test_delete_all_empties_the_peer_inbox``
        # against a peer whose inbox delete_all had in fact emptied.
        if isinstance(payload, (list, tuple)):
            return list(payload)
        return []
    return as_sequence(inner)


def shape(payload: Any) -> str:
    """A readable one-line field listing, so a surprising shape is legible.

    ``{a, b, c}`` for a dict, ``Type(a, b, c)`` for an object. Used in
    diagnostics where "what did the server actually send" has to survive a
    single run instead of costing another one.
    """
    if payload is None:
        return "None"
    if isinstance(payload, dict):
        return "{" + ", ".join(sorted(map(str, payload))) + "}"
    names = sorted(n for n in dir(payload) if not n.startswith("_"))
    return type(payload).__name__ + "(" + ", ".join(names) + ")"
