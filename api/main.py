"""FastAPI service exposing the text-to-SQL agent.

Endpoints:
    POST /ask     {question, planner?} -> {question, sql, columns, rows, steps, latency_ms, error}
    GET  /schema  -> the compact schema card + table list
    GET  /health  -> liveness
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from sql_agent.db import SafeDB  # noqa: E402
from sql_agent.agent import SQLAgent, build_planner, build_schema_card  # noqa: E402

app = FastAPI(title="Supply-Chain Text-to-SQL Agent", version="1.0.0")

_DB = SafeDB()
_SCHEMA_CARD = build_schema_card(_DB)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="natural-language question")
    planner: str = Field("auto", pattern="^(auto|llm|template)$")


class AskResponse(BaseModel):
    question: str
    sql: str | None
    columns: list[str]
    rows: list[dict]
    row_count: int
    steps: list[dict]
    latency_ms: float
    error: str | None
    planner: str
    repaired: bool


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": os.path.basename(_DB.path),
            "tables": len(_DB.list_tables())}


@app.get("/schema")
def schema() -> dict:
    return {"schema_card": _SCHEMA_CARD, "tables": _DB.list_tables()}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    agent = SQLAgent(db=_DB, planner=build_planner(req.planner))
    res = agent.ask(req.question)
    return AskResponse(**res.to_dict())
