"""End-to-end MCP test: spawn the real server as a subprocess and do a full
handshake + tools/call round trip over stdio."""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp_server", "server.py")


class MCPClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=ROOT, text=True, bufsize=1)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise AssertionError(f"server closed stdout. stderr={err!r}")
        return json.loads(line)

    def call(self, msg_id, method, params=None):
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method,
                   "params": params or {}})
        return self.recv()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=5)


def test_mcp_full_handshake_and_tool_call():
    c = MCPClient()
    try:
        # initialize
        init = c.call(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        })
        assert init["result"]["protocolVersion"] == "2024-11-05"
        assert init["result"]["serverInfo"]["name"] == "supply-chain-sql-agent"

        # notification (no id) -> no response line; then ping to sync
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        pong = c.call(2, "ping")
        assert pong["result"] == {}

        # tools/list
        tl = c.call(3, "tools/list")
        names = {t["name"] for t in tl["result"]["tools"]}
        assert names == {"list_tables", "describe_table", "run_sql",
                         "sample_rows", "ask_question"}

        # tools/call: run_sql
        rs = c.call(4, "tools/call", {
            "name": "run_sql",
            "arguments": {"sql": "SELECT COUNT(*) AS n FROM shipments"}})
        payload = json.loads(rs["result"]["content"][0]["text"])
        assert payload["error"] is None
        assert payload["rows"][0]["n"] == 900

        # tools/call: list_tables
        lt = c.call(5, "tools/call", {"name": "list_tables", "arguments": {}})
        tables = json.loads(lt["result"]["content"][0]["text"])["tables"]
        assert "suppliers" in tables

        # tools/call: ask_question (offline planner)
        aq = c.call(6, "tools/call", {
            "name": "ask_question",
            "arguments": {"question": "how many suppliers?",
                          "planner": "template"}})
        ans = json.loads(aq["result"]["content"][0]["text"])
        assert ans["row_count"] == 1
        assert ans["error"] is None
    finally:
        c.close()


def test_mcp_rejects_write_via_tool():
    c = MCPClient()
    try:
        c.call(1, "initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {}, "clientInfo": {}})
        rs = c.call(2, "tools/call", {
            "name": "run_sql", "arguments": {"sql": "DELETE FROM suppliers"}})
        payload = json.loads(rs["result"]["content"][0]["text"])
        assert payload["error"] is not None
        assert payload["error_kind"] == "guard"
    finally:
        c.close()


def test_mcp_unknown_method():
    c = MCPClient()
    try:
        c.call(1, "initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {}, "clientInfo": {}})
        resp = c.call(2, "does/not/exist")
        assert resp["error"]["code"] == -32601
    finally:
        c.close()
