"""Tests for the agent loop, schema card, and both planners."""
from __future__ import annotations

import pytest

from sql_agent.db import SafeDB
from sql_agent.agent import (
    SQLAgent, TemplatePlanner, BasePlanner, build_schema_card, build_planner,
    _parse_model_action,
)


@pytest.fixture(scope="module")
def db():
    return SafeDB()


# ---- schema card --------------------------------------------------------- #
def test_schema_card_mentions_traps(db):
    card = build_schema_card(db)
    assert "product_master" in card
    assert "kind" in card
    assert "INR" in card and "USD" in card


# ---- template planner ---------------------------------------------------- #
@pytest.mark.parametrize("question,needle", [
    ("how many suppliers do we have?", "suppliers"),
    ("how many purchase orders were cancelled?", "CANCELLED"),
    ("total spend by currency", "GROUP BY currency"),
    ("inventory by warehouse", "warehouse_id"),
    ("how many shipments were late?", "promised_date"),
])
def test_template_planner_routes(db, question, needle):
    res = SQLAgent(db=db, planner=TemplatePlanner()).ask(question)
    assert res.error is None
    assert needle in res.sql


# Coverage for the paraphrased / filtered-aggregation intents added to close the
# earlier gaps: each maps to a specific template and returns a runnable row set.
@pytest.mark.parametrize("question,needle", [
    ("show me all warehouse capacities", "capacity_units"),
    ("how many shipments are there in total?", "COUNT(*) AS shipment_count FROM shipments"),
    ("which carrier handles the most shipments?", "ORDER BY shipment_count DESC LIMIT 1"),
    ("how many shipments were returned?", "status = 'RETURNED'"),
    ("what is the total freight cost by currency?", "ROUND(SUM(freight_cost)"),
    ("how many defects are critical?", "severity = 'critical'"),
    ("which warehouse holds the most inventory on hand?", "on_hand DESC LIMIT 1"),
    ("how many distinct products appear on purchase order lines?", "COUNT(DISTINCT product_id)"),
    ("which supplier has the highest rating?", "ORDER BY rating DESC LIMIT 1"),
    ("how many quality inspections failed?", "result = 'FAIL'"),
    ("what is the average unit price of products by category?", "AVG(pl.unit_price)"),
    ("how many purchase orders are still open?", "status = 'OPEN'"),
    ("what is the total value of closed purchase orders?", "status = 'CLOSED'"),
    ("total spend by supplier country", "GROUP BY s.country"),
    ("average order value", "AVG(total_amount)"),
    ("shipments by carrier", "GROUP BY carrier"),
])
def test_template_planner_routes_new_intents(db, question, needle):
    res = SQLAgent(db=db, planner=TemplatePlanner()).ask(question)
    assert res.error is None
    assert res.sql is not None and needle in res.sql
    assert res.row_count >= 1


def test_template_planner_excludes_keep_intents_separate(db):
    """A generic intent must not steal a more specific filtered one."""
    agent = SQLAgent(db=db, planner=TemplatePlanner())
    # "how many shipments" (total) vs late / returned / per-carrier
    assert "WHERE status = 'RETURNED'" in agent.ask("how many shipments were returned?").sql
    assert "promised_date" in agent.ask("how many shipments were late?").sql
    assert "GROUP BY carrier" in agent.ask("shipments by carrier").sql
    # "quality inspections failed" must not route to the results breakdown
    assert "result = 'FAIL'" in agent.ask("how many quality inspections failed?").sql
    assert "GROUP BY result" in agent.ask("show the quality inspection results breakdown.").sql


# ---- unmapped-intent fallback -------------------------------------------- #
def test_template_planner_unmapped_intent_is_explicit(db):
    res = SQLAgent(db=db, planner=TemplatePlanner()).ask("what is the meaning of life?")
    # must NOT fall back to listing tables; must report an explicit message
    assert res.error is not None
    assert "could not map" in res.error.lower()
    assert res.sql is None
    assert res.rows == []
    assert any(s.action == "final" for s in res.steps)


def test_build_planner_routing():
    assert build_planner("template").name == "template"
    # no key in the test env -> template
    assert build_planner("auto").name in ("template", "llm")


# ---- self-repair with a fake model --------------------------------------- #
class FakeRepairPlanner(BasePlanner):
    """Returns a broken SQL first, then good SQL once it sees the error."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    def propose(self, question, schema_card, transcript):
        self.calls += 1
        saw_error = any("error" in str(m.get("content", "")).lower()
                        for m in transcript)
        if not saw_error:
            return {"type": "sql", "sql": "SELECT bogus_column FROM suppliers"}
        return {"type": "sql", "sql": "SELECT COUNT(*) AS n FROM suppliers"}


def test_agent_self_repairs(db):
    planner = FakeRepairPlanner()
    res = SQLAgent(db=db, planner=planner).ask("how many suppliers?")
    assert res.error is None
    assert res.repaired is True
    assert res.row_count == 1
    assert res.rows[0]["n"] == 40
    # a repair step must appear in the trace
    assert any(s.action == "repair" for s in res.steps)
    assert planner.calls >= 2


class AlwaysBadPlanner(BasePlanner):
    name = "always_bad"

    def propose(self, question, schema_card, transcript):
        return {"type": "sql", "sql": "SELECT nonexistent FROM suppliers"}


def test_agent_gives_up_after_max_repairs(db):
    res = SQLAgent(db=db, planner=AlwaysBadPlanner(), max_repairs=3).ask("x")
    assert res.error is not None
    repairs = [s for s in res.steps if s.action == "repair"]
    assert len(repairs) <= 4  # initial + up to 3 repairs


class ToolThenSqlPlanner(BasePlanner):
    """Uses a tool first, then answers — exercises the tool-call path."""

    name = "tool_then_sql"

    def __init__(self):
        self.seen_tool_result = False

    def propose(self, question, schema_card, transcript):
        if not any("OBSERVATION" in str(m.get("content", "")) for m in transcript):
            return {"type": "tool", "tool": "describe_table",
                    "args": {"table": "warehouses"}}
        self.seen_tool_result = True
        return {"type": "sql", "sql": "SELECT COUNT(*) AS n FROM warehouses"}


def test_agent_handles_tool_calls(db):
    planner = ToolThenSqlPlanner()
    res = SQLAgent(db=db, planner=planner).ask("how many warehouses?")
    assert planner.seen_tool_result is True
    assert res.rows[0]["n"] == 6
    assert any(s.action == "tool" for s in res.steps)


# ---- LLM JSON protocol parsing ------------------------------------------ #
def test_parse_model_action_sql():
    assert _parse_model_action('{"sql": "SELECT 1"}')["type"] == "sql"


def test_parse_model_action_tool():
    out = _parse_model_action('{"tool": "list_tables", "args": {}}')
    assert out["type"] == "tool" and out["tool"] == "list_tables"


def test_parse_model_action_fenced():
    out = _parse_model_action('```json\n{"sql": "SELECT 2"}\n```')
    assert out["sql"] == "SELECT 2"


def test_parse_model_action_garbage():
    out = _parse_model_action("the answer is 42")
    assert out["type"] == "sql"
