#!/usr/bin/env python3
"""No-NEW-import-cycles gate (Refactor Wave 0, unit U2).

Builds a grimp import graph of the ``app`` package, computes strongly
connected components (size > 1) at module level and aggregated to package
level, and compares them against the golden ledger in
``scripts/import_cycles_golden.json``.

Exit codes:
  0 — no new cycles (disappeared golden cycles only WARN; they are progress)
  1 — at least one cycle exists that is not in the golden set

The golden file is generated from the audited baseline state (six known
cycle groups). Regenerate deliberately after an approved refactor with:

    cd backend && .venv/bin/python scripts/check_no_new_cycles.py --regenerate

Grimp analyses imports statically (AST), so no database or network access
happens here and DATABASE_URL is irrelevant.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import grimp

GOLDEN_PATH = Path(__file__).parent / "import_cycles_golden.json"

# ADR 0015 Phase 5 four-layer tree (app/{foundation,lab,decision,interface}/).
_LAYERS = frozenset({"foundation", "lab", "decision", "interface"})


def _package_of(module: str) -> str:
    """Aggregate a module to its reporting package.

    app.decision.advisor.cycle -> app.decision.advisor ;
    app.interface.api.quant.portfolio -> app.interface.api ; shallow modules
    keep their full name. Deterministic and purely lexical.

    ADR 0015 Phase 5: the four-layer tree adds one nesting level, so the
    layer names are the aggregation pivot where ``services`` used to be.
    """
    parts = module.split(".")
    if len(parts) > 3 and parts[0] == "app" and parts[1] in _LAYERS:
        return ".".join(parts[:3])
    if len(parts) > 2:
        return ".".join(parts[:2])
    return module


def _sccs(nodes: list[str], edges: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """Iterative Tarjan SCC; returns components with size > 1, sorted."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = lowlink[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for succ in it:
                if succ not in index:
                    index[succ] = lowlink[succ] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(sorted(edges.get(succ, ())))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))

    return sorted(tuple(c) for c in result)


def compute_current_state() -> dict[str, list[list[str]]]:
    graph = grimp.build_graph("app")
    internal = [m for m in graph.modules if m == "app" or m.startswith("app.")]

    module_edges: dict[str, set[str]] = {m: set() for m in internal}
    pkg_edges: dict[str, set[str]] = {}
    for importer in internal:
        imp_pkg = _package_of(importer)
        for imported in graph.find_modules_directly_imported_by(importer):
            if imported in graph.modules and imported.startswith("app"):
                module_edges[importer].add(imported)
                pkg_edges.setdefault(imp_pkg, set()).add(_package_of(imported))

    module_cycles = sorted(list(c) for c in _sccs(internal, module_edges))
    pkg_nodes = sorted(pkg_edges)
    package_cycles = sorted(list(c) for c in _sccs(pkg_nodes, pkg_edges))
    return {"module_cycles": module_cycles, "package_cycles": package_cycles}


def _format_cycles(cycles: list[list[str]]) -> str:
    return "\n".join(
        f"  {' <-> '.join(cycle)}" for cycle in cycles
    ) or "  (none)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="overwrite the golden ledger with the current state and exit 0",
    )
    args = parser.parse_args()

    current = compute_current_state()

    if args.regenerate:
        GOLDEN_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"Golden ledger regenerated at {GOLDEN_PATH}")
        print(f"  module cycles:  {len(current['module_cycles'])}")
        print(f"  package cycles: {len(current['package_cycles'])}")
        return 0

    golden = json.loads(GOLDEN_PATH.read_text())
    golden_module = {tuple(c) for c in golden["module_cycles"]}
    golden_pkg = {tuple(c) for c in golden["package_cycles"]}
    current_module = {tuple(c) for c in current["module_cycles"]}
    current_pkg = {tuple(c) for c in current["package_cycles"]}

    new_cycles = sorted(current_module - golden_module) + sorted(
        current_pkg - golden_pkg
    )
    disappeared = sorted(golden_module - current_module) + sorted(
        golden_pkg - current_pkg
    )

    if new_cycles:
        print("FAIL: new import cycle(s) not in the golden ledger:\n")
        print(_format_cycles([list(c) for c in new_cycles]))
        print(
            "\nIf this cycle is an approved refactor outcome, regenerate the "
            "ledger deliberately:\n"
            "  cd backend && .venv/bin/python scripts/check_no_new_cycles.py --regenerate"
        )
        return 1

    if disappeared:
        print("WARN: golden cycle(s) no longer present (progress — prune the ledger):\n")
        print(_format_cycles([list(c) for c in disappeared]))

    print(
        f"OK: no new import cycles "
        f"(module SCCs: {len(current_module)}, package SCCs: {len(current_pkg)})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
