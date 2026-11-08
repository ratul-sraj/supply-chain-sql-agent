"""API tests via FastAPI's TestClient."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["tables"] == 9


def test_schema():
    r = client.get("/schema")
    assert r.status_code == 200
    body = r.json()
    assert "schema_card" in body
    assert "suppliers" in body["tables"]


def test_ask_returns_full_payload():
    r = client.post("/ask", json={"question": "how many suppliers?", "planner": "template"})
    assert r.status_code == 200
    body = r.json()
    for key in ("question", "sql", "rows", "columns", "steps", "latency_ms", "error"):
        assert key in body
    assert body["error"] is None
    assert body["rows"][0]["supplier_count"] == 40
    assert isinstance(body["steps"], list) and body["steps"]


def test_ask_default_planner_routes_offline():
    r = client.post("/ask", json={"question": "total spend by currency"})
    assert r.status_code == 200
    assert r.json()["row_count"] >= 1


def test_ask_rejects_empty_question():
    r = client.post("/ask", json={"question": ""})
    assert r.status_code == 422


def test_ask_rejects_bad_planner():
    r = client.post("/ask", json={"question": "x", "planner": "nope"})
    assert r.status_code == 422


def test_ask_unmapped_intent_returns_clean_error():
    r = client.post("/ask", json={"question": "what is the meaning of life?",
                                  "planner": "template"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is not None
    assert body["sql"] is None
    assert body["rows"] == []
    assert body["columns"] == []
