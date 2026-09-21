"""Structured protocol registry: messages, enums and service definitions.

Root's gRPC surface as data: 842 message definitions with their field numbers
and types, the platform enums, and the service/method table. Everything in
rootpy that encodes or decodes a request without a hand-written model reads
from here.

The data lives in ``rootpy/data/*.json``; this module exposes it as lazily-loaded
mappings so importing rootpy does not pay for 842 message definitions that a
given program probably will not touch.

Each mapping is a drop-in for the dict it replaced -- subscript, ``in``,
``.get()``, ``.items()``, ``len()`` and iteration all behave identically.
"""

from __future__ import annotations

from ._registry_loader import DerivedRegistry, LazyRegistry

#: Fully-qualified message name -> {name, namespace, fields}.
MESSAGES = LazyRegistry("messages.json")

#: Enum name -> {member: value}.
ENUMS = LazyRegistry("enums.json")

#: Service name -> {method: {request, response, method_type, endpoint, ...}}.
SERVICES = LazyRegistry("services.json")


def _build_simple_messages():
    """Short message name -> [fully-qualified names that end with it].

    Derived rather than stored: it is exactly an index over ``MESSAGES`` keys,
    and keeping a second copy on disk only created a way for the two to drift.
    Nothing is ambiguous in this data set: every short name resolves to
    exactly one fully-qualified name. That holds at the proto level too --
    the descriptors declare six packages (``connect``, ``root``, ``root.app``, ``root.app.messaging``,
    ``rootapp.client.domain.cache``, ``validate``) and no two of them reuse a
    message name. The signup messages are split across packages but not named
    alike: ``connect.PasswordSignUpRequest`` vs ``root.UserSignUpRequest``.

    The value is still a list because collision is possible in principle --
    nothing in protobuf stops two packages from declaring the same message
    name. Keeping the list keeps ``StructuredProtoCodec.resolve_message``
    honest: it returns a descriptor only when a short name maps to exactly one
    candidate, or when its ``namespace_hint`` picks one out.

    A test pins the current state, so a future data set that does collide
    fails loudly instead of silently resolving to whichever message happened
    to be indexed first.
    """
    index = {}
    for full_name in MESSAGES:
        index.setdefault(full_name.rsplit(".", 1)[-1], []).append(full_name)
    return index


#: Short name -> [fully-qualified names]. Computed on first use.
SIMPLE_MESSAGES = DerivedRegistry(_build_simple_messages)

__all__ = ["MESSAGES", "ENUMS", "SERVICES", "SIMPLE_MESSAGES"]
