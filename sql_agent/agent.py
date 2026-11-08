"""Text-to-SQL agent for the supply-chain database.

Architecture
------------
    question ──▶ Agent ──▶ Planner ──▶ {tool call | SQL}
                    │          ▲              │
                    │          └── observation ┘  (execute / repair loop)
                    ▼
                 StepTrace  ──▶ {sql, rows, columns, steps, error}

The agent loop is planner-agnostic. Two planners ship:

* ``LLMPlanner``       – calls an OpenAI-compatible chat API from env
                         (OPENAI_API_KEY / OPENAI_BASE_URL / MODEL) and speaks a
                         strict JSON tool-call protocol.
* ``TemplatePlanner``  – deterministic, offline. Maps ~40 intents to
                         parameterised SQL by keyword so the whole system is
                         runnable and demoable with no API key. Unmapped
                         questions return an explicit unmapped-intent response
                         instead of an unrelated result set.

Routing: ``build_planner()`` picks the LLM planner when a key is present,
otherwise the template planner. Override with ``--planner``.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from .db import SafeDB

MAX_STEPS = 6
MAX_REPAIRS = 3
SYSTEM_PREAMBLE = (
    "You are a supply-chain analytics SQL agent. You answer questions about a "
    "SQLite database by writing read-only SQLite SELECT queries. Follow the "
    "schema card exactly; do not invent tables or columns."
)


# --------------------------------------------------------------------------- #
# Schema card
# --------------------------------------------------------------------------- #
def build_schema_card(db: SafeDB) -> str:
    """Compact, model-friendly description of the schema (~1/3 the tokens of
    the raw DDL). Includes row counts, column list, keys and known traps."""
    lines = [SYSTEM_PREAMBLE, "", "TABLES:"]
    for t in db.list_tables():
        d = db.describe_table(t)
        cols = ", ".join(c["name"] for c in d["columns"])
        lines.append(f"- {t}({cols})  [{d['row_count']} rows]")
        for fk in d["foreign_keys"]:
            lines.append(f"    FK: {t}.{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}")
    lines += [
        "",
        "NOTES / TRAPS:",
        "- `product_master` contains BOTH finished products AND raw components; "
        "use `kind` = 'product' or 'component' to separate them.",
        "- `currency` appears on purchase_orders, shipments, po_lines(derived), "
        "product_master and suppliers; values are INR, USD, AED.",
        "- `supplier_name` has duplicates (same name, different supplier_id). "
        "Deduplicate with COUNT(DISTINCT supplier_name) when counting names.",
        "- Some columns are NULL by design (expected_date, delivered_date, "
        "received_qty, rating, root_cause, inspector, created_by).",
        "- Late shipment: delivered_date > promised_date.",
        "- Use SQLite date functions (strftime/date) for date maths.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    step: int
    action: str           # "plan" | "tool" | "sql" | "observe" | "repair" | "final"
    detail: str
    sql: str | None = None
    error: str | None = None
    row_count: int | None = None
    latency_ms: float | None = None


@dataclass
class AgentResult:
    question: str
    planner: str
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    repaired: bool = False
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d


# --------------------------------------------------------------------------- #
# Planners
# --------------------------------------------------------------------------- #
class BasePlanner:
    name = "base"

    def propose(self, question: str, schema_card: str,
                transcript: list[dict]) -> dict:
        """Return one of:
        {"type": "sql", "sql": "..."}
        {"type": "tool", "tool": "list_tables", "args": {...}}
        {"type": "final", "sql": "..."}
        """
        raise NotImplementedError


class LLMPlanner(BasePlanner):
    """OpenAI-compatible chat planner with a JSON tool-call protocol."""

    name = "llm"

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, temperature: float = 0.0):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("MODEL", "gpt-4o-mini")
        self.temperature = temperature
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set; cannot use LLMPlanner.")

    def _protocol(self) -> str:
        return (
            "Respond with ONLY a JSON object, no prose. One of:\n"
            '  {"tool":"list_tables","args":{}}\n'
            '  {"tool":"describe_table","args":{"table":"suppliers"}}\n'
            '  {"tool":"sample_rows","args":{"table":"suppliers","n":3}}\n'
            '  {"tool":"run_sql","args":{"sql":"SELECT ..."}}\n'
            '  {"sql":"SELECT ..."}\n'
            "Use tools to inspect the schema when unsure. When you are confident, "
            'emit {"sql": "..."} with a single read-only SELECT/WITH statement. '
            "SQLite dialect. Do not wrap SQL in markdown."
        )

    def propose(self, question, schema_card, transcript):
        import requests
        messages = [{"role": "system", "content": schema_card + "\n\n" + self._protocol()}]
        messages += transcript
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "temperature": self.temperature,
                  "messages": messages, "response_format": {"type": "json_object"}},
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_model_action(content)


def _parse_model_action(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # last resort: pull the first {...} block
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return {"type": "sql", "sql": ""}
        obj = json.loads(m.group(0))
    if "tool" in obj and obj.get("tool"):
        return {"type": "tool", "tool": obj["tool"], "args": obj.get("args", {})}
    return {"type": "sql", "sql": (obj.get("sql") or "").strip()}


# --- Template planner ------------------------------------------------------ #
def _sql_total_suppliers() -> str:
    return "SELECT COUNT(*) AS supplier_count FROM suppliers"


TEMPLATES: list[dict] = [
    {
        "name": "count_suppliers",
        "keywords": ["how many suppliers", "number of suppliers", "count suppliers",
                     "total suppliers"],
        "sql": "SELECT COUNT(*) AS supplier_count FROM suppliers",
    },
    {
        "name": "active_suppliers",
        "keywords": ["active suppliers", "suppliers active", "how many active suppliers"],
        "sql": "SELECT COUNT(*) AS active_supplier_count FROM suppliers WHERE is_active = 1",
    },
    {
        "name": "inactive_suppliers_list",
        "keywords": ["inactive suppliers", "blacklisted suppliers", "suppliers inactive"],
        "sql": ("SELECT supplier_id, supplier_name, country, rating FROM suppliers "
                "WHERE is_active = 0 ORDER BY supplier_name"),
    },
    {
        "name": "suppliers_by_country",
        "keywords": ["suppliers by country", "suppliers per country",
                     "count suppliers by country", "suppliers in each country"],
        "sql": ("SELECT country, COUNT(*) AS suppliers FROM suppliers "
                "GROUP BY country ORDER BY suppliers DESC"),
    },
    {
        "name": "duplicate_supplier_names",
        "keywords": ["duplicate supplier", "repeated supplier", "same supplier name",
                     "duplicate supplier names"],
        "sql": ("SELECT supplier_name, COUNT(*) AS occurrences FROM suppliers "
                "GROUP BY supplier_name HAVING COUNT(*) > 1 ORDER BY occurrences DESC"),
    },
    {
        "name": "list_warehouses",
        "keywords": ["list warehouses", "how many warehouses", "all warehouses",
                     "warehouses do we have", "list all warehouses"],
        "sql": ("SELECT warehouse_id, name, city, country, capacity_units "
                "FROM warehouses ORDER BY warehouse_id"),
    },
    {
        "name": "count_purchase_orders",
        "keywords": ["how many purchase orders", "total purchase orders",
                     "number of purchase orders", "count purchase orders",
                     "count of purchase orders", "how many pos",
                     "purchase orders are there"],
        "exclude": ["cancel", "open", "closed", "still", "status", "value", "spend"],
        "sql": "SELECT COUNT(*) AS po_count FROM purchase_orders",
    },
    {
        "name": "cancelled_pos",
        "keywords": ["cancelled purchase orders", "cancelled pos", "how many cancelled",
                     "cancelled orders", "cancelled", "canceled"],
        "sql": ("SELECT COUNT(*) AS cancelled_pos FROM purchase_orders "
                "WHERE status = 'CANCELLED'"),
    },
    {
        "name": "pos_by_status",
        "keywords": ["purchase orders by status", "pos by status", "order status breakdown",
                     "breakdown of purchase orders"],
        "sql": ("SELECT status, COUNT(*) AS po_count FROM purchase_orders "
                "GROUP BY status ORDER BY po_count DESC"),
    },
    {
        "name": "total_spend",
        "keywords": ["total spend", "total amount", "sum of purchase orders",
                     "total value of purchase orders", "total po value"],
        "sql": "SELECT ROUND(SUM(total_amount), 2) AS total_spend FROM purchase_orders",
    },
    {
        "name": "spend_by_currency",
        "keywords": ["spend by currency", "total by currency", "amount by currency",
                     "currency breakdown"],
        "sql": ("SELECT currency, ROUND(SUM(total_amount), 2) AS total_amount, "
                "COUNT(*) AS po_count FROM purchase_orders GROUP BY currency "
                "ORDER BY total_amount DESC"),
    },
    {
        "name": "top_suppliers_by_spend",
        "keywords": ["top suppliers by spend", "suppliers by spend",
                     "biggest suppliers", "largest suppliers by spend",
                     "top suppliers"],
        "sql": ("SELECT s.supplier_name, ROUND(SUM(po.total_amount), 2) AS spend "
                "FROM purchase_orders po JOIN suppliers s "
                "ON po.supplier_id = s.supplier_id "
                "GROUP BY s.supplier_name ORDER BY spend DESC LIMIT 10"),
    },
    {
        "name": "late_shipments",
        "keywords": ["late shipments", "delayed shipments", "how many late",
                     "shipments late", "shipments were late", "were late",
                     "late deliveries", "delayed deliveries"],
        "sql": ("SELECT COUNT(*) AS late_shipments FROM shipments "
                "WHERE delivered_date IS NOT NULL AND delivered_date > promised_date"),
    },
    {
        "name": "shipments_by_status",
        "keywords": ["shipments by status", "shipment status breakdown",
                     "status of shipments", "breakdown of shipments",
                     "shipment status counts", "shipment status"],
        "sql": ("SELECT status, COUNT(*) AS shipment_count FROM shipments "
                "GROUP BY status ORDER BY shipment_count DESC"),
    },
    {
        "name": "on_time_delivery_rate",
        "keywords": ["on time delivery", "on-time delivery rate", "otif",
                     "delivery performance", "on time performance"],
        "sql": ("SELECT ROUND(100.0 * SUM(CASE WHEN delivered_date <= promised_date "
                "THEN 1 ELSE 0 END) / COUNT(*), 2) AS on_time_pct FROM shipments "
                "WHERE delivered_date IS NOT NULL"),
    },
    {
        "name": "avg_freight_cost",
        "keywords": ["average freight", "avg freight", "mean freight cost",
                     "average shipping cost"],
        "sql": "SELECT ROUND(AVG(freight_cost), 2) AS avg_freight FROM shipments",
    },
    {
        "name": "inventory_by_warehouse",
        "keywords": ["inventory by warehouse", "stock by warehouse",
                     "on hand by warehouse", "inventory per warehouse"],
        "sql": ("SELECT w.name AS warehouse, SUM(i.on_hand_qty) AS on_hand "
                "FROM inventory_snapshots i JOIN warehouses w "
                "ON i.warehouse_id = w.warehouse_id "
                "GROUP BY w.name ORDER BY on_hand DESC"),
    },
    {
        "name": "below_reorder_point",
        "keywords": ["below reorder", "reorder point", "low stock", "under reorder"],
        "sql": ("SELECT product_id, warehouse_id, snapshot_date, on_hand_qty, "
                "reorder_point FROM inventory_snapshots "
                "WHERE on_hand_qty < reorder_point "
                "ORDER BY on_hand_qty - reorder_point LIMIT 20"),
    },
    {
        "name": "inspection_results",
        "keywords": ["inspection results", "quality inspections", "inspection breakdown",
                     "pass fail", "quality results"],
        # "quality inspections failed" is a filtered count, not the breakdown
        "exclude": ["failed"],
        "sql": ("SELECT result, COUNT(*) AS inspections FROM quality_inspections "
                "GROUP BY result ORDER BY inspections DESC"),
    },
    {
        "name": "defects_by_severity",
        "keywords": ["defects by severity", "defect severity", "severity breakdown",
                     "defects severity"],
        "sql": ("SELECT severity, COUNT(*) AS defects, SUM(quantity_affected) AS units "
                "FROM defects GROUP BY severity ORDER BY defects DESC"),
    },
    {
        "name": "top_defect_types",
        "keywords": ["top defect types", "most common defect", "defect types",
                     "common defects"],
        "sql": ("SELECT defect_type, COUNT(*) AS occurrences FROM defects "
                "GROUP BY defect_type ORDER BY occurrences DESC"),
    },
    {
        "name": "worst_supplier_ratings",
        "keywords": ["worst suppliers", "lowest rated suppliers", "supplier ratings",
                     "lowest rating", "worst ratings"],
        "sql": ("SELECT supplier_name, rating FROM suppliers "
                "WHERE rating IS NOT NULL ORDER BY rating ASC LIMIT 10"),
    },
    {
        "name": "avg_lead_time_by_category",
        "keywords": ["lead time by category", "average lead time", "lead times",
                     "avg lead time"],
        "sql": ("SELECT category, ROUND(AVG(lead_time_days), 1) AS avg_lead_days "
                "FROM product_master GROUP BY category ORDER BY avg_lead_days DESC"),
    },
    {
        "name": "products_vs_components",
        "keywords": ["products and components", "how many products", "how many components",
                     "product master", "finished products"],
        "sql": ("SELECT kind, COUNT(*) AS n FROM product_master GROUP BY kind"),
    },
    # --- warehouse capacity ------------------------------------------------- #
    {
        "name": "warehouse_capacities",
        "keywords": ["warehouse capacities", "warehouse capacity", "capacities of warehouses",
                     "storage capacity"],
        "sql": ("SELECT warehouse_id, name, capacity_units FROM warehouses "
                "ORDER BY capacity_units DESC"),
    },
    # --- shipments: totals / extremes / status filters ---------------------- #
    {
        "name": "count_shipments",
        "keywords": ["how many shipments", "number of shipments", "total shipments",
                     "count shipments", "shipments are there"],
        # not the late / returned / per-status / per-carrier questions
        "exclude": ["late", "delayed", "returned", "status", "carrier", "by",
                    "most", "in transit", "cancelled"],
        "sql": "SELECT COUNT(*) AS shipment_count FROM shipments",
    },
    {
        "name": "top_carrier",
        "keywords": ["which carrier", "carrier handles the most", "most shipments",
                     "top carrier", "busiest carrier"],
        "sql": ("SELECT carrier, COUNT(*) AS shipment_count FROM shipments "
                "GROUP BY carrier ORDER BY shipment_count DESC LIMIT 1"),
    },
    {
        "name": "shipments_by_carrier",
        "keywords": ["shipments by carrier", "shipments per carrier", "carrier breakdown"],
        "sql": ("SELECT carrier, COUNT(*) AS shipment_count FROM shipments "
                "GROUP BY carrier ORDER BY shipment_count DESC"),
    },
    {
        "name": "returned_shipments",
        "keywords": ["returned shipments", "shipments were returned", "shipments returned",
                     "how many shipments were returned", "returned"],
        "sql": ("SELECT COUNT(*) AS returned FROM shipments "
                "WHERE status = 'RETURNED'"),
    },
    {
        "name": "freight_by_currency",
        "keywords": ["freight cost by currency", "freight by currency",
                     "total freight cost by currency", "freight cost per currency"],
        "sql": ("SELECT currency, ROUND(SUM(freight_cost), 2) AS freight FROM shipments "
                "GROUP BY currency ORDER BY freight DESC"),
    },
    # --- defects: filtered count -------------------------------------------- #
    {
        "name": "critical_defects",
        "keywords": ["defects are critical", "critical defects", "defects critical",
                     "how many defects are critical"],
        "sql": ("SELECT COUNT(*) AS critical_defects FROM defects "
                "WHERE severity = 'critical'"),
    },
    # --- inventory: extreme value ------------------------------------------- #
    {
        "name": "top_warehouse_inventory",
        "keywords": ["most inventory", "holds the most", "most inventory on hand",
                     "which warehouse holds"],
        "sql": ("SELECT w.name AS warehouse, SUM(i.on_hand_qty) AS on_hand "
                "FROM inventory_snapshots i JOIN warehouses w "
                "ON i.warehouse_id = w.warehouse_id "
                "GROUP BY w.name ORDER BY on_hand DESC LIMIT 1"),
    },
    # --- po_lines: distinct products ---------------------------------------- #
    {
        "name": "distinct_products_on_po_lines",
        "keywords": ["distinct products", "products appear on purchase order lines",
                     "distinct products on purchase order lines", "on purchase order lines"],
        "sql": "SELECT COUNT(DISTINCT product_id) AS distinct_products FROM po_lines",
    },
    # --- suppliers: extreme rating ------------------------------------------ #
    {
        "name": "highest_supplier_rating",
        "keywords": ["highest rating", "highest-rated supplier", "supplier has the highest",
                     "best rated supplier", "top rated supplier", "highest rated supplier"],
        "sql": ("SELECT supplier_name, rating FROM suppliers "
                "ORDER BY rating DESC LIMIT 1"),
    },
    # --- quality_inspections: filtered count -------------------------------- #
    {
        "name": "failed_inspections",
        "keywords": ["inspections failed", "failed inspections", "quality inspections failed",
                     "how many inspections failed", "inspections fail"],
        "sql": ("SELECT COUNT(*) AS failed FROM quality_inspections "
                "WHERE result = 'FAIL'"),
    },
    # --- po_lines x product_master: average unit price by category ---------- #
    {
        "name": "avg_unit_price_by_category",
        "keywords": ["average unit price", "avg unit price", "unit price by category",
                     "average unit price of products"],
        "sql": ("SELECT pm.category, ROUND(AVG(pl.unit_price), 2) AS avg_unit_price "
                "FROM po_lines pl JOIN product_master pm "
                "ON pl.product_id = pm.product_id "
                "GROUP BY pm.category ORDER BY avg_unit_price DESC"),
    },
    # --- purchase_orders: filtered counts / values -------------------------- #
    {
        "name": "open_pos",
        "keywords": ["open purchase orders", "purchase orders are still open",
                     "purchase orders are open", "still open", "open pos",
                     "how many purchase orders are open"],
        "sql": ("SELECT COUNT(*) AS open_pos FROM purchase_orders "
                "WHERE status = 'OPEN'"),
    },
    {
        "name": "closed_po_value",
        "keywords": ["total value of closed", "value of closed purchase orders",
                     "closed purchase orders", "closed pos value"],
        "sql": ("SELECT ROUND(SUM(total_amount), 2) AS closed_value FROM purchase_orders "
                "WHERE status = 'CLOSED'"),
    },
    # --- additional obvious supply-chain intents ---------------------------- #
    {
        "name": "spend_by_supplier_country",
        "keywords": ["spend by country", "spend by supplier country",
                     "supplier country spend", "spend by region"],
        "sql": ("SELECT s.country, ROUND(SUM(po.total_amount), 2) AS spend "
                "FROM purchase_orders po JOIN suppliers s "
                "ON po.supplier_id = s.supplier_id "
                "GROUP BY s.country ORDER BY spend DESC"),
    },
    {
        "name": "avg_order_value",
        "keywords": ["average order value", "avg order value",
                     "average purchase order value", "average po value"],
        "sql": "SELECT ROUND(AVG(total_amount), 2) AS avg_order_value FROM purchase_orders",
    },
]


class TemplatePlanner(BasePlanner):
    """Deterministic offline planner: keyword routing to canned SQL.

    Each template lists ``keywords`` (any substring hit counts) and optional
    ``exclude`` tokens that veto the template so a generic intent does not steal
    a more specific filtered one. Best match wins by hit count and matched
    phrase length. When nothing matches, the planner returns a ``final`` action
    carrying an explicit unmapped-intent message rather than unrelated SQL.
    """

    name = "template"

    def propose(self, question, schema_card, transcript):
        # If we already tried and got an error, give up gracefully (no better
        # deterministic answer exists) so the agent reports the failure.
        if any(m.get("role") == "user" and "error" in str(m.get("content", ""))
               for m in transcript[-2:]):
            return {"type": "sql", "sql": ""}
        q = (question or "").lower()
        best, best_score = None, 0
        for t in TEMPLATES:
            # skip a template if the question contains one of its exclusion
            # tokens (used so a generic intent, e.g. "count all POs", does not
            # swallow a filtered one, e.g. "how many POs are still open")
            if any(x in q for x in t.get("exclude", ())):
                continue
            hits = [kw for kw in t["keywords"] if kw in q]
            if not hits:
                continue
            # score = number of hits + total matched keyword length, so a more
            # specific (longer) phrase like "spend by currency" beats the
            # generic "total spend" on a tie.
            score = len(hits) + sum(len(kw) for kw in hits) / 100.0
            if score > best_score:
                best, best_score = t, score
        if best is None:
            # No template matched. Be explicit that the offline planner cannot
            # map this question rather than returning an unrelated result set
            # (e.g. sqlite_master) that would look like a real answer.
            return {
                "type": "final",
                "message": (
                    "The offline template planner could not map this question "
                    "to a known intent. Set OPENAI_API_KEY (or pass "
                    "--planner llm) to use the LLM planner for open-ended "
                    "questions, or rephrase using a supported metric."
                ),
            }
        return {"type": "sql", "sql": best["sql"]}


def build_planner(which: str | None = None) -> BasePlanner:
    """Route: explicit name wins; else LLM if key present; else template."""
    if which == "template":
        return TemplatePlanner()
    if which == "llm":
        return LLMPlanner()
    if which in (None, "auto"):
        if os.environ.get("OPENAI_API_KEY"):
            try:
                return LLMPlanner()
            except RuntimeError:
                pass
        return TemplatePlanner()
    raise ValueError(f"Unknown planner: {which!r}")


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class SQLAgent:
    def __init__(self, db: SafeDB | None = None, planner: BasePlanner | None = None,
                 *, max_steps: int = MAX_STEPS, max_repairs: int = MAX_REPAIRS):
        self.db = db or SafeDB()
        self.planner = planner or build_planner()
        self.schema_card = build_schema_card(self.db)
        self.max_steps = max_steps
        self.max_repairs = max_repairs

    # -- tools exposed to planners ----------------------------------------
    def call_tool(self, name: str, args: dict) -> dict:
        if name == "list_tables":
            return {"tables": self.db.list_tables()}
        if name == "describe_table":
            return self.db.describe_table(args.get("table", ""))
        if name == "sample_rows":
            return self.db.sample_rows(args.get("table", ""), args.get("n", 3))
        if name == "run_sql":
            return self.db.run_sql(args.get("sql", ""))
        return {"error": f"Unknown tool: {name}"}

    TOOLS = ["list_tables", "describe_table", "run_sql", "sample_rows"]

    def ask(self, question: str, planner: BasePlanner | None = None) -> AgentResult:
        planner = planner or self.planner
        t0 = time.monotonic()
        res = AgentResult(question=question, planner=planner.name)
        transcript: list[dict] = [{"role": "user", "content": question}]
        steps: list[Step] = []
        repairs = 0
        last_sql = None

        for step_no in range(1, self.max_steps + 1):
            action = planner.propose(question, self.schema_card, transcript)

            if action.get("type") == "tool":
                tool = action.get("tool")
                out = self.call_tool(tool, action.get("args", {}))
                steps.append(Step(step_no, "tool", f"{tool}({json.dumps(action.get('args', {}))})"))
                transcript.append({"role": "assistant",
                                   "content": json.dumps({"tool": tool})})
                transcript.append({"role": "user",
                                   "content": "OBSERVATION: " + json.dumps(out, default=str)[:2000]})
                continue

            if action.get("type") == "final":
                # planner has explicitly given up (e.g. unmapped intent)
                msg = action.get("message") or "The planner stopped without SQL."
                steps.append(Step(step_no, "final", msg))
                res.error = msg
                break

            sql = (action.get("sql") or "").strip()
            if not sql:
                steps.append(Step(step_no, "final", "no SQL produced"))
                res.error = ("The planner could not produce SQL for this question. "
                             "Try rephrasing or check the schema.")
                break

            last_sql = sql
            out = self.db.run_sql(sql)
            steps.append(Step(step_no, "sql", "execute SQL", sql=sql,
                              error=out["error"], row_count=out["row_count"],
                              latency_ms=out["latency_ms"]))

            if out["error"] is None:
                # success (empty result is still valid SQL)
                res.sql = sql
                res.columns = out["columns"]
                res.rows = out["rows"]
                res.row_count = out["row_count"]
                steps.append(Step(step_no, "final",
                                  f"returned {out['row_count']} row(s)"))
                break

            # ---- error path: repair ----
            repairs += 1
            res.repaired = True
            steps.append(Step(step_no, "repair", f"repair attempt {repairs}: {out['error']}",
                              error=out["error"]))
            if repairs > self.max_repairs:
                res.error = out["error"]
                res.sql = sql
                break
            transcript.append({"role": "assistant",
                               "content": json.dumps({"sql": sql})})
            transcript.append({"role": "user",
                               "content": f"OBSERVATION: SQL error: {out['error']}. "
                                          "Fix the SQL and try again."})
        else:
            res.error = res.error or "Reached maximum agent steps without success."

        if res.sql is None and last_sql is not None and res.error:
            res.sql = last_sql

        res.steps = steps
        res.latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return res


def ask(question: str, *, planner: str | None = None, db_path: str | None = None) -> AgentResult:
    db = SafeDB(db_path) if db_path else SafeDB()
    agent = SQLAgent(db=db, planner=build_planner(planner))
    return agent.ask(question)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Ask the supply-chain SQL agent")
    ap.add_argument("question", nargs="*", help="natural-language question")
    ap.add_argument("--planner", choices=["auto", "llm", "template"], default="auto")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    q = " ".join(a.question) or "how many late shipments are there?"
    r = ask(q, planner=a.planner)
    if a.json:
        print(json.dumps(r.to_dict(), indent=2, default=str))
    else:
        print(f"Q: {r.question}\nplanner={r.planner}  rows={r.row_count}  "
              f"latency={r.latency_ms}ms  error={r.error}")
        print("SQL:", r.sql)
        for row in r.rows[:20]:
            print("  ", row)
