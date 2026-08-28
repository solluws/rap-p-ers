"""Do the kwargs the SDK sends actually exist on the request messages?

``StructuredProtoCodec.encode_message`` raises ``TypeError: <Request> has no
field 'x'`` for an unknown keyword -- *before* the request goes out. So a
manager that names a field wrong is not a subtle wire-level problem, it is a
method that has never worked and can never work. Nothing catches that until
someone calls it, which for a rarely-used setter can be never.

Found exactly that in ``user_settings``: all three
``set_*_invite_requirement`` methods sent ``is_required`` at a request whose
field is ``is_email_verified``.

This walks every ``*.high.<alias>.<method>(...)`` call site in ``rootpy/`` and
checks the keyword names against the registry schema, both for keywords written
literally and for the very common

    kwargs = {"community_id": community_id}
    if reason is not None:
        kwargs["reason"] = reason
    return (await self.client.high.thing.do(**kwargs)).data

shape, where the keys are string literals in the same function.

    python devscripts/kwargcheck.py           # report problems, exit 1 on any
    python devscripts/kwargcheck.py --all     # also list the calls that check out
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rootpy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def wire_fields(api, alias, method):
    """{accepted keyword names} for high.<alias>.<method>, or None if unknown."""
    try:
        service = getattr(api, alias)
    except AttributeError:
        return None
    try:
        bound = getattr(service, method)
        info = bound.info
    except Exception:
        return None
    schema = api.codec.resolve_message(info["request"])
    if schema is None:
        return set(), info["request"]
    names = set()
    for field in schema["fields"]:
        names.add(field["name"])
        names.add(field["python_name"])
        names.add(field["name"].casefold())
    return names, info["request"]


def chain(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    return tuple(reversed(parts))


def literal_keys_for(func_node, var_name):
    """String-literal keys assigned into ``var_name`` anywhere in this function.

    Covers ``kwargs = {"a": 1}``, ``kwargs["b"] = 2`` and
    ``kwargs.update({"c": 3})``. Returns (keys, exact) where ``exact`` is False
    if anything non-literal was written into the dict -- a computed key means
    the key set is a subset, so a missing field is a maybe, not a finding.
    """
    keys = set()
    exact = True
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                keys.add(key.value)
                            else:
                                exact = False
                    elif isinstance(node.value, ast.Call):
                        # kwargs = dict(kwargs) -- carries whatever came in.
                        exact = False
                    else:
                        exact = False
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == var_name
                ):
                    key = target.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
                    else:
                        exact = False
        elif isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "update"
                and isinstance(f.value, ast.Name)
                and f.value.id == var_name
            ):
                if node.args and isinstance(node.args[0], ast.Dict):
                    for key in node.args[0].keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
                        else:
                            exact = False
                else:
                    exact = False
    return keys, exact


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="list clean call sites too")
    args = ap.parse_args()

    api = rootpy.RootClient(token="x").high

    problems = []
    clean = 0
    unknown = 0

    for path in sorted(ROOT.joinpath("rootpy").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        for func in funcs:
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                parts = chain(node.func)
                if len(parts) < 3 or "high" not in parts:
                    continue
                idx = parts.index("high")
                if idx + 2 >= len(parts) + 0 or len(parts) < idx + 3:
                    continue
                alias, method = parts[idx + 1], parts[idx + 2]

                resolved = wire_fields(api, alias, method)
                if resolved is None:
                    unknown += 1
                    continue
                accepted, request_type = resolved

                sent = set()
                exact = True
                for kw in node.keywords:
                    if kw.arg is None:
                        if isinstance(kw.value, ast.Name):
                            keys, ok = literal_keys_for(func, kw.value.id)
                            sent |= keys
                            exact = exact and ok
                        else:
                            exact = False
                    else:
                        sent.add(kw.arg)

                bad = sorted(k for k in sent if k not in accepted)
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                if bad:
                    problems.append(
                        f"{where}\n"
                        f"    {func.name}() -> high.{alias}.{method}({request_type})\n"
                        f"    sends {bad} -- not a field.\n"
                        f"    accepted: "
                        + ", ".join(sorted(
                            n for n in accepted if n.islower() and "_" in n or n.islower()
                        ))
                    )
                else:
                    clean += 1
                    if args.all:
                        print(f"ok   {where}  high.{alias}.{method}  "
                              f"{sorted(sent) if exact else sorted(sent) + ['...']}")

    print()
    if problems:
        print(f"{len(problems)} call site(s) send a field the request does not have:\n")
        for p in problems:
            print(p)
            print()
    print(f"{clean} clean, {len(problems)} broken, {unknown} unresolved call sites")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
