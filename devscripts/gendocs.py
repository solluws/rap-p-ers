"""Generate LLMS.md's "Complete surface" section from the live package.

That section is 850 of LLMS.md's 1584 lines and every one of them was typed by
hand. It showed: 49 signatures had their keyword arguments elided to `*, ...`,
which for those methods was the only rendering anywhere -- three agents given
only the docs each had to guess at them. Alongside that, one heading was
malformed (`#### ### client.admin`), two sections disagreed about whether to
quote annotations, and one return type was double-quoted (`-> "'CallSession'"`).

None of that is a writing problem. A signature is derivable, so deriving it
makes the whole class of defect impossible.

What this does NOT generate is the prose: the mental model, the protocol traps,
the worked examples, the performance notes. Those are the half of the document
a machine cannot check and a reader actually needs -- and the traps section is
the one that measurably worked, so it stays hand-written.

    python devscripts/gendocs.py            # diff against LLMS.md, exit 1 on drift
    python devscripts/gendocs.py --print    # emit the section to stdout
    python devscripts/gendocs.py --write    # replace the section in LLMS.md
"""

from __future__ import annotations

import argparse
import difflib
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rootpy import RootClient  # noqa: E402

LLMS = pathlib.Path(__file__).resolve().parent.parent / "LLMS.md"
START = "## 14. Complete surface"
END = "## 15. Testing"


# --------------------------------------------------------------------------
# signature rendering
# --------------------------------------------------------------------------
def unwrap(text) -> str:
    """`from __future__ import annotations` stringifies these; unwrap fully.

    One entry in the hand-written listing read ``-> "'CallSession'"`` -- quoted
    twice, because a previous pass stringified an already-stringified
    annotation. Looping rather than stripping once makes that unrepresentable.
    """
    value = str(text)
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def render_signature(func) -> str | None:
    """`(a: int, *, b: str = 'x') -> Thing`, or None if it cannot be read."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None

    parts: list[str] = []
    seen_kwonly = False
    for name, p in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append("*" + name)
            seen_kwonly = True
            continue
        if p.kind is p.VAR_KEYWORD:
            parts.append("**" + name)
            continue
        if p.kind is p.KEYWORD_ONLY and not seen_kwonly:
            parts.append("*")
            seen_kwonly = True
        piece = name
        if p.annotation is not p.empty:
            piece += f": {unwrap(p.annotation)}"
        if p.default is not p.empty:
            piece += f" = {p.default!r}"
        parts.append(piece)

    text = f"({', '.join(parts)})"
    if sig.return_annotation is not sig.empty:
        text += f" -> {unwrap(sig.return_annotation)}"
    return text


def prefix_for(func) -> str:
    """`await `, `async `, or padding -- so the column lines up."""
    if inspect.isasyncgenfunction(func):
        return "  gen "
    if inspect.iscoroutinefunction(func):
        return "async "
    return "      "


def public_callables(obj):
    """(name, func) for the public methods of obj's type, sorted."""
    cls = obj if inspect.isclass(obj) else type(obj)
    out = []
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if isinstance(inspect.getattr_static(cls, name, None), property):
            continue
        if callable(member):
            out.append((name, member))
    return sorted(out)


def public_properties(obj):
    cls = obj if inspect.isclass(obj) else type(obj)
    return sorted(
        name
        for name in dir(cls)
        if not name.startswith("_")
        and isinstance(inspect.getattr_static(cls, name, None), property)
    )


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------
def client_verbs(client) -> list[str]:
    lines = [
        "### Client verbs",
        "",
        "Generated from the live package. `async` entries are awaited, `gen`",
        "entries are async generators (`async for`), and the rest are",
        "synchronous cache reads that raise TypeError if awaited.",
        "",
        "```python",
    ]
    for name, func in public_callables(client):
        rendered = render_signature(func)
        if rendered is None:
            continue
        lines.append(f"{prefix_for(func)}client.{name}{rendered}")
    lines += ["```", ""]

    # Naming the await trap explicitly, because it is the one this listing
    # can cause: a reader who sees a name in a signature dump reaches for
    # parentheses. Derived, so a new property cannot go unmentioned.
    props = public_properties(client)
    if props:
        lines += [
            "Properties (no parentheses, never awaited): "
            + ", ".join(f"`{p}`" for p in props),
            "",
            "`gen` entries are async generators — iterate with `async for`, "
            "do not await them.",
            "",
        ]
    return lines


def namespaces(client) -> list[str]:
    """One block per `client.<attr>` that is a service/manager object."""
    lines = [
        "### Service namespaces",
        "",
        "Grouped by what they touch. Aliases are noted where two names reach",
        "the same object.",
        "",
    ]

    # Namespace attributes are whatever on the client is a rootpy object with
    # public methods of its own. Both instance attributes *and* class-level
    # properties count: `client.calls` is a lazily-constructed property, so a
    # vars(client) walk missed the entire 15-method CallService namespace.
    found: dict[int, list[str]] = {}
    candidates = set(vars(client))
    candidates |= {
        name
        for name in dir(type(client))
        if isinstance(inspect.getattr_static(type(client), name, None), property)
    }
    for name in sorted(candidates):
        if name.startswith("_"):
            continue
        try:
            value = getattr(client, name, None)
        except Exception:
            continue        # a property that needs a live connection
        if value is None or not hasattr(type(value), "__module__"):
            continue
        if not str(type(value).__module__).startswith("rootpy"):
            continue
        if not public_callables(value):
            continue
        found.setdefault(id(value), []).append(name)

    for names in sorted(found.values(), key=lambda n: n[0]):
        value = getattr(client, names[0])
        title = " / ".join(f"`client.{n}`" for n in names)
        lines.append(f"#### {title} — {type(value).__name__}")
        if len(names) > 1:
            lines.append("")
            lines.append(f"*{names[0]} and {names[1]} are the same object.*")
        lines += ["", "```python"]
        for meth, func in public_callables(value):
            rendered = render_signature(func)
            if rendered is None:
                continue
            lines.append(
                f"{prefix_for(func)}client.{names[0]}.{meth}{rendered}"
            )
        lines += ["```", ""]
    return lines


