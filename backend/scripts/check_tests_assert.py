"""Fail on a test function that asserts nothing (plan 2026-11-12, WP12 item 2).

A test passes when it contains an ``assert`` statement, a ``pytest.raises``/``warns``/``fail``/``skip``/``xfail``/
``approx`` call, any call whose name starts with ``assert`` (``mock.assert_called``, ``assert_frame_equal``,
``self.assertEqual``), or a ``raise``. Helpers a test delegates to by name (``_check_*``/``_assert_*``/
``_verify_*``/``_expect*``) count too. The tests that predate this check are listed in
``tests_without_assertion_golden.json``; the list may only shrink (``--regenerate`` rewrites it).

    python scripts/check_tests_assert.py [--regenerate]
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
GOLDEN = Path(__file__).resolve().parent / "tests_without_assertion_golden.json"
_PYTEST_CALLS = {"raises", "warns", "fail", "skip", "xfail", "approx", "importorskip", "deprecated_call"}
_DELEGATE_PREFIXES = ("_assert", "_check", "_verify", "_expect", "assert", "check_", "verify_", "expect_")


def _asserts(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert | ast.Raise):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _PYTEST_CALLS or name.startswith(_DELEGATE_PREFIXES):
                return True
    return False


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A helper named ``test_*`` that is a pytest fixture is not a test."""
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        name = target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(target, ast.Name) else ""
        if name == "fixture":
            return True
    return False


def _test_defs(tree: ast.Module):
    """Module-level functions and class methods; a function nested in a test (a dependency override) is no test."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield member


def find_offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in _test_defs(tree):
            if node.name.startswith("test"):
                if _is_fixture(node):
                    continue
                if not _asserts(node):
                    out.append(f"{path.relative_to(ROOT)}::{node.name}")
    return sorted(set(out))


def main(argv: list[str]) -> int:
    found = find_offenders()
    if "--regenerate" in argv:
        GOLDEN.write_text(json.dumps(found, indent=1) + "\n")
        print(f"wrote {len(found)} entries")
        return 0
    allowed = set(json.loads(GOLDEN.read_text())) if GOLDEN.exists() else set()
    new = [name for name in found if name not in allowed]
    gone = sorted(allowed - set(found))
    if new:
        print("Tests without an assertion (add one, or use pytest.raises):")
        print("\n".join(f"  {name}" for name in new))
        return 1
    if gone:
        print(f"{len(gone)} listed test(s) now assert something; run with --regenerate to shrink the list.")
        return 1
    print(f"ok: {len(found)} legacy test(s) without an assertion, none new")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
