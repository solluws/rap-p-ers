"""Which of the 230 service methods does the live suite actually reach?

The 150/230 figure in LLMS.md was counted by hand. This recomputes it, and
more usefully names the methods that are *not* reached, so a coverage push can
be aimed rather than guessed at.

How it works. A live test rarely calls a service method directly -- it calls a
high-level verb (``client.send``, ``sandbox.community``) that calls two or
three of them. So a textual grep undercounts badly. Instead:

1. Every function and method in ``rootpy/`` and ``tests/`` is AST-parsed into a
   node keyed ``Class.method`` (or ``module:function``), recording the
   attribute chains it calls -- ``self.client.high.community_role.create``
   becomes the chain ``(self, client, high, community_role, create)``.
2. Chains are resolved against the service table: the *last* attribute is the
   method, and the one before it is matched against a service attribute name
   (``messages``, ``community``, ...) or a known alias (``admin`` ->
   ``community_admin``, ``dm_service`` -> ``direct_messages``).
3. From every ``test_*`` function marked live, walk the call graph transitively
   and union the service methods reached.

Unqualified chains (``self.foo(...)``, ``bar(...)``) are followed by name so a
verb defined on RootClient counts toward whatever it calls. Names that collide
across classes (``list``, ``create``, ``get``) are followed into *every*
candidate, which can over-count; ``--strict`` drops those, giving a lower
bound. Run both -- the truth is between them, and the gap tells you how much of
the number is inference.

**Known under-count.** A method reached through a helper that takes the
service as an *argument* is not credited, because nothing in the call chain
names it. There is one such case today:
``conftest.create_channel_group(manager, ...)`` calls
``manager.create_channel_group(...)``, so ``community.create_channel_group``
reports uncovered while several tests exercise it. Crediting every definition
the walk lands on was tried and rejected: it inflated the total by five,
because the walk also visits ambiguous name matches it never really calls.
Under-counting in a documented way beats over-counting silently -- read the
figure as a floor.

    python devscripts/covermap.py                 # summary + uncovered list
    python devscripts/covermap.py --strict        # lower bound
    python devscripts/covermap.py --service calls # one service, verbose
    python devscripts/covermap.py --json out.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rootpy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# client attribute -> the same object under another name. Both spellings appear
# in tests and in the library, and counting them separately would report a
# method as uncovered because the test happened to use the alias.
ALIASES = {
    "admin": "community_admin",
    "dm_service": "direct_messages",
}

SERVICE_ATTRS = [
    "direct_messages", "dm", "messages", "assets", "users", "community_service",
    "community_admin", "emojis", "moderation", "friends", "blocks", "invites",
    "notifications", "user_settings", "directories", "search", "roles",
    "friend_requests", "members", "community_files", "logs", "community_apps",
    "voice_admin", "friend_groups", "community", "permissions", "calls",
]


def service_table():
    """{service_attr: {method_name}} for the 230 public service methods."""
    client = rootpy.RootClient(token="x")
    table = {}
    for attr in SERVICE_ATTRS:
        obj = getattr(client, attr)
        cls = type(obj)
        table[attr] = {
            name
            for name in dir(cls)
            if not name.startswith("_") and callable(getattr(cls, name, None))
        }
    return table


def attr_chain(node):
    """('self', 'client', 'high', 'community_role', 'create') for a call func."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):
        parts.append("()")
    else:
        parts.append("?")
    return tuple(reversed(parts))


class Collector(ast.NodeVisitor):
    """One pass over a module: every function, and what it calls."""

    def __init__(self, module, module_is_live=False):
        self.module = module
        self.module_is_live = module_is_live
        self.nodes = {}          # key -> {"calls": [chain], "live": bool}
        self.stack = []

    def _key(self, name):
        cls = next((s for s in self.stack if s[0] == "class"), None)
        if cls:
            return f"{cls[1]}.{name}"
        return f"{self.module}:{name}"

    def visit_ClassDef(self, node):
        self.stack.insert(0, ("class", node.name))
        self.generic_visit(node)
        self.stack.pop(0)

    def _function(self, node):
        key = self._key(node.name)
        marks = set()
        for dec in node.decorator_list:
            chain = attr_chain(dec.func if isinstance(dec, ast.Call) else dec)
            if "mark" in chain:
                marks.update(chain)
        entry = self.nodes.setdefault(key, {"calls": [], "live": False})
        # Live files mark the whole module (``pytestmark = [pytest.mark.live,
        # ...]``), so a per-function decorator is the exception, not the rule.
        is_live = bool(marks & {"live", "live2"}) or (
            self.module_is_live and node.name.startswith("test_")
        )
        entry["live"] = entry["live"] or is_live
        self.stack.insert(0, ("func", key))
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                entry["calls"].append(attr_chain(child.func))
        self.stack.pop(0)
        # Nested classes/functions are already covered by ast.walk above; do
        # not generic_visit or their calls get attributed twice.

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function


