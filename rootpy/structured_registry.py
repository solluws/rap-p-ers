"""Structured protocol registry: messages, enums and service definitions.

Parsed out of the decompiled client's per-message ``.cs`` class files under
``sources/RootSrcV2`` (and ``sources/RootSrc``) -- *not* out of its
``*Reflection.cs`` protobuf descriptors, which is what this docstring used to
claim. The stored data disproves it: a ``FileDescriptor`` cannot produce
C#-level artefacts like the types ``RepeatedField<AccessRuleResponse>`` and
``RootGuid?``, the synthesised oneof discriminators (``MetadataOneofCase`` on
``BillingUserPaymentItem.MetadataCase``), or the 35 fields that carry no field
number at all. Every entry records the class file it came from in ``source``.

``source`` is written ``<assembly>/<namespace-as-path>/<Class>.cs``, which is
not where the file is: all 831 of them fail to resolve as written, in both
trees, because the decompiled trees give each namespace one flat dotted
directory instead of a directory per segment. Rejoin the namespace segments
with dots and all 831 resolve::

    source  RootApp.WebApi.Shared.Entities/RootApp/WebApi/Shared/Grpc/Requests/MessageCreateRequest.cs
    on disk sources/RootSrcV2/RootApp.WebApi.Shared.Entities/RootApp.WebApi.Shared.Grpc.Requests/MessageCreateRequest.cs

Nothing in the package reads ``source`` -- it is provenance for humans, so the
mismatch costs no behaviour.

The registries are produced by external tooling -- there is no generator script
in this repo -- and were last regenerated for client 0.9.128.

The data lives in ``rootpy/data/*.json``; this module exposes it as lazily-loaded
mappings so importing rootpy does not pay for 831 message definitions that a
given program probably will not touch.

Each mapping is a drop-in for the dict it replaced -- subscript, ``in``,
``.get()``, ``.items()``, ``len()`` and iteration all behave identically.
"""

from __future__ import annotations

from ._registry_loader import DerivedRegistry, LazyRegistry

#: Fully-qualified message name -> {name, namespace, fields, source}.
MESSAGES = LazyRegistry("messages.json")

#: Enum name -> {member: value}.
ENUMS = LazyRegistry("enums.json")

#: Service name -> {method: {request, response, method_type, endpoint, ...}}.
SERVICES = LazyRegistry("services.json")


def _build_simple_messages():
    """Short message name -> [fully-qualified names that end with it].

    Derived rather than stored: it is exactly an index over ``MESSAGES`` keys,
    and keeping a second copy on disk only created a way for the two to drift.
    Nothing is ambiguous in this dump: all 831 keys sit under the single C#
    namespace root ``RootApp.*``, and every short name resolves to exactly one
    of them. That holds at the proto level too -- the 98 descriptors declare
    six packages (``connect``, ``root``, ``root.app``, ``root.app.messaging``,
    ``rootapp.client.domain.cache``, ``validate``) and no two of them reuse a
    message name. The signup messages are split across packages but not named
    alike: ``connect.PasswordSignUpRequest`` vs ``root.UserSignUpRequest``.

    The value is still a list because collision is possible in principle --
    nothing in protobuf stops two packages from declaring the same message
    name, and this data is a snapshot of one client build. Keeping the list
    keeps ``StructuredProtoCodec.resolve_message`` honest: it returns a
    descriptor only when a short name maps to exactly one candidate, or when
    its ``namespace_hint`` (the C# namespace, not the proto package) picks one
    out. ``test_short_names_are_unique_in_this_dataset`` in
    ``tests/test_untested_surface.py`` pins the current state, so a future dump
    that does collide fails a test instead of silently resolving to whichever
    message happened to be indexed first.
    """
    index = {}
    for full_name in MESSAGES:
        index.setdefault(full_name.rsplit(".", 1)[-1], []).append(full_name)
    return index


#: Short name -> [fully-qualified names]. Computed on first use.
SIMPLE_MESSAGES = DerivedRegistry(_build_simple_messages)

__all__ = ["MESSAGES", "ENUMS", "SERVICES", "SIMPLE_MESSAGES"]
