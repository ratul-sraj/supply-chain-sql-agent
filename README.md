# Supply-Chain Text-to-SQL Agent (with MCP server)

Ask a plain-English question about a supply-chain database → get **safe, guarded
SQL**, the result rows, and the agent's step trace. Exposed three ways: a Python
agent, a FastAPI service, a Streamlit UI, **and an MCP server** so any
Model-Context-Protocol client (Claude Desktop, Cursor, …) can use it as tools.

> Built as a portfolio project targeting **AI Engineer / Forward Deployed
> Engineer** roles: the point is not "an LLM writes SQL" but that the whole
> pipeline is *guarded, self-repairing, evaluated, and integrable*.

## Problem

Business users can't write SQL, but letting an LLM run raw SQL against a
production warehouse is dangerous: it can drop tables, scan billions of rows, or
silently return the wrong number. This project shows the full loop —
introspect → plan → execute → observe → repair — with hard guardrails at the
execution boundary and a real evaluation harness that measures accuracy.

## Architecture

```
                ┌──────────────────────────────────────────────┐
   user ──────▶ │  SQLAgent (sql_agent/agent.py)               │
   question     │   schema card ─▶ Planner ─▶ {tool | SQL}     │
                │            ▲          │                      │
                │            └ observation ┘  (repair ≤3×, ≤6 steps)
                └───────┬──────────────────────────────────────┘
                        │ uses
                        ▼
        ┌───────────────────────────────┐        ┌──────────────────────────┐
        │ SafeDB (sql_agent/db.py)      │        │  Planners                │
        │  read-only conn + query_only  │        │  • LLMPlanner (OPENAI_*) │
        │  SELECT/WITH-only guard       │        │  • TemplatePlanner        │
        │  banned keywords, auto LIMIT  │        │    (offline, ~40 intents) │
        │  timeout, row cap, err map    │        └──────────────────────────┘
        └───────┬───────────────────────┘
                │  ◀── MCP BOUNDARY ── everything above is exposed as MCP tools
                ▼
   ┌────────────────────────┐   ┌──────────────┐   ┌──────────────┐
   │ mcp_server/server.py   │   │ api/main.py  │   │ ui/app.py    │
   │ stdio JSON-RPC 2.0     │   │ FastAPI /ask │   │ Streamlit    │
   │ (Claude Desktop, …)    │   │ /schema /health │  │ SQL + trace  │
   └────────────────────────┘   └──────────────┘   └──────────────┘
                ▼
        data/supply_chain.db  (9 tables, ~7.6k rows, synthetic + messy)
```

**The MCP boundary** is `mcp_server/server.py`: it imports the *same* `SafeDB`
and `SQLAgent` and exposes them as five MCP tools (`list_tables`,
`describe_table`, `run_sql`, `sample_rows`, `ask_question`). Any MCP client gets
the guarded SQL layer for free — no separate code path, no re-implemented guards.

## Quickstart

```bash
# 0. install
pip install -r requirements.txt          # MCP server needs nothing extra

# 1. build the database (deterministic, seed 42)
python scripts/build_db.py

# 2. ask a question from the CLI
python -m sql_agent.agent --planner template "how many late shipments are there?"

# 3. run the tests
python -m pytest -q                       # 119 passed

# 4. run the evaluation
python eval/run_eval.py --planner template

# 5. serve it
python -m uvicorn api.main:app --reload --port 8000      # API
python -m streamlit run ui/app.py                        # UI
python mcp_server/server.py                              # MCP (stdio)
```

### LLM planner

