"""WP15.3: the report A/B set builds offline and scores a model answer with the production checks."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("llm_batch_bench", Path(__file__).resolve().parent.parent / "scripts" / "llm_batch_bench.py")


def _bench():
    mod = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(mod)
    return mod


def test_set_is_seeded_and_scores_a_clean_and_a_planted_answer(tmp_path):
    bench = _bench()
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    bench.report_build(a, n=6)
    bench.report_build(b, n=6)
    assert a.read_text() == b.read_text()
    item = json.loads(a.read_text())["items"][0]
    assert "Executive Summary" in item["messages_by_section"]
    clean = bench.report_score(item, "Conclusion", "A large publisher with a moderate record.")
    assert clean["outcome"] == "ok"
    bad = bench.report_score(item, "Conclusion", "Free cash flow is exceptional and revenue grew 99.9 %.")
    assert bad["outcome"] == "rejected" and bad["problems"]
