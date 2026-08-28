"""Offline discovery for the API surface: ``client.explain`` and
``client.preview``.

The structured layer already knows the exact wire shape of every RPC --
``client.high.<service>.describe()`` lists a service's methods and
``StructuredMethod.signature()`` gives a method's request fields -- but there
was no path to it from the ordinary client, so the one piece of the SDK that
can answer "what does this actually send" was the piece nobody reached for.

Two helpers close that:

* ``client.explain(target)`` resolves a name the way a person would spell it --
  a wire service (``"file"``), a wire method (``"file.search"``) or a friendly
  manager method (``"community_files.search"``) -- and shows the Python
  signature *and* the wire fields underneath it. The manager-to-wire link is
  read from the code itself (which ``high.<service>.<method>`` calls the method
  makes), not from a hand-kept table, so it cannot drift.

* ``client.preview(target, **kwargs)`` encodes a request and shows exactly what
  would go on the wire -- the framed byte length and body -- *without sending
  it*. Because encoding validates field names and values (an unknown keyword or
  a malformed GUID raises here, before any request), a preview is also a way to
  check a call is well-formed for the price of zero round trips.

Everything here is pure introspection: no token, no network, no heavy import.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field as _dcfield
from typing import Any, Dict, List, Optional, Tuple

from .protocol import grpc_frame
from .structured_api import _snake

# Infrastructure objects on the client that carry coroutine methods but are not
# part of the callable API surface a person would want to explain.
_INFRA = frozenset({"transport", "auth", "gateway", "session", "cache", "stats"})


# -------------------------------------------------------------------------- #
# resolution
# -------------------------------------------------------------------------- #
def _managers(client) -> Dict[str, Any]:
    """The manager/service objects on ``client``, by attribute name.

    Discovered, not listed: any rootpy-defined instance attribute that exposes
    at least one public coroutine method. New managers show up automatically.
    """
    out: Dict[str, Any] = {}
    for name, value in vars(client).items():
        if name.startswith("_") or name in _INFRA or value is None or callable(value):
            continue
        cls = type(value)
        if not cls.__module__.startswith("rootpy"):
            continue
        if any(
            inspect.iscoroutinefunction(getattr(cls, n, None))
            for n in dir(cls)
            if not n.startswith("_")
        ):
            out[name] = value
    return out


def _structured_service(api, head: str):
    """Resolve ``head`` to ``(alias, StructuredService)`` or ``(None, None)``.

    Accepts a wire alias (``"file"``), the ``<x>_api`` spelling the client
    exposes (``"file_api"``) and any casing/underscore variant the structured
    layer already tolerates.
    """
    from .client import _HIGH_ALIASES

    candidate = _HIGH_ALIASES.get(head, head)
    try:
        service = getattr(api, candidate)
    except AttributeError:
        return None, None
    return api.service_alias(service.service_name), service


def _high_calls(func) -> List[Tuple[str, str]]:
    """The ``(alias, method)`` wire calls a manager method makes, read from its
    source. Empty when the source is unavailable or the method makes none."""
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: List[Tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        parts: List[str] = []
        cursor: Any = node.func
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        parts.reverse()
        if "high" in parts:
            tail = parts[parts.index("high") + 1:]
            if len(tail) >= 2:
                pair = (tail[0], tail[1])
                if pair not in found:
                    found.append(pair)
    return found


def _wire_method(api, service, method_name: str):
    """The ``StructuredMethod`` for ``service.method_name``.

    Raises ``AttributeError`` with the service's method list when the name does
    not resolve -- the same shape of error the structured layer already gives.
    """
    try:
        return getattr(service, method_name)
    except AttributeError:
        raise AttributeError(
            f"wire service {api.service_alias(service.service_name)!r} has no "
            f"method {method_name!r}. Methods: "
            + ", ".join(service.methods())
        ) from None


def _as_wire(method) -> "WireMethod":
    info = method.info
    return WireMethod(
        alias=method.api.service_alias(method.service_name),
        method=_snake(method.method_name),
        endpoint=info["endpoint"],
        request=info["request"],
        response=info["response"],
        method_type=info["method_type"],
        signature=method.signature(),
        fields=list(method.fields),
    )


# -------------------------------------------------------------------------- #
# result objects
# -------------------------------------------------------------------------- #
@dataclass
class WireMethod:
    """One structured RPC and the request fields it carries."""

    alias: str
    method: str
    endpoint: str
    request: str
    response: str
    method_type: str
    signature: str
    fields: List[dict]

    def field_lines(self, indent: str = "    ") -> List[str]:
        if not self.fields:
            return [indent + "(no request fields)"]
        width = max(len(f["python_name"]) for f in self.fields)
        lines = []
        for f in self.fields:
            suffix = "[]" if f.get("repeated") else ""
            lines.append(
                f"{indent}{f['python_name']:<{width}} : {f['inner_type']}{suffix}"
            )
        return lines


@dataclass
class Explanation:
    """What :meth:`RootClient.explain` returns. Prints itself; ``as_dict`` for
    programmatic use."""

    target: Optional[str]
    kind: str  # "index" | "service" | "manager" | "wire_method" | "manager_method"
    title: str = ""
    doc: Optional[str] = None
    python_signature: Optional[str] = None
    members: List[Tuple[str, str]] = _dcfield(default_factory=list)
    wire: List[WireMethod] = _dcfield(default_factory=list)
    managers: List[str] = _dcfield(default_factory=list)
    services: List[str] = _dcfield(default_factory=list)
    hint: Optional[str] = None

    def render(self) -> str:
        out: List[str] = []
        if self.title:
            out.append(self.title)
        if self.python_signature:
            out.append(f"  {self.python_signature}")
        if self.doc:
            out.append(f"  {self.doc}")

        if self.kind == "index":
            out.append("")
            out.append("managers (friendly signatures) -- explain(\"<name>\"):")
            out += _wrap(self.managers, "  ")
            out.append("")
            out.append("wire services (client.high.<name>, exact fields):")
            out += _wrap(self.services, "  ")
        elif self.members:
            out.append("")
            width = max((len(name) for name, _ in self.members), default=0)
            for name, detail in self.members:
                out.append(f"  {name:<{width}}  {detail}".rstrip())

        if self.wire:
            for wm in self.wire:
                out.append("")
                out.append(f"  sends on the wire -> {wm.signature}")
                out.append(f"    {wm.method_type}  {wm.endpoint}")
                out.append(f"    request {wm.request}  ->  response {wm.response}")
                out += wm.field_lines("      ")

        if self.hint:
            out.append("")
            out.append(self.hint)
        return "\n".join(out)

    __str__ = render

    def __repr__(self) -> str:  # so it prints usefully when echoed in a REPL
        return self.render()

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "kind": self.kind,
            "python_signature": self.python_signature,
            "doc": self.doc,
            "members": [{"name": n, "detail": d} for n, d in self.members],
            "wire": [
                {
                    "signature": w.signature,
                    "endpoint": w.endpoint,
                    "request": w.request,
                    "response": w.response,
                    "method_type": w.method_type,
                    "fields": [
                        {
                            "name": f["python_name"],
                            "type": f["inner_type"],
                            "repeated": bool(f.get("repeated")),
                        }
                        for f in w.fields
                    ],
                }
                for w in self.wire
            ],
            "managers": list(self.managers),
            "services": list(self.services),
        }


@dataclass
class Preview:
    """What :meth:`RootClient.preview` returns: an encoded, *unsent* request."""

    alias: str
    method: str
    endpoint: str
    request: str
    response: str
    method_type: str
    values: Dict[str, Any]
    fields: List[dict]
    unframed: bytes
    framed: bytes

    @property
    def size(self) -> int:
        """Bytes that would go on the wire, framing included."""
        return len(self.framed)

    def _value_lines(self, indent: str = "    ") -> List[str]:
        # Map each supplied keyword to its wire field name for display.
        by_key = {}
        for f in self.fields:
            for key in (f["python_name"], f["name"], f["name"].casefold()):
                by_key[key] = f["python_name"]
        shown = {}
        for key, value in self.values.items():
            shown[by_key.get(key, by_key.get(str(key).casefold(), key))] = value
        if not shown:
            return [indent + "(no fields set)"]
        width = max(len(str(k)) for k in shown)
        return [f"{indent}{str(k):<{width}} = {v!r}" for k, v in shown.items()]

    def render(self) -> str:
        head = self.framed[:32].hex()
        if len(self.framed) > 32:
            head += "..."
        out = [
            f"preview  {self.alias}.{self.method}   (encoded, NOT sent)",
            f"  endpoint  {self.endpoint}",
            f"  type      {self.method_type}",
            f"  request   {self.request}  ->  response {self.response}",
            f"  wire body {len(self.unframed)} bytes  ({len(self.framed)} framed)",
            "  fields sent:",
            *self._value_lines("    "),
            f"  bytes     {head}",
        ]
        return "\n".join(out)

    __str__ = render

    def __repr__(self) -> str:
        return self.render()

    def as_dict(self) -> dict:
        return {
            "alias": self.alias,
            "method": self.method,
            "endpoint": self.endpoint,
            "request": self.request,
            "response": self.response,
            "method_type": self.method_type,
            "values": dict(self.values),
            "unframed_len": len(self.unframed),
            "framed_len": len(self.framed),
            "framed_hex": self.framed.hex(),
        }


# -------------------------------------------------------------------------- #
# entry points (RootClient.explain / .preview delegate here)
# -------------------------------------------------------------------------- #
def explain(client, target: Optional[str] = None) -> Explanation:
    api = client._build_high()
    managers = _managers(client)

    if not target:
        return _index(api, managers)

    head, dot, tail = target.partition(".")
    method_name = tail if dot else None

    alias, service = _structured_service(api, head)
    if service is not None:
        if method_name is None:
            return _service_explanation(alias, service)
        return _wire_method_explanation(api, service, method_name)

    if head in managers:
        manager = managers[head]
        if method_name is None:
            return _manager_explanation(api, head, manager)
        return _manager_method_explanation(api, head, manager, method_name)

    raise ValueError(
        f"can't explain {target!r}: {head!r} is not a manager or wire service. "
        f"Try explain() with no argument to list them."
    )


def preview(client, target: str, /, **kwargs) -> Preview:
    api = client._build_high()
    method = _resolve_for_preview(client, api, target)
    info = method.info
    if info["method_type"] != "Unary":
        raise NotImplementedError(
            f"{method.service_name}/{method.method_name} is "
            f"{info['method_type']}, not unary -- preview covers unary RPCs"
        )
    # encode_message validates keyword names and field values (a bad GUID or an
    # unknown field raises here, offline, before anything is sent).
    unframed = api.codec.encode_message(info["request"], kwargs)
    return Preview(
        alias=api.service_alias(method.service_name),
        method=_snake(method.method_name),
        endpoint=info["endpoint"],
        request=info["request"],
        response=info["response"],
        method_type=info["method_type"],
        values=dict(kwargs),
        fields=list(method.fields),
        unframed=unframed,
        framed=grpc_frame(unframed),
    )


# -------------------------------------------------------------------------- #
# builders
# -------------------------------------------------------------------------- #
def _index(api, managers) -> Explanation:
    # Fold exact-identity aliases (e.g. admin/community_admin) to one name.
    seen: Dict[int, str] = {}
    for name in sorted(managers):
        seen.setdefault(id(managers[name]), name)
    manager_names = sorted(seen.values())
    return Explanation(
        target=None,
        kind="index",
        title="rootpy discovery -- explain(\"<name>\") or explain(\"<name>.<method>\")",
        managers=manager_names,
        services=sorted(api.services()),
        hint="  preview a call without sending it: "
        "client.preview(\"message.create\", ...)",
    )


def _service_explanation(alias, service) -> Explanation:
    described = service.describe()  # {method: wire signature}
    members = [(name, described[name]) for name in sorted(described)]
    return Explanation(
        target=alias,
        kind="service",
        title=f"wire service {alias!r}  (client.high.{alias})",
        doc=f"{len(members)} method(s); each signature is the exact request fields.",
        members=members,
        hint=f"  detail one: client.explain(\"{alias}.{members[0][0]}\")"
        if members
        else None,
    )


def _wire_method_explanation(api, service, method_name) -> Explanation:
    method = _wire_method(api, service, method_name)
    wm = _as_wire(method)
    return Explanation(
        target=f"{wm.alias}.{wm.method}",
        kind="wire_method",
        title=f"wire method  {wm.alias}.{wm.method}",
        wire=[wm],
        hint=f"  preview it: client.preview(\"{wm.alias}.{wm.method}\", ...)",
    )


def _manager_explanation(api, head, manager) -> Explanation:
    cls = type(manager)
    members: List[Tuple[str, str]] = []
    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        attr = inspect.getattr_static(cls, name, None)
        if not (inspect.iscoroutinefunction(attr) or inspect.isfunction(attr)):
            continue
        calls = _high_calls(attr)
        detail = "-> wire " + ", ".join(f"{a}.{m}" for a, m in calls) if calls else ""
        members.append((name, detail))
    return Explanation(
        target=head,
        kind="manager",
        title=f"manager {head!r}  (client.{head})  -- {cls.__name__}",
        doc=_first_doc_line(cls),
        members=members,
        hint=f"  detail one: client.explain(\"{head}.{members[0][0]}\")"
        if members
        else None,
    )


def _manager_method_explanation(api, head, manager, method_name) -> Explanation:
    cls = type(manager)
    func = inspect.getattr_static(cls, method_name, None)
    if func is None or not (
        inspect.iscoroutinefunction(func) or inspect.isfunction(func)
    ):
        public = sorted(
            n
            for n in dir(cls)
            if not n.startswith("_")
            and (
                inspect.iscoroutinefunction(inspect.getattr_static(cls, n, None))
                or inspect.isfunction(inspect.getattr_static(cls, n, None))
            )
        )
        raise AttributeError(
            f"manager {head!r} ({cls.__name__}) has no method {method_name!r}. "
            f"Methods: " + ", ".join(public)
        )

    try:
        # The bound method's signature omits ``self``.
        py_sig = f"{head}.{method_name}{inspect.signature(getattr(manager, method_name))}"
    except (TypeError, ValueError):
        py_sig = f"{head}.{method_name}(...)"

    wire: List[WireMethod] = []
    for called_alias, called_method in _high_calls(func):
        alias, service = _structured_service(api, called_alias)
        if service is None:
            continue
        try:
            wire.append(_as_wire(_wire_method(api, service, called_method)))
        except AttributeError:
            continue

    hint = None
    if len(wire) == 1:
        hint = (
            f"  preview the wire call: "
            f"client.preview(\"{wire[0].alias}.{wire[0].method}\", ...)"
        )
    return Explanation(
        target=f"{head}.{method_name}",
        kind="manager_method",
        title=f"manager method  {head}.{method_name}  -- {cls.__name__}",
        python_signature=py_sig,
        doc=_first_doc_line(func),
        wire=wire,
        hint=hint,
    )


def _resolve_for_preview(client, api, target: str):
    """Resolve ``target`` to a ``StructuredMethod`` for encoding.

    A wire spelling (``"file.search"``) resolves directly. A manager spelling
    (``"community_files.search"``) resolves to its single underlying wire call;
    if it makes none or several, that is an error naming the alternatives,
    because there is no one request to encode.
    """
    if "." not in target:
        raise ValueError(
            f"preview needs a method, e.g. \"message.create\" -- got {target!r}. "
            f"Use client.explain({target!r}) to list its methods."
        )
    head, _, method_name = target.partition(".")

    alias, service = _structured_service(api, head)
    if service is not None:
        return _wire_method(api, service, method_name)

    managers = _managers(client)
    if head in managers:
        func = inspect.getattr_static(type(managers[head]), method_name, None)
        if func is None:
            raise AttributeError(
                f"manager {head!r} has no method {method_name!r}"
            )
        calls = _high_calls(func)
        if len(calls) == 1:
            calias, cmethod = calls[0]
            _, csvc = _structured_service(api, calias)
            if csvc is not None:
                return _wire_method(api, csvc, cmethod)
        if not calls:
            raise ValueError(
                f"{target!r} makes no wire call to preview -- it is a "
                f"client-side helper. There is no single request to encode."
            )
        listed = ", ".join(f"{a}.{m}" for a, m in calls)
        raise ValueError(
            f"{target!r} maps to {len(calls)} wire calls ({listed}); preview one "
            f"directly, e.g. client.preview(\"{calls[0][0]}.{calls[0][1]}\", ...)"
        )

    raise ValueError(
        f"can't preview {target!r}: {head!r} is not a manager or wire service."
    )


# -------------------------------------------------------------------------- #
# small helpers
# -------------------------------------------------------------------------- #
def _first_doc_line(obj) -> Optional[str]:
    doc = inspect.getdoc(obj)
    if not doc:
        return None
    return doc.strip().splitlines()[0]


def _wrap(names: List[str], indent: str, width: int = 76) -> List[str]:
    lines: List[str] = []
    current = indent
    for name in names:
        piece = name + ", "
        if len(current) + len(piece) > width and current != indent:
            lines.append(current.rstrip())
            current = indent
        current += piece
    if current.strip():
        lines.append(current.rstrip().rstrip(","))
    return lines
