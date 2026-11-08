"""Build eval/heldout.jsonl — an independent paraphrase set.

Why this exists
---------------
`eval/questions.jsonl` was written alongside the template router, so its
questions overlap with the router's own keyword phrasing. Scoring 100% there
is a coverage check, not a benchmark: the set and the system were built
together.

This file holds 20 questions phrased the way someone would actually type them,
written *without* looking at the template keywords (deliberately avoiding the
canned phrasings), and with gold SQL that is executed against the real
database so the expected hash and row count are ground truth.

Run:  python eval/build_heldout.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sql_agent.db import SafeDB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "heldout.jsonl")

# (question, gold_sql, ordered)
QA: list[tuple[str, str, bool]] = [
    ("What share of our supplier base is inactive?",
     "SELECT ROUND(100.0 * SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END) / COUNT(*), 2) "
     "AS inactive_pct FROM suppliers", False),
    ("Which five suppliers have the weakest ratings?",
     "SELECT supplier_name, rating FROM suppliers WHERE rating IS NOT NULL "
     "ORDER BY rating ASC LIMIT 5", True),
    ("How much freight have we paid each carrier in total?",
     "SELECT carrier, ROUND(SUM(freight_cost), 2) AS freight FROM shipments "
     "GROUP BY carrier ORDER BY freight DESC", True),
    ("On average, how many days pass between placing an order and its expected date?",
     "SELECT ROUND(AVG(julianday(expected_date) - julianday(order_date)), 1) AS avg_days "
     "FROM purchase_orders WHERE expected_date IS NOT NULL", False),
    ("Which site can hold the most stock?",
     "SELECT name, capacity_units FROM warehouses ORDER BY capacity_units DESC LIMIT 1", True),
    ("How many orders were placed without an expected delivery date?",
     "SELECT COUNT(*) AS missing_expected FROM purchase_orders WHERE expected_date IS NULL", False),
    ("Which defect types affect the largest number of units?",
     "SELECT defect_type, SUM(quantity_affected) AS units FROM defects "
     "GROUP BY defect_type ORDER BY units DESC LIMIT 3", True),
    ("What percentage of inspections came back clean?",
     "SELECT ROUND(100.0 * SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / COUNT(*), 2) "
     "AS pass_pct FROM quality_inspections", False),
    ("How many stock records are sitting under their reorder point?",
     "SELECT COUNT(*) AS below_reorder FROM inventory_snapshots "
     "WHERE on_hand_qty < reorder_point", False),
    ("How many of the catalogue items are components rather than finished goods?",
     "SELECT COUNT(*) AS components FROM product_master WHERE kind = 'component'", False),
    ("What is the single most expensive finished product per unit?",
     "SELECT name, unit_cost FROM product_master WHERE kind = 'product' "
     "AND unit_cost IS NOT NULL ORDER BY unit_cost DESC LIMIT 1", True),
    ("Break the late deliveries down by carrier.",
     "SELECT carrier, COUNT(*) AS late FROM shipments "
     "WHERE delivered_date IS NOT NULL AND delivered_date > promised_date "
     "GROUP BY carrier ORDER BY late DESC", True),
    ("How many consignments have not arrived yet?",
     "SELECT COUNT(*) AS in_transit FROM shipments WHERE status = 'IN_TRANSIT'", False),
    ("What quantity did we order in total across every PO line?",
     "SELECT SUM(quantity) AS total_qty FROM po_lines", False),
    ("What fraction of order lines arrived complete?",
     "SELECT ROUND(100.0 * SUM(CASE WHEN received_qty >= quantity THEN 1 ELSE 0 END) / COUNT(*), 2) "
     "AS complete_pct FROM po_lines WHERE received_qty IS NOT NULL", False),
    ("Are there suppliers we have never ordered from?",
     "SELECT COUNT(*) AS never_ordered FROM suppliers s "
     "WHERE NOT EXISTS (SELECT 1 FROM purchase_orders po WHERE po.supplier_id = s.supplier_id)", False),
    ("On average, how many defects show up per inspection?",
     "SELECT ROUND(AVG(defect_count), 2) AS avg_defects FROM quality_inspections", False),
    ("How many orders are only partly fulfilled?",
     "SELECT COUNT(*) AS partial_pos FROM purchase_orders WHERE status = 'PARTIAL'", False),
    ("Which month saw the largest order value?",
     "SELECT substr(order_date, 1, 7) AS month, ROUND(SUM(total_amount), 2) AS value "
     "FROM purchase_orders GROUP BY month ORDER BY value DESC LIMIT 1", True),
    ("How many different logistics partners do we work with?",
     "SELECT COUNT(DISTINCT carrier) AS carriers FROM shipments", False),
]


def canon(rows: list[dict], ordered: bool) -> str:
    vals = [tuple(r.values()) for r in rows]
    if not ordered:
        vals = sorted(vals, key=lambda t: [str(x) for x in t])
    return hashlib.sha256(json.dumps(vals, default=str).encode()).hexdigest()[:16]


def main() -> None:
    db = SafeDB()
    lines = []
    for i, (question, sql, ordered) in enumerate(QA, start=1):
        res = db.run_sql(sql)
        if res["error"]:
            raise SystemExit(f"gold SQL failed for {question!r}: {res['error']}\n{sql}")
        lines.append(json.dumps({
            "id": i,
            "question": question,
            "gold_sql": sql,
            "ordered": ordered,
            "expected_row_count": res["row_count"],
            "expected_hash": canon(res["rows"], ordered),
        }, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(lines)} questions)")


if __name__ == "__main__":
    main()
