"""Ledger of broad ``except Exception`` handlers in decision/, lab/ and worker.py (WP12.7).

The count may only shrink, like the import-cycle ledger. A *silent* handler is a
broad one whose body neither logs, re-raises, records an error nor does anything
but pass/continue/return a constant: those hide failures and must stay at zero.

    python scripts/check_broad_excepts.py              # check against the golden
    python scripts/check_broad_excepts.py --regenerate # after removing handlers
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
GOLDEN = Path(__file__).with_name("broad_excepts_golden.json")
SCOPE = ["decision", "lab", "worker.py"]
LOG_NAMES = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
    return any(isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names)


def _is_silent(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in LOG_NAMES or name.startswith(("record", "_record", "log", "_log", "report", "_report")):
                return False
    for stmt in handler.body:
        if isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(stmt, ast.Return) and (stmt.value is None or isinstance(stmt.value, ast.Constant)
                                             or isinstance(stmt.value, (ast.List, ast.Dict, ast.Tuple)) and not getattr(stmt.value, "elts", getattr(stmt.value, "keys", []))):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


def scan() -> dict:
    total, silent = 0, []
    for entry in SCOPE:
        base = ROOT / entry
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and _is_broad(node):
                    total += 1
                    if _is_silent(node):
                        silent.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return {"broad": total, "silent": sorted(silent)}


def main() -> int:
    now = scan()
    if "--regenerate" in sys.argv:
        GOLDEN.write_text(json.dumps({"broad": now["broad"], "silent": len(now["silent"])}, indent=2) + "\n")
        print(f"regenerated: {now['broad']} broad, {len(now['silent'])} silent")
        return 0
    gold = json.loads(GOLDEN.read_text())
    bad = False
    if now["broad"] > gold["broad"]:
        print(f"FAIL: {now['broad']} broad `except Exception` handlers, golden allows {gold['broad']}. Catch the specific error, or log and re-raise.")
        bad = True
    if len(now["silent"]) > gold["silent"]:
        print(f"FAIL: {len(now['silent'])} silent broad handlers, golden allows {gold['silent']}:")
        print("\n".join("  " + s for s in now["silent"]))
        bad = True
    if not bad:
        print(f"OK: {now['broad']} broad (golden {gold['broad']}), {len(now['silent'])} silent (golden {gold['silent']})")
        if now["broad"] < gold["broad"] or len(now["silent"]) < gold["silent"]:
            print("The count shrank: run with --regenerate to lock it in.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
