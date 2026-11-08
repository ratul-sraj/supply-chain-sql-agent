.PHONY: all db test eval api ui clean

PY ?= python

all: db test

db:            ## build the SQLite database from scratch
	$(PY) scripts/build_db.py

test: db       ## run the test suite
	$(PY) -m pytest -q

eval: db       ## run the evaluation (offline template planner)
	$(PY) eval/run_eval.py --planner template

eval-llm: db   ## run the evaluation with the LLM planner (needs OPENAI_API_KEY)
	$(PY) eval/run_eval.py --planner llm

api: db        ## start the FastAPI service
	$(PY) -m uvicorn api.main:app --reload --port 8000

ui: db         ## start the Streamlit UI
	$(PY) -m streamlit run ui/app.py

mcp: db        ## run the MCP server on stdio
	$(PY) mcp_server/server.py

clean:         ## remove generated artefacts
	rm -f data/*.db eval/results.json eval/report.md
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
