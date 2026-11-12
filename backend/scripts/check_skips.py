"""Report the skipped tests of a pytest run and fail on more than the budget (plan 2026-11-12, WP12 item 3).

    python scripts/check_skips.py junit.xml [--regenerate]

``skips_budget.json`` holds ``max_skips``, the number of skips the suite is known to have in CI (optional
imports, tests that need Redis, platform guards). A new skip raises the count and fails the job, so a test
cannot quietly stop running. After removing skips, run with ``--regenerate`` to lower the budget.
"""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

BUDGET = Path(__file__).resolve().parent / "skips_budget.json"


def skipped(junit: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        skip = case.find("skipped")
        if skip is not None:
            out.append((f"{case.get('classname')}::{case.get('name')}", (skip.get("message") or "").strip()[:120]))
    return out


def main(argv: list[str]) -> int:
    path = Path(argv[0])
    found = skipped(path)
    reasons = Counter(reason for _, reason in found)
    print(f"{len(found)} skipped test(s):")
    for reason, count in reasons.most_common():
        print(f"  {count:4d}  {reason or '(no reason)'}")
    if "--regenerate" in argv:
        BUDGET.write_text(json.dumps({"max_skips": len(found)}, indent=1) + "\n")
        print(f"budget set to {len(found)}")
        return 0
    budget = int(json.loads(BUDGET.read_text())["max_skips"]) if BUDGET.exists() else 0
    if len(found) > budget:
        print(f"FAIL: {len(found)} skips, budget {budget}. A test stopped running; fix it or raise the budget deliberately.")
        return 1
    if len(found) < budget:
        print(f"note: {len(found)} skips, budget {budget}; run with --regenerate to lower it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
