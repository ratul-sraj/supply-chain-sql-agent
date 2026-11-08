# Supply-chain database schema

Synthetic but realistic SQLite database built by `scripts/build_db.py` (seed `42`,
deterministic). Nine tables, ~7 600 rows total, with deliberate real-world messiness.

## Entity-relationship overview

```
suppliers ──< purchase_orders ──< po_lines >── product_master
    │              │                                 │
    │              └──< shipments ──< quality_inspections ──< defects
    │                        │
warehouses ──< inventory_snapshots >── product_master
```

## Tables

### `suppliers` — 40 rows
| column | type | notes |
| --- | --- | --- |
| supplier_id | INTEGER PK | |
| supplier_name | TEXT | **has exact duplicates** (same name, different id) |
| country, region | TEXT | |
| currency | TEXT | INR / USD / AED |
| is_active | INTEGER | 0/1 |
| onboarded_date | TEXT | ISO date |
| rating | REAL | **nullable** (~10% NULL) |

### `product_master` — 60 rows  ⚠️ misleading name
Despite sounding like a product-only catalogue, this table holds **both finished
products and raw components**, separated only by `kind ∈ {'product','component'}`
(25 products, 35 components). Always filter on `kind` when the question says
"products" or "components".

| column | type | notes |
| --- | --- | --- |
| product_id | INTEGER PK | |
| sku | TEXT | `SKU-0001`… |
| name | TEXT | |
| kind | TEXT | `'product'` or `'component'` |
| category | TEXT | electronics, fasteners, polymers, metals, packaging, power, cabling |
| unit_cost | REAL | **nullable** |
| currency | TEXT | INR / USD / AED |
| lead_time_days | INTEGER | |
| supplier_id | FK → suppliers | |

### `warehouses` — 6 rows
`warehouse_id` (TEXT PK, e.g. `WH-CHN`), `name`, `city`, `country`, `capacity_units`.

### `purchase_orders` — 400 rows
| column | type | notes |
| --- | --- | --- |
| po_id | INTEGER PK | |
| po_number | TEXT | `PO-00001`… |
| supplier_id | FK → suppliers | |
| warehouse_id | FK → warehouses | |
| order_date | TEXT | |
| expected_date | TEXT | **nullable** (~6% NULL) |
| status | TEXT | `OPEN`, `CLOSED`, `CANCELLED`, `PARTIAL` |
| currency | TEXT | INR / USD / AED (mixed) |
| total_amount | REAL | |
| created_by | TEXT | **nullable** |

### `po_lines` — 1 184 rows
`line_id` PK, `po_id` FK, `product_id` FK, `quantity`, `unit_price` (**nullable**),
`received_qty` (**nullable** unless line closed/partial), `line_status`
(`OPEN`/`CLOSED`/`PARTIAL`/`CANCELLED`).

### `shipments` — 900 rows
| column | type | notes |
| --- | --- | --- |
| shipment_id | INTEGER PK | |
| po_id | FK → purchase_orders | |
| carrier | TEXT | Maersk, DHL, FedEx, Kuehne+Nagel, DB Schenker, BlueDart |
| shipped_date | TEXT | **nullable** |
| promised_date | TEXT | NOT NULL |
| delivered_date | TEXT | **nullable** (NULL while in transit) |
| status | TEXT | `IN_TRANSIT`, `DELIVERED`, `DELAYED`, `CANCELLED`, `RETURNED` |
| freight_cost | REAL | **nullable** |
| currency | TEXT | INR / USD / AED |

**Late shipment** ⇔ `delivered_date IS NOT NULL AND delivered_date > promised_date`.
224 of 900 shipments are late.

### `inventory_snapshots` — 3 000 rows
`snapshot_id` PK, `product_id` FK, `warehouse_id` FK, `snapshot_date` (weekly, 2024),
`on_hand_qty`, `reserved_qty` (**nullable**), `reorder_point`.
Low stock ⇔ `on_hand_qty < reorder_point`.

### `quality_inspections` — 700 rows
`inspection_id` PK, `shipment_id` FK, `product_id` FK, `inspection_date`,
`inspector` (**nullable**), `result` (`PASS`/`FAIL`/`CONDITIONAL`),
`sample_size`, `defect_count`.

### `defects` — 300 rows
`defect_id` PK, `inspection_id` FK, `defect_type`, `severity`
(`cosmetic`/`minor`/`major`/`critical`), `quantity_affected`, `reported_date`
(**nullable**), `root_cause` (**nullable**).

## Deliberate messiness (and why)

| Trap | Where | Why it matters |
| --- | --- | --- |
| Misleading table name | `product_master` mixes products + components | naïve "list all products" is wrong |
| Duplicate supplier names | `suppliers` (e.g. `Stark Alloys` ×2) | `COUNT(*)` vs `COUNT(DISTINCT supplier_name)` differ |
| Currency mix | PO / shipments / product_master / suppliers | you cannot `SUM` amounts across currencies blindly |
| NULLs | expected_date, delivered_date, received_qty, rating, root_cause, inspector, created_by | filters must handle NULL |
| Cancelled / partial rows | purchase_orders, po_lines, shipments | totals must decide whether to include them |
| Late shipments | shipments (224) | derived metric, no stored flag |
