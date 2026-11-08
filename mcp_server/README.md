# MCP server

A **zero-dependency** Model Context Protocol server (stdlib `json` + `sqlite3`
only) that exposes the supply-chain text-to-SQL agent as MCP tools over the
stdio transport.

## Run it

```bash
python mcp_server/server.py
```

It reads newline-delimited JSON-RPC 2.0 messages from stdin and writes
responses to stdout. (It also tolerates LSP-style `Content-Length:` framing.)

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python mcp_server/server.py
```

## Protocol

* `initialize` → `protocolVersion: "2024-11-05"`, `capabilities.tools`, `serverInfo`
* `notifications/initialized` → accepted, no response
* `ping` → `{}`
* `tools/list` → the 5 tools below
* `tools/call` → `{content:[{type:"text",text:<json>}], isError}`

### Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_tables` | – | all table names |
| `describe_table` | `table` | columns, types, PKs, FKs, row count |
| `sample_rows` | `table`, `n` (≤20) | first *n* rows |
| `run_sql` | `sql` | guarded read-only query result |
| `ask_question` | `question`, `planner?` | generated SQL + rows + step trace |

`run_sql` goes through the same guard as the rest of the system: single
SELECT/WITH only, banned-keyword rejection, automatic LIMIT, statement timeout,
row cap.

## Registering in Claude Desktop

Add this block to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "supply-chain-sql": {
      "command": "python",
      "args": ["D:/projects/ai-ml-portfolio/supply-chain-sql-agent/mcp_server/server.py"],
      "env": {
        "PYTHONPATH": "D:/projects/ai-ml-portfolio/supply-chain-sql-agent"
      }
    }
  }
}
```

Use an **absolute path** to `server.py` (and to the Python interpreter if `python`
is not on PATH, e.g. `C:/Python311/python.exe`). The database must exist first:
`python scripts/build_db.py`.

## Registering in any other MCP client

The client config shape is the same everywhere (Cursor, Continue, custom
agents): a `command` that starts the server plus `args`, communicating over
stdio. If your client prefers a typed config (e.g. an `mcp.json`), the
`command`/`args` pair above is all you need.

## Testing

`tests/test_mcp_stdio.py` spawns the server as a real subprocess and performs a
full handshake + `tools/call` round trip:

```bash
python -m pytest tests/test_mcp_stdio.py -q
```
