"""Build a realistic synthetic supply-chain SQLite database.

Produces data/supply_chain.db with deliberate real-world messiness:
  * late shipments and cancelled POs
  * NULLs in the places a real ERP leaves them
  * duplicate supplier names (same human name, different IDs)
  * a currency mix (INR / USD / AED)
  * one table with a deliberately misleading name: ``product_master`` sounds
    like a product-only catalog but actually mixes finished products and raw
    components, distinguished only by the ``kind`` column.

Fixed seed => deterministic output.
"""
from __future__ import annotations

import os
import random
import sqlite3
from datetime import date, timedelta

import numpy as np

SEED = 42
random.seed(SEED)
np.random.seed(SEED)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "supply_chain.db")

CURRENCIES = ["INR", "USD", "AED"]
CURRENCY_WEIGHTS = [0.6, 0.28, 0.12]

COUNTRIES = [
    ("India", "APAC"), ("China", "APAC"), ("Vietnam", "APAC"),
    ("Germany", "EMEA"), ("UAE", "EMEA"), ("USA", "AMER"),
    ("Mexico", "AMER"), ("Poland", "EMEA"), ("Thailand", "APAC"),
]

SUPPLIER_ROOTS = [
    "Acme Components", "Globex Industrial", "Initech Parts", "Umbrella Supply",
    "Soylent Materials", "Wayne Fabrication", "Stark Alloys", "Wonka Polymers",
    "Cyberdyne Systems", "Tyrell Motors", "Aperture Metals", "BlackMesa Chem",
    "Pied Piper Plastics", "Vandelay Imports", "Dunder Fasteners", "Hooli Devices",
    "Massive Dynamic", "Oscorp Rubber", "Prestige Worldwide", "Sterling Cooper",
]
SUPPLIER_SUFFIX = ["", " Pvt Ltd", " Industries", " Traders", " & Co", " LLC"]

CATEGORIES = ["electronics", "fasteners", "polymers", "metals", "packaging", "power", "cabling"]

WAREHOUSES = [
    ("WH-CHN", "Chennai DC", "Chennai", "India", 50000),
    ("WH-BLR", "Bengaluru Hub", "Bengaluru", "India", 42000),
    ("WH-DXB", "Dubai Freezone", "Dubai", "UAE", 38000),
    ("WH-FRA", "Frankfurt Cross-dock", "Frankfurt", "Germany", 30000),
    ("WH-DFW", "Dallas Regional", "Dallas", "USA", 45000),
    ("WH-SIN", "Singapore Transship", "Singapore", "Singapore", 26000),
]

CARRIERS = ["Maersk", "DHL", "FedEx", "Kuehne+Nagel", "DB Schenker", "BlueDart"]
PO_STATUS = ["OPEN", "CLOSED", "CANCELLED", "PARTIAL"]
PO_STATUS_W = [0.32, 0.44, 0.09, 0.15]
SHIP_STATUS = ["IN_TRANSIT", "DELIVERED", "DELAYED", "CANCELLED", "RETURNED"]
SHIP_STATUS_W = [0.18, 0.6, 0.12, 0.05, 0.05]
DEFECT_TYPES = ["solder_bridge", "cracked_housing", "wrong_pinout", "out_of_tolerance",
                "discoloration", "missing_label", "insufficient_torque", "contamination"]
SEVERITIES = ["cosmetic", "minor", "major", "critical"]
SEVERITY_W = [0.35, 0.35, 0.22, 0.08]
ROOT_CAUSES = ["supplier_process", "handling_damage", "design_tolerance",
               "packaging_failure", "shipping_vibration"]


def _date(d: date) -> str:
    return d.isoformat()


