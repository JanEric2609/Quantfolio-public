import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs/runbooks/2026-11-12-null-fundamentals-cleanup.md"


def _pattern() -> re.Pattern[str]:
    m = re.search(r"data_json ~ '([^']+)'", DOC.read_text())
    assert m
    return re.compile(m.group(1))


def test_matches_only_all_null_rows():
    p = _pattern()
    assert p.match("{}")
    assert p.match('{"pe_ratio": null, "roe": null}')
    assert not p.match('{"pe_ratio": null, "roe": 0.12}')
    assert not p.match('{"name": "Müller null"}')
