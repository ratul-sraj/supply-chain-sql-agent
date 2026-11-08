"""Generate eval/questions.jsonl from a curated list of (question, gold SQL)
pairs. The gold SQL is executed against the *real* database so the recorded
row_count and result hash are ground truth, not hand-written.

Run:  python eval/_gen_questions.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sql_agent.db import SafeDB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "questions.jsonl")

# (question, gold_sql, ordered?)  -- ordered=True => result order matters
QA: list[tuple[str, str, bool]] = [
    # --- template-aligned -------------------------------------------------
    ("How many suppliers do we have?",
     "SELECT COUNT(*) AS supplier_count FROM suppliers", False),
    ("How many active suppliers are there?",
     "SELECT COUNT(*) AS active_supplier_count FROM suppliers WHERE is_active = 1", False),
    ("List the inactive suppliers.",
     "SELECT supplier_id, supplier_name, country, rating FROM suppliers "
     "WHERE is_active = 0 ORDER BY supplier_name", True),
    ("Give me the number of suppliers by country.",
     "SELECT country, COUNT(*) AS suppliers FROM suppliers "
     "GROUP BY country ORDER BY suppliers DESC", True),
    ("Are there any duplicate supplier names?",
     "SELECT supplier_name, COUNT(*) AS occurrences FROM suppliers "
     "GROUP BY supplier_name HAVING COUNT(*) > 1 ORDER BY occurrences DESC", True),
    ("List all warehouses.",
     "SELECT warehouse_id, name, city, country, capacity_units FROM warehouses "
     "ORDER BY warehouse_id", True),
    ("How many purchase orders are there in total?",
     "SELECT COUNT(*) AS po_count FROM purchase_orders", False),
    ("How many purchase orders were cancelled?",
     "SELECT COUNT(*) AS cancelled_pos FROM purchase_orders WHERE status = 'CANCELLED'", False),
    ("Show purchase orders by status.",
     "SELECT status, COUNT(*) AS po_count FROM purchase_orders "
     "GROUP BY status ORDER BY po_count DESC", True),
    ("What is the total spend?",
     "SELECT ROUND(SUM(total_amount), 2) AS total_spend FROM purchase_orders", False),
    ("Total spend by currency.",
     "SELECT currency, ROUND(SUM(total_amount), 2) AS total_amount, COUNT(*) AS po_count "
     "FROM purchase_orders GROUP BY currency ORDER BY total_amount DESC", True),
    ("Top suppliers by spend.",
     "SELECT s.supplier_name, ROUND(SUM(po.total_amount), 2) AS spend "
     "FROM purchase_orders po JOIN suppliers s ON po.supplier_id = s.supplier_id "
     "GROUP BY s.supplier_name ORDER BY spend DESC LIMIT 10", True),
    ("How many shipments were late?",
     "SELECT COUNT(*) AS late_shipments FROM shipments "
     "WHERE delivered_date IS NOT NULL AND delivered_date > promised_date", False),
    ("Show shipments by status.",
     "SELECT status, COUNT(*) AS shipment_count FROM shipments "
     "GROUP BY status ORDER BY shipment_count DESC", True),
    ("What is the on-time delivery rate?",
     "SELECT ROUND(100.0 * SUM(CASE WHEN delivered_date <= promised_date THEN 1 ELSE 0 END) "
     "/ COUNT(*), 2) AS on_time_pct FROM shipments WHERE delivered_date IS NOT NULL", False),
    ("What is the average freight cost?",
     "SELECT ROUND(AVG(freight_cost), 2) AS avg_freight FROM shipments", False),
    ("Inventory by warehouse.",
     "SELECT w.name AS warehouse, SUM(i.on_hand_qty) AS on_hand "
     "FROM inventory_snapshots i JOIN warehouses w ON i.warehouse_id = w.warehouse_id "
     "GROUP BY w.name ORDER BY on_hand DESC", True),
    ("Which items are below the reorder point?",
     "SELECT product_id, warehouse_id, snapshot_date, on_hand_qty, reorder_point "
     "FROM inventory_snapshots WHERE on_hand_qty < reorder_point "
     "ORDER BY on_hand_qty - reorder_point LIMIT 20", True),
    ("Show the quality inspection results breakdown.",
     "SELECT result, COUNT(*) AS inspections FROM quality_inspections "
     "GROUP BY result ORDER BY inspections DESC", True),
    ("Defects by severity.",
     "SELECT severity, COUNT(*) AS defects, SUM(quantity_affected) AS units "
     "FROM defects GROUP BY severity ORDER BY defects DESC", True),
    ("What are the top defect types?",
     "SELECT defect_type, COUNT(*) AS occurrences FROM defects "
     "GROUP BY defect_type ORDER BY occurrences DESC", True),
    ("Which suppliers have the worst ratings?",
     "SELECT supplier_name, rating FROM suppliers WHERE rating IS NOT NULL "
     "ORDER BY rating ASC LIMIT 10", True),
    ("Average lead time by category.",
     "SELECT category, ROUND(AVG(lead_time_days), 1) AS avg_lead_days FROM product_master "
     "GROUP BY category ORDER BY avg_lead_days DESC", True),
    ("How many products and components are in the catalogue?",
     "SELECT kind, COUNT(*) AS n FROM product_master GROUP BY kind", False),
    ("Show me all warehouse capacities.",
     "SELECT warehouse_id, name, capacity_units FROM warehouses ORDER BY capacity_units DESC", True),
    ("How many suppliers are onboarded?",
     "SELECT COUNT(*) AS supplier_count FROM suppliers", False),
    ("Give me the count of purchase orders.",
     "SELECT COUNT(*) AS po_count FROM purchase_orders", False),
    ("Show shipment status counts.",
     "SELECT status, COUNT(*) AS shipment_count FROM shipments "
     "GROUP BY status ORDER BY shipment_count DESC", True),
    ("How many shipments are there in total?",
     "SELECT COUNT(*) AS shipment_count FROM shipments", False),

    # --- harder / paraphrase (offline planner will mostly miss) ------------
    ("Which carrier handles the most shipments?",
     "SELECT carrier, COUNT(*) AS shipment_count FROM shipments "
     "GROUP BY carrier ORDER BY shipment_count DESC LIMIT 1", True),
    ("How many shipments were returned?",
     "SELECT COUNT(*) AS returned FROM shipments WHERE status = 'RETURNED'", False),
    ("What is the total freight cost by currency?",
     "SELECT currency, ROUND(SUM(freight_cost), 2) AS freight FROM shipments "
     "GROUP BY currency ORDER BY freight DESC", True),
    ("How many defects are critical?",
     "SELECT COUNT(*) AS critical_defects FROM defects WHERE severity = 'critical'", False),
    ("Which warehouse holds the most inventory on hand?",
     "SELECT w.name AS warehouse, SUM(i.on_hand_qty) AS on_hand "
     "FROM inventory_snapshots i JOIN warehouses w ON i.warehouse_id = w.warehouse_id "
     "GROUP BY w.name ORDER BY on_hand DESC LIMIT 1", True),
    ("How many distinct products appear on purchase order lines?",
     "SELECT COUNT(DISTINCT product_id) AS distinct_products FROM po_lines", False),
    ("Which supplier has the highest rating?",
     "SELECT supplier_name, rating FROM suppliers ORDER BY rating DESC LIMIT 1", True),
    ("How many quality inspections failed?",
     "SELECT COUNT(*) AS failed FROM quality_inspections WHERE result = 'FAIL'", False),
    ("What is the average unit price of products by category?",
     "SELECT pm.category, ROUND(AVG(pl.unit_price), 2) AS avg_unit_price "
     "FROM po_lines pl JOIN product_master pm ON pl.product_id = pm.product_id "
     "GROUP BY pm.category ORDER BY avg_unit_price DESC", True),
    ("How many purchase orders are still open?",
     "SELECT COUNT(*) AS open_pos FROM purchase_orders WHERE status = 'OPEN'", False),
    ("What is the total value of closed purchase orders?",
     "SELECT ROUND(SUM(total_amount), 2) AS closed_value FROM purchase_orders "
     "WHERE status = 'CLOSED'", False),
]


def canonical_hash(rows: list[dict], ordered: bool) -> str:
    vals = [tuple(r.values()) for r in rows]
    if not ordered:
        vals = sorted(vals, key=lambda t: [str(x) for x in t])
    blob = json.dumps(vals, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def main() -> None:
    db = SafeDB()
    lines = []
    for i, (q, gold, ordered) in enumerate(QA, 1):
        res = db.run_sql(gold)
        assert res["error"] is None, f"gold SQL failed for Q{i}: {res['error']}\n{gold}"
        rec = {
            "id": i,
            "question": q,
            "gold_sql": gold,
            "ordered": ordered,
            "expected_row_count": res["row_count"],
            "expected_hash": canonical_hash(res["rows"], ordered),
        }
        lines.append(rec)
    with open(OUT, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Wrote {len(lines)} questions to {OUT}")


if __name__ == "__main__":
    main()
