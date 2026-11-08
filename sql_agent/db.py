"""Safe, read-only SQL execution layer for the supply-chain agent.

Design goals (why this exists):
  * The model writes SQL from natural language; that SQL is untrusted input.
  * We therefore enforce defence-in-depth:
      1. connection opened read-only (``file:...?mode=ro``) AND
         ``PRAGMA query_only=ON`` so SQLite itself refuses writes;
      2. a static guard that only allows a single ``SELECT`` / ``WITH``
         statement and rejects banned keywords (INSERT/UPDATE/DELETE/DROP/
         ATTACH/PRAGMA/...);
      3. automatic LIMIT injection + a hard row cap so a careless
         ``SELECT * FROM inventory_snapshots`` cannot return a million rows;
      4. a wall-clock statement timeout enforced with a progress handler;
      5. DB errors are mapped to short, *actionable* messages the agent can
         act on during self-repair (e.g. "no such column: foo").
  * ``explain`` provides an EXPLAIN QUERY PLAN fallback so the agent can ask
    "what would SQLite do with this?" without executing it.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(ROOT, "data", "supply_chain.db")

# statement must begin with one of these
_ALLOWED_STARTS = ("select", "with")
# banned anywhere (as a whole word) -> instant rejection
_BANNED = {
    "insert", "update", "delete", "drop", "attach", "detach", "alter",
    "create", "replace", "pragma", "vacuum", "reindex", "truncate",
    "grant", "revoke", "begin", "commit", "rollback", "savepoint",
    "transaction", "trigger", "index", "view",
}


class GuardError(Exception):
    """Raised when SQL is rejected by the static guard (not a DB error)."""


def _strip_sql_comments(sql: str) -> str:
    # remove -- line comments and /* */ block comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _tokenize_words(sql: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z_0-9]*", sql)


def guard_sql(sql: str) -> str:
    """Validate a single read-only statement. Returns cleaned SQL or raises."""
    if sql is None or not sql.strip():
        raise GuardError("Empty SQL statement.")

    cleaned = _strip_sql_comments(sql).strip()
    # strip a single trailing semicolon; reject multi-statement
    body = cleaned.rstrip()
    if body.endswith(";"):
        body = body[:-1].strip()
    if ";" in body:
        raise GuardError(
            "Multiple SQL statements are not allowed; submit a single SELECT.")

    lowered = body.lower()
    # must start with select or with (allow leading parenthesis)
    lead = re.match(r"^\(\s*([a-z_]+)", lowered)
    first_word = lead.group(1) if lead else (lowered.split(None, 1)[0] if lowered.split() else "")
    if not first_word:
        raise GuardError("Could not parse a leading SQL keyword.")
    if first_word not in _ALLOWED_STARTS:
        raise GuardError(
            f"Only read-only SELECT / WITH queries are allowed "
            f"(got '{first_word.upper()}'). This database is query-only.")

    words = set(w.lower() for w in _tokenize_words(body))
    bad = sorted(words & _BANNED)
    if bad:
        raise GuardError(
            "Query contains forbidden keyword(s): " + ", ".join(b.upper() for b in bad)
            + ". Only read-only SELECT/WITH statements are permitted.")

    return body


def _has_top_level_limit(sql: str) -> bool:
    """Best-effort: does the statement already carry a LIMIT clause?"""
    return bool(re.search(r"\blimit\b", _strip_sql_comments(sql), flags=re.IGNORECASE))


def inject_limit(sql: str, cap: int) -> str:
    """Wrap a SELECT/WITH so at most ``cap`` rows come back (if no LIMIT set)."""
    if _has_top_level_limit(sql):
        return sql
    return f"SELECT * FROM (\n{sql}\n) LIMIT {cap}"


class SafeDB:
    def __init__(self, path: str = DEFAULT_DB, *, row_cap: int = 500,
                 timeout_s: float = 5.0):
        self.path = path
        self.row_cap = row_cap
        self.timeout_s = timeout_s
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Database not found at {path}. Run `python scripts/build_db.py` first.")

    # ---- connection ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=self.timeout_s)
        con.row_factory = sqlite3.Row
        # belt-and-braces: even if the file were writable, refuse writes.
        con.execute("PRAGMA query_only = ON")
        con.execute("PRAGMA foreign_keys = OFF")
        return con

    def _deadline_handler(self, deadline: float):
        def handler() -> int:
            return 1 if time.monotonic() > deadline else 0
        return handler

    # ---- public API ------------------------------------------------------
    def run_sql(self, sql: str) -> dict[str, Any]:
        """Guard -> LIMIT-wrap -> execute -> map rows. Never raises for user
        errors; returns a result dict with an ``error`` field instead."""
        t0 = time.monotonic()
        result: dict[str, Any] = {
            "sql": sql, "sql_executed": None, "columns": [], "rows": [],
            "row_count": 0, "truncated": False, "latency_ms": 0.0,
            "error": None, "error_kind": None,
        }
        try:
            safe = guard_sql(sql)
        except GuardError as e:
            result["error"] = str(e)
            result["error_kind"] = "guard"
            result["latency_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return result

        wrapped = inject_limit(safe, self.row_cap + 1)
        result["sql_executed"] = wrapped
        con = self._connect()
        try:
            deadline = time.monotonic() + self.timeout_s
            con.set_progress_handler(self._deadline_handler(deadline), 1000)
            cur = con.execute(wrapped)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            truncated = len(rows) > self.row_cap
            rows = rows[: self.row_cap]
            result["columns"] = cols
            result["rows"] = [dict(zip(cols, tuple(r))) for r in rows]
            result["row_count"] = len(result["rows"])
            result["truncated"] = truncated
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "interrupted" in msg.lower():
                result["error"] = (
                    f"Query exceeded the {self.timeout_s:g}s statement timeout. "
                    "Add a WHERE filter or aggregate to narrow it down.")
                result["error_kind"] = "timeout"
            else:
                result["error"] = _map_db_error(msg)
                result["error_kind"] = "sql"
        except sqlite3.DatabaseError as e:
            result["error"] = _map_db_error(str(e))
            result["error_kind"] = "sql"
        finally:
            con.close()
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 2)
        return result

    def explain(self, sql: str) -> dict[str, Any]:
        """EXPLAIN QUERY PLAN fallback: no rows are executed, just the plan."""
        try:
            safe = guard_sql(sql)
        except GuardError as e:
            return {"error": str(e), "plan": []}
        con = self._connect()
        try:
            cur = con.execute("EXPLAIN QUERY PLAN " + safe)
            plan = [dict(r) for r in cur.fetchall()]
        except sqlite3.DatabaseError as e:
            return {"error": _map_db_error(str(e)), "plan": []}
        finally:
            con.close()
        return {"error": None, "plan": plan}

    # ---- introspection ---------------------------------------------------
    def list_tables(self) -> list[str]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def describe_table(self, table: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table or ""):
            return {"error": f"Invalid table name: {table!r}"}
        if table not in self.list_tables():
            return {"error": f"No such table: {table}"}
        con = self._connect()
        try:
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            fks = con.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()
        return {
            "table": table,
            "row_count": n,
            "columns": [{"name": c[1], "type": c[2], "notnull": bool(c[3]),
                         "pk": bool(c[5])} for c in cols],
            "foreign_keys": [{"column": f[3], "ref_table": f[2], "ref_column": f[4]}
                             for f in fks],
        }

    def sample_rows(self, table: str, n: int = 3) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table or ""):
            return {"error": f"Invalid table name: {table!r}"}
        n = max(1, min(int(n), 20))
        res = self.run_sql(f"SELECT * FROM {table} LIMIT {n}")
        return {"table": table, "columns": res["columns"], "rows": res["rows"],
                "error": res["error"]}


def _map_db_error(msg: str) -> str:
    """Turn a raw SQLite message into a short, actionable hint."""
    low = msg.lower()
    if "no such table" in low:
        m = re.search(r"no such table: (\S+)", msg)
        tbl = m.group(1) if m else "?"
        return (f"No such table: {tbl}. Call list_tables to see valid tables. "
                "Note: product_master holds both products and components.")
    if "no such column" in low:
        m = re.search(r"no such column: (\S+)", msg)
        col = m.group(1) if m else "?"
        return (f"No such column: {col}. Call describe_table on the table "
                "to see its columns.")
    if "ambiguous column" in low:
        return ("Ambiguous column name. Qualify it with a table alias "
                "(e.g. s.supplier_id).")
    if "syntax error" in low:
        return f"SQL syntax error: {msg}. Check the statement near the reported token."
    if "misuse of aggregate" in low:
        return "Aggregate used incorrectly; use GROUP BY or a subquery."
    if "datatype mismatch" in low:
        return "Datatype mismatch; check that text and numeric columns line up."
    return msg


if __name__ == "__main__":
    import sys
    db = SafeDB()
    print("Tables:", db.list_tables())
    q = sys.argv[1] if len(sys.argv) > 1 else "SELECT * FROM suppliers LIMIT 3"
    import json
    print(json.dumps(db.run_sql(q), indent=2, default=str))
