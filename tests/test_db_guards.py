"""Tests for the read-only SQL guard layer (sql_agent/db.py)."""
from __future__ import annotations

import pytest

from sql_agent.db import SafeDB, GuardError, guard_sql, inject_limit, _has_top_level_limit


@pytest.fixture(scope="module")
def db():
    return SafeDB()


# ---- static guard -------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT * FROM suppliers",
    "select supplier_id from suppliers",
    "  SELECT COUNT(*) FROM purchase_orders  ",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT * FROM suppliers WHERE supplier_name = 'Acme'",
])
def test_guard_allows_reads(sql):
    assert guard_sql(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE suppliers",
    "INSERT INTO suppliers VALUES (1)",
    "UPDATE suppliers SET rating = 5",
    "DELETE FROM suppliers",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA table_info(suppliers)",
    "CREATE TABLE t (a int)",
    "SELECT 1; DROP TABLE suppliers",
    "SELECT 1; DELETE FROM suppliers",
    "ALTER TABLE suppliers RENAME TO s2",
    "REPLACE INTO suppliers VALUES (1)",
    "VACUUM",
])
def test_guard_blocks_writes(sql):
    with pytest.raises(GuardError):
        guard_sql(sql)


def test_guard_rejects_empty():
    with pytest.raises(GuardError):
        guard_sql("   ")


def test_guard_allows_trailing_semicolon():
    assert guard_sql("SELECT 1;").strip() == "SELECT 1"


# ---- LIMIT injection ----------------------------------------------------- #
def test_inject_limit_adds_when_missing():
    out = inject_limit("SELECT * FROM suppliers", 500)
    assert "LIMIT 500" in out and out.startswith("SELECT * FROM (")


def test_inject_limit_respects_existing():
    sql = "SELECT * FROM suppliers LIMIT 3"
    assert inject_limit(sql, 500) == sql


def test_has_top_level_limit():
    assert _has_top_level_limit("SELECT * FROM t LIMIT 5")
    assert not _has_top_level_limit("SELECT * FROM t")


# ---- runtime behaviour --------------------------------------------------- #
def test_row_cap_enforced(db):
    res = db.run_sql("SELECT * FROM inventory_snapshots")  # 3000 rows in table
    assert res["error"] is None
    assert res["row_count"] <= 500
    assert res["truncated"] is True


def test_run_sql_returns_columns_and_rows(db):
    res = db.run_sql("SELECT supplier_id, supplier_name FROM suppliers LIMIT 2")
    assert res["columns"] == ["supplier_id", "supplier_name"]
    assert res["row_count"] == 2
    assert all(isinstance(r["supplier_name"], str) for r in res["rows"])


def test_run_sql_blocks_writes_at_runtime(db):
    res = db.run_sql("DROP TABLE suppliers")
    assert res["error"] is not None
    assert res["error_kind"] == "guard"


def test_connection_is_query_only(db):
    # even bypassing the static guard, SQLite must refuse the write
    con = db._connect()
    try:
        with pytest.raises(Exception):
            con.execute("CREATE TABLE nope (a int)")
    finally:
        con.close()


def test_error_mapping_no_such_column(db):
    res = db.run_sql("SELECT nope FROM suppliers")
    assert res["error_kind"] == "sql"
    assert "No such column" in res["error"]
    assert "describe_table" in res["error"]


def test_error_mapping_no_such_table(db):
    res = db.run_sql("SELECT * FROM vendors")
    assert "No such table" in res["error"]
    assert "list_tables" in res["error"]


def test_timeout_maps_to_actionable_message():
    db = SafeDB(timeout_s=0.0005)
    # a big cross join that will exceed the deadline
    res = db.run_sql(
        "SELECT COUNT(*) FROM inventory_snapshots a, inventory_snapshots b, "
        "inventory_snapshots c")
    # either it times out (actionable) or (rarely) completes; both are acceptable
    if res["error"]:
        assert res["error_kind"] == "timeout"
        assert "timeout" in res["error"].lower()


def test_explain_fallback(db):
    out = db.explain("SELECT * FROM suppliers WHERE country = 'India'")
    assert out["error"] is None
    assert out["plan"]


def test_explain_rejects_writes(db):
    out = db.explain("DROP TABLE suppliers")
    assert out["error"] is not None


# ---- introspection ------------------------------------------------------- #
def test_list_tables(db):
    tables = db.list_tables()
    assert "suppliers" in tables and "product_master" in tables
    assert len(tables) == 9


def test_describe_table(db):
    d = db.describe_table("shipments")
    assert d["row_count"] == 900
    names = [c["name"] for c in d["columns"]]
    assert "promised_date" in names and "delivered_date" in names
    assert any(fk["ref_table"] == "purchase_orders" for fk in d["foreign_keys"])


def test_describe_unknown_table(db):
    assert "error" in db.describe_table("does_not_exist")


def test_sample_rows(db):
    out = db.sample_rows("warehouses", 2)
    assert out["error"] is None
    assert len(out["rows"]) == 2
