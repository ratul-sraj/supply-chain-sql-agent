#!/usr/bin/env python
"""Minimal MCP (Model Context Protocol) server over stdio — ZERO third-party
dependencies. Implements JSON-RPC 2.0 with the newline-delimited message
framing used by the MCP stdio transport (and also sniffs LSP-style
``Content-Length:`` headers for clients that send them).

Run it directly::

    python mcp_server/server.py

It reads one JSON-RPC message per line from stdin and writes responses to
stdout. Supported methods:

    initialize                     -> protocolVersion 2024-11-05
    notifications/initialized      -> (notification, no response)
    ping                           -> {}
    tools/list                     -> the 5 supply-chain tools
    tools/call                     -> execute a tool (list_tables, describe_table,
                                      run_sql, sample_rows, ask_question)
"""
from __future__ import annotations

import json
import os
import sys

# make the project importable when run as a bare script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sql_agent.db import SafeDB          # noqa: E402
from sql_agent.agent import SQLAgent, build_planner  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "supply-chain-sql-agent"
SERVER_VERSION = "1.0.0"

_DB = SafeDB()
_AGENT = SQLAgent(db=_DB, planner=build_planner())

# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "list_tables",
        "description": "List all tables in the supply-chain database.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "describe_table",
        "description": "Describe a table: columns, types, primary keys, foreign keys and row count.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string",
                                     "description": "table name"}},
            "required": ["table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sample_rows",
        "description": "Return up to N sample rows from a table.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"},
                           "n": {"type": "integer", "minimum": 1, "maximum": 20,
                                 "default": 3}},
            "required": ["table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sql",
        "description": "Run a read-only SELECT/WITH query. Guards reject writes, "
                       "enforce a statement timeout and cap rows.",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string",
                                   "description": "a single SELECT or WITH statement"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_question",
        "description": "Ask a natural-language question; returns the generated SQL "
                       "and the result rows (text-to-SQL).",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "planner": {"type": "string",
                                       "enum": ["auto", "llm", "template"],
                                       "default": "auto"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name not in _TOOL_NAMES:
        return _text_result(f"Unknown tool: {name}", is_error=True)
    try:
        if name == "list_tables":
            return _text_result(json.dumps({"tables": _DB.list_tables()}))
        if name == "describe_table":
            out = _DB.describe_table(args.get("table", ""))
            return _text_result(json.dumps(out, default=str),
                                is_error=bool(out.get("error")))
        if name == "sample_rows":
            out = _DB.sample_rows(args.get("table", ""), args.get("n", 3))
            return _text_result(json.dumps(out, default=str),
                                is_error=bool(out.get("error")))
        if name == "run_sql":
            out = _DB.run_sql(args.get("sql", ""))
            return _text_result(json.dumps(out, default=str),
                                is_error=bool(out.get("error")))
        if name == "ask_question":
            agent = _AGENT if args.get("planner") in (None, "auto") else \
                SQLAgent(db=_DB, planner=build_planner(args.get("planner")))
            res = agent.ask(args.get("question", ""))
            return _text_result(json.dumps(res.to_dict(), default=str),
                                is_error=bool(res.error))
    except Exception as e:  # noqa: BLE001  - never crash the server loop
        return _text_result(f"{type(e).__name__}: {e}", is_error=True)
    return _text_result(f"Unhandled tool: {name}", is_error=True)


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #
def handle_message(msg: dict) -> dict | None:
    """Return a JSON-RPC response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")
    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    is_notification = "id" not in msg or msg_id is None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return None if is_notification else _result(msg_id, result)

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return None if is_notification else _result(msg_id, {})

    if method == "tools/list":
        return None if is_notification else _result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        out = call_tool(name, args)
        return None if is_notification else _result(msg_id, out)

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return None if is_notification else _result(msg_id, {key: []})

    if is_notification:
        return None
    return _error(msg_id, -32601, f"Method not found: {method}")


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# stdio transport (newline-delimited JSON, with optional Content-Length framing)
# --------------------------------------------------------------------------- #
def _read_messages(stream):
    """Yield decoded JSON objects from a binary stream. Supports both
    newline-delimited JSON (MCP stdio) and LSP-style Content-Length framing."""
    while True:
        first = stream.readline()
        if not first:
            return
        line = first.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                continue
            # consume headers until blank line
            while True:
                h = stream.readline()
                if not h or h.strip() == b"":
                    break
            body = stream.read(length)
            try:
                yield json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue
        else:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"__parse_error__": line}


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout
    for msg in _read_messages(stdin):
        if "__parse_error__" in msg:
            stdout.write(json.dumps(_error(None, -32700, "Parse error")) + "\n")
            stdout.flush()
            continue
        resp = handle_message(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
