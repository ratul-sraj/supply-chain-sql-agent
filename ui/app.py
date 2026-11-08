"""Streamlit UI for the supply-chain text-to-SQL agent.

Shows the generated SQL, the result table, and the full agent step trace.

Run:  streamlit run ui/app.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from sql_agent.db import SafeDB  # noqa: E402
from sql_agent.agent import SQLAgent, build_planner  # noqa: E402

st.set_page_config(page_title="Supply-Chain SQL Agent", page_icon="🏭", layout="wide")

EXAMPLES = [
    "How many late shipments are there?",
    "Total spend by currency",
    "Top suppliers by spend",
    "Inventory by warehouse",
    "Quality inspection results breakdown",
    "Worst suppliers by rating",
    "Defects by severity",
]


@st.cache_resource
def get_agent(planner: str) -> SQLAgent:
    return SQLAgent(db=SafeDB(), planner=build_planner(planner))


st.title("🏭 Supply-Chain Text-to-SQL Agent")
st.caption("Ask a question in English. The agent writes read-only SQL, runs it, "
           "and shows you the query, the rows, and its step trace.")

with st.sidebar:
    st.header("Settings")
    planner = st.selectbox("Planner", ["auto", "template", "llm"], index=0,
                           help="auto = LLM if OPENAI_API_KEY is set, else template")
    st.markdown("**Example questions**")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["question"] = ex

question = st.text_input("Your question",
                         value=st.session_state.get("question", EXAMPLES[0]))

if st.button("Ask", type="primary") or question:
    if question.strip():
        with st.spinner("Planning and executing..."):
            agent = get_agent(planner)
            res = agent.ask(question)

        c1, c2, c3 = st.columns(3)
        c1.metric("Rows", res.row_count)
        c2.metric("Latency", f"{res.latency_ms:.0f} ms")
        c3.metric("Planner", res.planner)

        st.subheader("Generated SQL")
        st.code(res.sql or "-- no SQL produced", language="sql")

        if res.error:
            st.error(res.error)
        else:
            st.subheader("Result")
            if res.columns:
                st.dataframe(pd.DataFrame(res.rows), use_container_width=True)
            else:
                st.info("Query returned no rows.")

        with st.expander("Agent step trace", expanded=True):
            if res.steps:
                st.dataframe(pd.DataFrame([s.__dict__ for s in res.steps]),
                             use_container_width=True)
            else:
                st.write("No steps recorded.")