def collect(paths):
    nodes = {}
    by_name = defaultdict(set)     # bare method name -> {node key}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = path.stem
        col = Collector(module, module_is_live=module.startswith("test_live"))
        col.visit(tree)
        for key, entry in col.nodes.items():
            if key in nodes:
                nodes[key]["calls"].extend(entry["calls"])
                nodes[key]["live"] = nodes[key]["live"] or entry["live"]
            else:
                nodes[key] = entry
            by_name[key.split(".")[-1].split(":")[-1]].add(key)
    return nodes, by_name


def resolve(chain, table):
    """Return (service, method) if this chain lands on a service method."""
    if len(chain) < 2:
        return None
    method = chain[-1]
    holder = ALIASES.get(chain[-2], chain[-2])
    if holder in table and method in table[holder]:
        return (holder, method)
    return None


def walk(nodes, by_name, table, strict, ambiguous_cap=4):
    """BFS from every live test; union the service methods reached."""
    reached = defaultdict(set)       # (service, method) -> {test key}
    for key, entry in nodes.items():
        if not entry["live"]:
            continue
        seen = set()
        queue = [key]
        while queue:
            cur = queue.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for chain in nodes.get(cur, {}).get("calls", []):
                hit = resolve(chain, table)
                if hit:
                    reached[hit].add(key)
                    continue
                name = chain[-1]
                candidates = by_name.get(name, set())
                if not candidates:
                    continue
                if strict and len(candidates) > 1:
                    continue
                if len(candidates) > ambiguous_cap:
                    continue
                queue.extend(candidates)
    return reached


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="drop ambiguous name-only edges; reports a lower bound")
    ap.add_argument("--service", help="show one service in full")
    ap.add_argument("--json", help="write the full map here")
    args = ap.parse_args()

    table = service_table()
    total = sum(len(v) for v in table.values())

    paths = sorted(ROOT.joinpath("rootpy").rglob("*.py"))
    paths += sorted(ROOT.joinpath("tests").glob("test_live*.py"))
    paths += [ROOT / "tests" / "conftest.py"]

    nodes, by_name = collect(paths)
    reached = walk(nodes, by_name, table, args.strict)

    covered = {k for k in reached}
    print(f"{len(covered)}/{total} service methods reached by the live suite "
          f"({100 * len(covered) / total:.0f}%)"
          + ("  [strict lower bound]" if args.strict else ""))
    print()

    rows = []
    for attr in SERVICE_ATTRS:
        methods = sorted(table[attr])
        hit = [m for m in methods if (attr, m) in covered]
        miss = [m for m in methods if (attr, m) not in covered]
        rows.append((attr, hit, miss))

    width = max(len(a) for a in SERVICE_ATTRS)
    for attr, hit, miss in sorted(rows, key=lambda r: (len(r[2]) == 0, -len(r[2]))):
        flag = "OK " if not miss else "   "
        print(f"{flag}{attr:{width}s} {len(hit):3d}/{len(hit) + len(miss):<3d}"
              + ("" if not miss else "  missing: " + ", ".join(miss)))

    if args.service:
        attr = args.service
        print()
        print(f"--- {attr} ---")
        for method in sorted(table[attr]):
            tests = sorted(reached.get((attr, method), ()))
            mark = "yes" if tests else "NO "
            print(f"  {mark} {method}")
            for test in tests[:3]:
                print(f"        via {test}")

    if args.json:
        payload = {
            "total": total,
            "covered": len(covered),
            "strict": args.strict,
            "services": {
                attr: {"hit": hit, "miss": miss} for attr, hit, miss in rows
            },
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
