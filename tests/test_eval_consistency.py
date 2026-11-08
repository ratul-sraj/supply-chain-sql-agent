"""Consistency tests: every eval question's gold SQL must still run against
the live database and reproduce its recorded hash / row count. This keeps the
eval set honest as the schema evolves."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_agent.db import SafeDB  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUESTIONS = os.path.join(ROOT, "eval", "questions.jsonl")


def _canonical_hash(rows, ordered):
    vals = [tuple(r.values()) for r in rows]
    if not ordered:
        vals = sorted(vals, key=lambda t: [str(x) for x in t])
    return hashlib.sha256(json.dumps(vals, default=str).encode()).hexdigest()[:16]


def _questions():
    with open(QUESTIONS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_question_count_is_40():
    assert len(_questions()) == 40


def test_questions_have_required_fields():
    for q in _questions():
        for field in ("id", "question", "gold_sql", "ordered",
                      "expected_row_count", "expected_hash"):
            assert field in q, f"Q{q.get('id')} missing {field}"


@pytest.mark.parametrize("q", _questions(), ids=lambda q: f"q{q['id']}")
def test_gold_sql_matches_recorded_hash(q):
    db = SafeDB()
    res = db.run_sql(q["gold_sql"])
    assert res["error"] is None, f"gold SQL failed: {res['error']}"
    assert res["row_count"] == q["expected_row_count"], "row count drifted"
    assert _canonical_hash(res["rows"], q["ordered"]) == q["expected_hash"], \
        "result hash drifted"
