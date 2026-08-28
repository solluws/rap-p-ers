"""Would every call in the live suite bind to its signature?

The await-contract guard checks that ``client.foo.bar`` exists and that its
sync/async-ness matches. It does not check the *arguments*. A misspelled
keyword or one positional too many is a ``TypeError`` at call time, which
means it is discovered part-way through a live run -- the same class of cost
as the missing import that prompted the undefined-name guard.

``inspect.Signature.bind`` answers it offline. Every ``client.*`` /
``peer.*`` / ``gateway_client.*`` call in ``tests/test_live_*.py`` is bound
against the real method, with a placeholder for each argument since only the
shape matters.

Methods that take ``**kwargs`` bind anything, so ``inspect`` has nothing to
say about them -- and those are exactly the ones a test is most likely to get
wrong, because the signature is not the documentation. For those the keywords
are traced one hop further, to the ``high.<alias>.<method>`` the manager
forwards to, and checked against the wire schema instead. That is the same
check ``kwargcheck.py`` runs over the library; this runs it over the tests.

    python devscripts/bindcheck.py
    python devscripts/bindcheck.py --all    # list the permissive ones too
"""

from __future__ import annotations

import argparse
import ast
import inspect
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rootpy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RECEIVERS = {"client", "gateway_client", "peer"}


class Placeholder:
    def __repr__(self):
        return "<arg>"


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in RECEIVERS:
        return ".".join(reversed(parts))
    return None


def resolve(path):
    target = rootpy.RootClient(token="x")
    for part in path.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    return target


def takes_var_keyword(signature):
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in signature.parameters.values()
    )


def forwarded_endpoint(method):
    """The ``(alias, method)`` a ``**kwargs`` manager forwards to, if unambiguous.

    Reads the method's own source for a single ``...high.<alias>.<name>(...)``
    call. Anything with none or several is left alone -- guessing which one a
    branchy method took is how the wrong thing gets asserted.
    """
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return None
    # getsource keeps the method's original indentation, and cleandoc only
    # strips from the *second* line on -- so a one-line body ends up
    # dedented relative to its own `def`. textwrap.dedent treats all lines
    # alike, which is what is wanted here.
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts = []
        cur = node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        parts = list(reversed(parts))
        if "high" in parts:
            index = parts.index("high")
            if len(parts) >= index + 3:
                found.add((parts[index + 1], parts[index + 2]))
    return next(iter(found)) if len(found) == 1 else None


def wire_fields(api, alias, name):
    """Accepted keyword names for ``high.<alias>.<name>``, or None."""
    try:
        bound = getattr(getattr(api, alias), name)
        schema = api.codec.resolve_message(bound.info["request"])
    except Exception:
        return None
    if schema is None:
        return None
    accepted = set()
    for field in schema["fields"]:
        accepted.add(field["name"])
        accepted.add(field["python_name"])
        accepted.add(field["name"].casefold())
    return accepted, bound.info["request"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    api = rootpy.RootClient(token="x").high
    problems = []
    permissive = []
    checked = 0

    for source in sorted(ROOT.joinpath("tests").glob("test_live_*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            path = dotted(node.func)
            if not path:
                continue
            target = resolve(path)
            if target is None or not callable(target):
                continue
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):
                continue

            where = f"{source.name}:{node.lineno}"
            positional = [Placeholder() for _ in node.args]
            starred = any(isinstance(a, ast.Starred) for a in node.args)
            keywords = {}
            double_starred = False
            for keyword in node.keywords:
                if keyword.arg is None:
                    double_starred = True
                else:
                    keywords[keyword.arg] = Placeholder()

            if starred or double_starred:
                continue

            if takes_var_keyword(signature):
                endpoint = forwarded_endpoint(target)
                resolved = (
                    wire_fields(api, *endpoint) if endpoint else None
                )
                if resolved is None:
                    permissive.append(
                        f"{where}  client.{path}{tuple(keywords)}  "
                        "(no single forward to trace)"
                    )
                    continue
                accepted, request_type = resolved
                checked += 1
                bad = sorted(k for k in keywords if k not in accepted)
                if bad:
                    problems.append(
                        f"{where}\n"
                        f"    client.{path}(**kwargs) forwards to "
                        f"high.{endpoint[0]}.{endpoint[1]}({request_type})\n"
                        f"    passes {bad}, which the request does not have.\n"
                        f"    accepted: "
                        + ", ".join(sorted(
                            f["python_name"]
                            for f in api.codec.resolve_message(
                                request_type
                            )["fields"]
                        ))
                    )
                continue

            checked += 1
            try:
                signature.bind(*positional, **keywords)
            except TypeError as exc:
                problems.append(
                    f"{where}\n"
                    f"    client.{path}{inspect.signature(target)}\n"
                    f"    called with {len(positional)} positional and "
                    f"{sorted(keywords)}\n"
                    f"    -> TypeError: {exc}"
                )

    print()
    if problems:
        print(f"{len(problems)} call(s) would raise TypeError:\n")
        for problem in problems:
            print(problem)
            print()
    if args.all and permissive:
        print(f"{len(permissive)} call(s) nothing here can verify:")
        for line in permissive:
            print("  " + line)
        print()
    print(f"{checked} call(s) checked, {len(problems)} would fail, "
          f"{len(permissive)} unverifiable")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