def objects() -> list[str]:
    """Field lists and methods for the records the API hands back."""
    import dataclasses

    from rootpy import models

    lines = [
        "### Objects you get back",
        "",
        "Fields come from the dataclass definition, so this cannot drift from",
        "the code. Objects returned by a fetch carry a client reference and can",
        "act on themselves.",
        "",
    ]
    # Every dataclass in the package, found by walking the submodules rather
    # than by reading the `rootpy` namespace.
    #
    # Reading the namespace made the output depend on what had already been
    # imported: running this after a test that imported more of the package
    # produced six extra entries (`AudioPlayback`, `EnumValue`, ...), so the
    # staleness check passed alone and failed in a full suite run. A generator
    # whose output depends on import order cannot gate anything. Walking every
    # submodule is both deterministic and complete -- and it is what caught
    # `Asset`, which lives in rootpy.services.assets and was missing entirely
    # when this only scanned rootpy.models.
    import importlib
    import pkgutil

    import rootpy

    seen = {}
    for info in pkgutil.walk_packages(rootpy.__path__, "rootpy."):
        try:
            module = importlib.import_module(info.name)
        except Exception:            # an optional dependency is missing
            continue
        for n, c in inspect.getmembers(module, inspect.isclass):
            if n.startswith("_") or not dataclasses.is_dataclass(c):
                continue
            if not str(c.__module__).startswith("rootpy"):
                continue
            seen[n] = c

    for name in sorted(seen):
        cls = seen[name]
        fields = [
            f.name for f in dataclasses.fields(cls) if not f.name.startswith("_")
        ]
        lines += [f"#### `{name}`", "", "```python"]
        lines.append(f"# fields: {', '.join(fields)}")
        for meth, func in public_callables(cls):
            rendered = render_signature(func)
            if rendered is None:
                continue
            lines.append(f"{prefix_for(func)}{name.lower()}.{meth}{rendered}")
        for prop in public_properties(cls):
            lines.append(f"      {name.lower()}.{prop}    # property")
        lines += ["```", ""]
    return lines


#: Everything from this heading to the end of section 14 is hand-written and
#: is spliced back verbatim. These are *curated* lists -- someone chose which
#: imports matter and annotated them ("a str (the URL) carrying signup
#: context") -- so generating them would replace judgement with a dump.
#:
#: They are not immune to drift, though: the multi-account sketch still said
#: ``shared_transport=False`` long after the default became True. Deriving what
#: can be derived shrinks the hand-written surface; it does not remove the need
#: to read it.
TAIL_MARKER = "### Module-level classes"


def preserved_tail() -> str:
    """The hand-written remainder of section 14, verbatim."""
    text = LLMS.read_text(encoding="utf-8")
    start = text.index(TAIL_MARKER)
    end = text.index(END)
    return text[start:end].rstrip() + "\n"


def build() -> str:
    client = RootClient(token="x")
    lines = [
        START,
        "",
        "**Generated** by `python devscripts/gendocs.py` from the live package —",
        "do not edit by hand. Signatures, defaults, field lists and async-ness",
        "are read from the code, so this section cannot drift from it. The prose",
        "elsewhere in this file is written by hand and is where the reasoning",
        "lives; this is the reference.",
        "",
        "Two ways to reach most things: a **verb on the client** (shortest) or",
        "the **namespace** it lives in (fuller options). `client.message(...)`",
        "and `client.messages.send(...)` do the same job.",
        "",
    ]
    lines += client_verbs(client)
    lines += namespaces(client)
    lines += objects()
    generated = "\n".join(lines).rstrip() + "\n"
    return generated + "\n" + preserved_tail()


# --------------------------------------------------------------------------
def current_section() -> str:
    text = LLMS.read_text(encoding="utf-8")
    start = text.index(START)
    end = text.index(END)
    return text[start:end].rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    generated = build()

    if args.show:
        sys.stdout.write(generated)
        return 0

    if args.write:
        text = LLMS.read_text(encoding="utf-8")
        start, end = text.index(START), text.index(END)
        LLMS.write_text(text[:start] + generated + "\n" + text[end:], encoding="utf-8")
        print(f"wrote {len(generated.splitlines())} lines into LLMS.md")
        return 0

    old, new = current_section(), generated
    if old == new:
        print(f"LLMS.md section 14 is up to date ({len(new.splitlines())} lines)")
        return 0
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        "LLMS.md (committed)", "generated", lineterm="", n=1,
    ))
    print("\n".join(diff[:400]))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    print(f"\nsection 14 is stale: +{added} / -{removed} line(s). "
          "Run `python devscripts/gendocs.py --write`.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
