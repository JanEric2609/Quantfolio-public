"""Extinction guard for the retired experimental feature gate (audit-fixes-2026-08 todo 17).

The gate was removed end-to-end in commit 20837f7 (deletions-only doctrine;
rollback = git revert — there is deliberately no runtime toggle). This module
is a pure filesystem scan over ``backend/app/**`` and ``frontend/src/**`` so
it keeps enforcing extinction even if the scanned modules are renamed,
broken, or unimportable. Reintroducing any guarded string anywhere in app
code fails the suite.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = (
    "experimental_features_enabled",
    "require_experimental",
    "ExperimentalBanner",
)

SCAN_ROOTS = ("backend/app", "frontend/src")

_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".map"}


def _scan(root: Path) -> list[str]:
    """Return one ``"<location>: <token>"`` hit per forbidden occurrence."""
    hits: list[str] = []
    if not root.exists():
        return hits
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in FORBIDDEN:
            if token in text:
                try:
                    location = str(path.relative_to(REPO_ROOT))
                except ValueError:
                    location = str(path)
                hits.append(f"{location}: {token}")
    return hits


def test_no_experimental_gate_strings_remain():
    hits: list[str] = []
    for rel in SCAN_ROOTS:
        hits.extend(_scan(REPO_ROOT / rel))
    assert hits == [], f"retired experimental-gate strings reintroduced: {hits}"


def test_guard_catches_reintroduction(tmp_path):
    (tmp_path / "svc.py").write_text("flag = experimental_features_enabled\n")
    (tmp_path / "dep.py").write_text("def require_experimental(db): ...\n")
    (tmp_path / "Banner.tsx").write_text("export const ExperimentalBanner = () => null;\n")
    (tmp_path / "clean.py").write_text("print('nothing to see here')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.ts").write_text("// ExperimentalBanner import\n")

    hits = _scan(tmp_path)

    assert len(hits) == 4
    assert {hit.split(": ", 1)[1] for hit in hits} == set(FORBIDDEN)
