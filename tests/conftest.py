"""Shared pytest fixtures: ensure the project root is importable and the
database exists before any test runs."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB_PATH = os.path.join(ROOT, "data", "supply_chain.db")


@pytest.fixture(scope="session", autouse=True)
def _ensure_db():
    if not os.path.exists(DB_PATH):
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_db.py")],
                       check=True)
    yield