Set the standard OpenAI-compatible env vars and the agent routes to the LLM
planner automatically (`--planner auto`), or force it with `--planner llm`:

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1   # or any compatible gateway
export MODEL=gpt-4o-mini
python -m sql_agent.agent --planner llm "which supplier has the highest rating?"
```

With **no key**, everything still runs end-to-end on the deterministic
`TemplatePlanner` — that is exactly how the evaluation below was produced.

## Evaluation (measured)

Two question sets, reported separately on purpose. Gold SQL is executed against
the real database, so expected hashes and row counts are ground truth, and
execution accuracy compares the **result set** (order-insensitive unless the
gold query is ordered).

| Set | What it measures | Execution accuracy | Valid-SQL rate |
| --- | --- | --- | --- |
| `eval/questions.jsonl` (40) | **intent coverage** — questions written alongside the template router | 100.0% (40/40) | 100.0% |
| `eval/heldout.jsonl` (20) | **generalisation** — independently phrased, never used to build the router | **0.0% (0/20)** | 15.0% (3/20) |

The first row is a regression check, not a benchmark: the question set and the
keyword router were built together, so scoring 100% only proves the router still
covers its own intents. The second row is the honest number. On 20 paraphrases
(*"How many consignments have not arrived yet?"*, *"Which month saw the largest
order value?"*, *"What fraction of order lines arrived complete?"*) the offline
planner abstains on 17 and produces a runnable-but-wrong statement on 3.

That gap is the point of the project's architecture: **the planner is a
swappable component, the value is the layer underneath** — the guarded execution
boundary, the plan/observe/repair loop, the MCP surface, and an evaluation
harness honest enough to publish a 0% row. A nearest-intent cosine fallback was
tried and deleted: it lifted valid-SQL rate to 90% with 0 correct answers, which
is worse than abstaining. With an API key, `--planner llm` replaces the router
and the same harness measures it on the same two sets.

Reproduce:

```bash
python eval/run_eval.py --planner template                          # coverage
python eval/run_eval.py --planner template --questions eval/heldout.jsonl \
       --results eval/heldout_results.json --report eval/heldout_report.md
```

Full per-question breakdowns: [`eval/report.md`](eval/report.md),
[`eval/heldout_report.md`](eval/heldout_report.md).

**Repair rate is 0% here on purpose:** the template planner emits one canned
statement and never errs, so there is nothing to repair. The self-repair path is
covered by `tests/test_agent.py::test_agent_self_repairs`, which drives the loop
with a fake model that returns broken SQL first and good SQL after seeing the
error.

## Guardrails / security

Untrusted, model-generated SQL is defended in depth in `sql_agent/db.py`:

1. **Read-only connection** — opened `file:…?mode=ro` *and* `PRAGMA query_only=ON`,
   so SQLite itself refuses writes even if the static guard were bypassed.
2. **Single-statement guard** — only one `SELECT`/`WITH` may run; `;`-separated
   multi-statements are rejected.
3. **Banned keywords** — `INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA/ALTER/CREATE/
   REPLACE/VACUUM/…` are rejected as whole words.
4. **Automatic LIMIT + row cap** — queries without a `LIMIT` are wrapped and
   capped (default 500 rows) so a model can't stream a whole table.
5. **Statement timeout** — a SQLite progress handler aborts queries past the
   wall-clock budget and maps it to an actionable message.
6. **Actionable error mapping** — raw SQLite errors become agent-legible hints
   ("No such column: foo → call describe_table …").
7. **`explain` fallback** — the agent can ask `EXPLAIN QUERY PLAN` without
   executing.

Every guard has a dedicated test in `tests/test_db_guards.py`.

## Repository layout

```
scripts/build_db.py     deterministic synthetic DB builder + messiness
sql_agent/db.py         SafeDB: the guarded read-only SQL layer
sql_agent/agent.py      SQLAgent loop, schema card, LLM + Template planners
mcp_server/server.py    stdio JSON-RPC 2.0 MCP server (zero deps)
mcp_server/README.md    how to register it in Claude Desktop / any MCP client
api/main.py             FastAPI: POST /ask, GET /schema, GET /health
ui/app.py               Streamlit UI: SQL, result table, step trace
eval/                   questions.jsonl (40), heldout.jsonl (20), run_eval.py, build_heldout.py, reports
tests/                  db guards, agent self-repair, MCP subprocess, API, eval
docs/schema.md          full schema + the deliberate messiness
```

## Limitations / honest notes

- **The database is synthetic.** The messiness (dupes, NULLs, currency mix, the
  misleadingly-named `product_master`) is realistic but hand-designed, so
  absolute accuracy numbers won't transfer to a real warehouse untouched.
- **The offline planner is keyword routing, not generalisation.** Its 100% on
  this 40-question eval reflects that the templates now cover these exact
  intents; it will not generalise to unseen phrasings, which is where the LLM
  planner (and its schema card) comes in. Unmapped questions return an explicit
  error rather than a guess.
- **Repair-rate and multi-step numbers are only meaningful with the LLM planner**
  (the template planner never fails a statement). The repair loop itself is
  unit-tested with a fake model.
- **No result-value tolerance.** Accuracy compares result sets by values/order;
  rounding differences count as misses.
- **`response_format: json_object`** is assumed by the LLM planner; some
  gateways ignore it, in which case the parser still strips markdown fences.

## License

MIT © Ratul S Raj, 2026. See [LICENSE](LICENSE).