def build() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE suppliers (
            supplier_id     INTEGER PRIMARY KEY,
            supplier_name   TEXT NOT NULL,
            country         TEXT NOT NULL,
            region          TEXT NOT NULL,
            currency        TEXT NOT NULL,
            is_active       INTEGER NOT NULL,
            onboarded_date  TEXT NOT NULL,
            rating          REAL
        );

        -- NOTE: deliberately misleading name. Despite sounding like a
        -- product-only catalog, this holds BOTH finished products and raw
        -- components, split by the `kind` column.
        CREATE TABLE product_master (
            product_id      INTEGER PRIMARY KEY,
            sku             TEXT NOT NULL,
            name            TEXT NOT NULL,
            kind            TEXT NOT NULL CHECK (kind IN ('product','component')),
            category        TEXT NOT NULL,
            unit_cost       REAL,
            currency        TEXT NOT NULL,
            lead_time_days  INTEGER,
            supplier_id     INTEGER REFERENCES suppliers(supplier_id)
        );

        CREATE TABLE warehouses (
            warehouse_id    TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            city            TEXT NOT NULL,
            country         TEXT NOT NULL,
            capacity_units  INTEGER NOT NULL
        );

        CREATE TABLE purchase_orders (
            po_id           INTEGER PRIMARY KEY,
            po_number       TEXT NOT NULL,
            supplier_id     INTEGER REFERENCES suppliers(supplier_id),
            warehouse_id    TEXT REFERENCES warehouses(warehouse_id),
            order_date      TEXT NOT NULL,
            expected_date   TEXT,
            status          TEXT NOT NULL,
            currency        TEXT NOT NULL,
            total_amount    REAL,
            created_by      TEXT
        );

        CREATE TABLE po_lines (
            line_id         INTEGER PRIMARY KEY,
            po_id           INTEGER REFERENCES purchase_orders(po_id),
            product_id      INTEGER REFERENCES product_master(product_id),
            quantity        INTEGER NOT NULL,
            unit_price      REAL,
            received_qty    INTEGER,
            line_status     TEXT
        );

        CREATE TABLE shipments (
            shipment_id     INTEGER PRIMARY KEY,
            po_id           INTEGER REFERENCES purchase_orders(po_id),
            carrier         TEXT NOT NULL,
            shipped_date    TEXT,
            promised_date   TEXT NOT NULL,
            delivered_date  TEXT,
            status          TEXT NOT NULL,
            freight_cost    REAL,
            currency        TEXT NOT NULL
        );

        CREATE TABLE inventory_snapshots (
            snapshot_id     INTEGER PRIMARY KEY,
            product_id      INTEGER REFERENCES product_master(product_id),
            warehouse_id    TEXT REFERENCES warehouses(warehouse_id),
            snapshot_date   TEXT NOT NULL,
            on_hand_qty     INTEGER NOT NULL,
            reserved_qty    INTEGER,
            reorder_point   INTEGER
        );

        CREATE TABLE quality_inspections (
            inspection_id   INTEGER PRIMARY KEY,
            shipment_id     INTEGER REFERENCES shipments(shipment_id),
            product_id      INTEGER REFERENCES product_master(product_id),
            inspection_date TEXT NOT NULL,
            inspector       TEXT,
            result          TEXT NOT NULL,
            sample_size     INTEGER NOT NULL,
            defect_count    INTEGER
        );

        CREATE TABLE defects (
            defect_id         INTEGER PRIMARY KEY,
            inspection_id     INTEGER REFERENCES quality_inspections(inspection_id),
            defect_type       TEXT NOT NULL,
            severity          TEXT NOT NULL,
            quantity_affected INTEGER NOT NULL,
            reported_date     TEXT,
            root_cause        TEXT
        );
        """
    )

    # ---------------- suppliers ----------------
    suppliers = []
    for i in range(1, 41):
        root = SUPPLIER_ROOTS[(i - 1) % len(SUPPLIER_ROOTS)]
        # duplicate names: every supplier name appears, some *exactly* duplicated
        suffix = SUPPLIER_SUFFIX[(i - 1) % len(SUPPLIER_SUFFIX)]
        name = f"{root}{suffix}"
        country, region = COUNTRIES[(i - 1) % len(COUNTRIES)]
        currency = random.choices(CURRENCIES, CURRENCY_WEIGHTS)[0]
        is_active = 1 if random.random() < 0.85 else 0
        onboarded = date(2018, 1, 1) + timedelta(days=random.randint(0, 2400))
        rating = round(random.uniform(2.0, 5.0), 1) if random.random() < 0.9 else None
        suppliers.append((i, name, country, region, currency, is_active, _date(onboarded), rating))
    # Force a handful of exact duplicate names (different IDs) -> real dedup headache.
    suppliers[7] = (8, suppliers[6][1], suppliers[7][2], suppliers[7][3], suppliers[7][4],
                    suppliers[7][5], suppliers[7][6], suppliers[7][7])   # dup of id 7
    suppliers[22] = (23, suppliers[21][1], suppliers[22][2], suppliers[22][3], suppliers[22][4],
                     suppliers[22][5], suppliers[22][6], suppliers[22][7])
    cur.executemany("INSERT INTO suppliers VALUES (?,?,?,?,?,?,?,?)", suppliers)

    # ---------------- product_master ----------------
    products = []
    for i in range(1, 61):
        kind = "product" if i <= 25 else "component"
        cat = CATEGORIES[(i - 1) % len(CATEGORIES)]
        name = f"{cat.title()} {kind.title()} {i:03d}"
        cost = round(random.uniform(0.4, 420.0), 2) if random.random() < 0.92 else None
        currency = random.choices(CURRENCIES, CURRENCY_WEIGHTS)[0]
        lead = random.randint(3, 90)
        sup = random.randint(1, 40)
        products.append((i, f"SKU-{i:04d}", name, kind, cat, cost, currency, lead, sup))
    cur.executemany("INSERT INTO product_master VALUES (?,?,?,?,?,?,?,?,?)", products)

    # ---------------- warehouses ----------------
    wh = [(w[0], w[1], w[2], w[3], w[4]) for w in WAREHOUSES]
    cur.executemany("INSERT INTO warehouses VALUES (?,?,?,?,?)", wh)

    # ---------------- purchase_orders ----------------
    pos = []
    n_pos = 400
    for i in range(1, n_pos + 1):
        supplier_id = random.randint(1, 40)
        warehouse_id = random.choice([w[0] for w in WAREHOUSES])
        order_d = date(2023, 1, 1) + timedelta(days=random.randint(0, 700))
        expected = order_d + timedelta(days=random.randint(5, 75))
        status = random.choices(PO_STATUS, PO_STATUS_W)[0]
        currency = random.choices(CURRENCIES, CURRENCY_WEIGHTS)[0]
        total = round(random.uniform(250.0, 85000.0), 2)
        created_by = random.choice(["arjun.k", "mei.l", "sara.h", "tom.b", "priya.n", None])
        expected_v = _date(expected) if random.random() < 0.94 else None
        pos.append((i, f"PO-{i:05d}", supplier_id, warehouse_id, _date(order_d),
                    expected_v, status, currency, total, created_by))
    cur.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?)", pos)

    # ---------------- po_lines (~1200) ----------------
    lines = []
    line_id = 1
    per_po = np.random.randint(1, 6, size=n_pos)  # sums ~ to 1200
    for po_id in range(1, n_pos + 1):
        for _ in range(int(per_po[po_id - 1])):
            product_id = random.randint(1, 60)
            qty = random.randint(1, 2000)
            unit_price = round(random.uniform(0.3, 450.0), 2) if random.random() < 0.95 else None
            received = None
            line_status = random.choices(
                ["OPEN", "CLOSED", "PARTIAL", "CANCELLED"], [0.25, 0.5, 0.15, 0.1])[0]
            if line_status in ("CLOSED", "PARTIAL"):
                received = int(qty * random.uniform(0.4, 1.0))
            lines.append((line_id, po_id, product_id, qty, unit_price, received, line_status))
            line_id += 1
    cur.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?)", lines)

    # ---------------- shipments (~900) ----------------
    ships = []
    n_ships = 900
    for i in range(1, n_ships + 1):
        po_id = random.randint(1, n_pos)
        carrier = random.choice(CARRIERS)
        shipped = date(2023, 1, 1) + timedelta(days=random.randint(0, 720))
        promised = shipped + timedelta(days=random.randint(2, 30))
        status = random.choices(SHIP_STATUS, SHIP_STATUS_W)[0]
        delivered = None
        if status in ("DELIVERED", "DELAYED"):
            # ~30% arrive late (after promised)
            if status == "DELAYED" or random.random() < 0.22:
                delivered = promised + timedelta(days=random.randint(1, 25))
            else:
                delivered = promised - timedelta(days=random.randint(0, 6))
        freight = round(random.uniform(50.0, 4200.0), 2) if random.random() < 0.9 else None
        currency = random.choices(CURRENCIES, CURRENCY_WEIGHTS)[0]
        ships.append((i, po_id, carrier, _date(shipped), _date(promised),
                      _date(delivered) if delivered else None, status, freight, currency))
    cur.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?)", ships)

    # ---------------- inventory_snapshots (~3000) ----------------
    snaps = []
    snap_id = 1
    base_dates = [date(2024, 1, 1) + timedelta(days=7 * k) for k in range(27)]
    target = 3000
    while snap_id <= target:
        product_id = random.randint(1, 60)
        warehouse_id = random.choice([w[0] for w in WAREHOUSES])
        snap_d = random.choice(base_dates)
        on_hand = int(np.random.gamma(3.0, 40.0))
        reserved = int(on_hand * random.uniform(0, 0.6)) if random.random() < 0.85 else None
        reorder = random.randint(20, 400)
        snaps.append((snap_id, product_id, warehouse_id, _date(snap_d),
                      on_hand, reserved, reorder))
        snap_id += 1
    cur.executemany("INSERT INTO inventory_snapshots VALUES (?,?,?,?,?,?,?)", snaps)

    # ---------------- quality_inspections (~700) ----------------
    insp = []
    n_insp = 700
    for i in range(1, n_insp + 1):
        shipment_id = random.randint(1, n_ships)
        product_id = random.randint(1, 60)
        insp_d = date(2023, 2, 1) + timedelta(days=random.randint(0, 700))
        inspector = random.choice(["r.kumar", "j.osei", "l.zhang", "m.ali", None])
        result = random.choices(["PASS", "FAIL", "CONDITIONAL"], [0.72, 0.14, 0.14])[0]
        sample = random.randint(5, 200)
        defect_count = random.randint(0, 25) if result != "PASS" else random.randint(0, 2)
        insp.append((i, shipment_id, product_id, _date(insp_d), inspector,
                     result, sample, defect_count))
    cur.executemany("INSERT INTO quality_inspections VALUES (?,?,?,?,?,?,?,?)", insp)

    # ---------------- defects (~300) ----------------
    defects = []
    inspectable = [x for x in insp if x[5] in ("FAIL", "CONDITIONAL")]
    n_def = 300
    for i in range(1, n_def + 1):
        inspection_id = random.choice(inspectable)[0]
        dtype = random.choice(DEFECT_TYPES)
        sev = random.choices(SEVERITIES, SEVERITY_W)[0]
        qty = random.randint(1, 120)
        rep = date(2023, 2, 1) + timedelta(days=random.randint(0, 700))
        cause = random.choice(ROOT_CAUSES) if random.random() < 0.82 else None
        defects.append((i, inspection_id, dtype, sev, qty,
                        _date(rep) if random.random() < 0.95 else None, cause))
    cur.executemany("INSERT INTO defects VALUES (?,?,?,?,?,?,?)", defects)

    con.commit()

    counts = {}
    for tbl in ("suppliers", "product_master", "warehouses", "purchase_orders",
                "po_lines", "shipments", "inventory_snapshots",
                "quality_inspections", "defects"):
        counts[tbl] = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    con.close()
    return counts


def main() -> None:
    counts = build()
    print(f"Built {DB_PATH}")
    for tbl, n in counts.items():
        print(f"  {tbl:22s} {n:>6d} rows")


if __name__ == "__main__":
    main()
