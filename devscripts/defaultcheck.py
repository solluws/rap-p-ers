"""Does any wrapper's default defeat the sentinel of the method it forwards to?

The layering has five overlapping manager levels, and a call typically passes
straight through two or three of them by keyword: ``community.edit_role`` ->
``admin.edit_role``, ``members.ban`` -> ``moderation.ban``, and so on.

That is fine until the lower layer uses ``None`` as a *sentinel* -- "the
caller did not supply this, so read the current object and carry the value
forward". ``CommunityRoleEdit`` and ``CommunityEdit`` are replaces, not
patches, so this sentinel is load-bearing: an omitted field is a blanked
field.

``CommunityManager.edit_role`` declared ``color_hex: str = ""`` and forwarded
it verbatim. ``""`` is not ``None``, so the carry-forward never ran, and
``normalize_hex_colour("")`` then raised ``ValueError`` before the request was
built. ``client.admin.edit_role(cid, rid, name="x")`` worked; the manager
spelling of the same call could not be made at all.

This finds every ``f(x=x)`` forward where the callee's default is ``None`` and
the caller's is a *different* falsy constant -- the shape that silently turns
"unspecified" into "specified as empty".

    python devscripts/defaultcheck.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def constant_default(node):
    """(is_constant, value) for a default expression."""
    if isinstance(node, ast.Constant):
        return True, node.value
    return False, None


def collect_functions(trees):
    """{method_name: [(where, {param: (is_const, value)})]}."""
    out = {}
    for path, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            defaults = {}

            positional = args.posonlyargs + args.args
            for arg, default in zip(
                positional[len(positional) - len(args.defaults):], args.defaults
            ):
                defaults[arg.arg] = constant_default(default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None:
                    defaults[arg.arg] = constant_default(default)

            out.setdefault(node.name, []).append(
                (f"{path.name}:{node.lineno}", node.name, defaults)
            )
    return out


def main():
    trees = []
    for path in sorted(ROOT.joinpath("rootpy").rglob("*.py")):
        trees.append((path, ast.parse(path.read_text(encoding="utf-8"))))

    functions = collect_functions(trees)

    problems = []
    checked = 0
    for path, tree in trees:
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            caller_defaults = {}
            for entry in functions.get(func.name, []):
                if entry[0] == f"{path.name}:{func.lineno}":
                    caller_defaults = entry[2]

            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                callee = getattr(node.func, "attr", None)
                if callee is None or callee not in functions:
                    continue
                # Only pass-through keywords: f(x=x).
                forwards = [
                    kw.arg for kw in node.keywords
                    if kw.arg
                    and isinstance(kw.value, ast.Name)
                    and kw.value.id == kw.arg
                ]
                for target_where, _name, target_defaults in functions[callee]:
                    if target_where == f"{path.name}:{func.lineno}":
                        continue
                    for param in forwards:
                        if param not in caller_defaults:
                            continue
                        if param not in target_defaults:
                            continue
                        checked += 1
                        caller_const, caller_value = caller_defaults[param]
                        target_const, target_value = target_defaults[param]
                        if not (caller_const and target_const):
                            continue
                        if target_value is None and caller_value is not None \
                                and not caller_value:
                            problems.append(
                                f"{path.name}:{func.lineno} {func.name}() "
                                f"declares {param}={caller_value!r} and "
                                f"forwards it to {callee}() at {target_where}, "
                                f"whose default is None.\n"
                                f"    If None is that callee's "
                                f"'carry the current value forward' sentinel, "
                                f"this wrapper can never trigger it."
                            )

    print()
    if problems:
        print(f"{len(problems)} forward(s) whose default defeats a None sentinel:\n")
        for problem in sorted(set(problems)):
            print(problem)
            print()
    print(f"{checked} forwarded parameter(s) compared, {len(set(problems))} suspect")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
