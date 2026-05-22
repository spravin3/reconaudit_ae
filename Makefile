DB         := reconaudit.duckdb
DBT        := .venv/bin/dbt
PYTEST     := .venv/bin/pytest
PYTHON     := uv run python
DBT_ARGS   := --profiles-dir . --no-partial-parse
DBT_ENV    := DUCKDB_PATH=../$(DB)

.PHONY: install pipeline transform test-dbt test-py test docs audit audit-loop report all clean

install:
	uv sync

# ── Extraction layer ────────────────────────────────────────────────────────
pipeline:
	$(PYTHON) -m pipeline.run

# ── Transform layer ─────────────────────────────────────────────────────────
transform:
	cd transform && $(DBT_ENV) ../$(DBT) run $(DBT_ARGS)

# ── Tests ───────────────────────────────────────────────────────────────────
test-dbt:
	cd transform && $(DBT_ENV) ../$(DBT) test $(DBT_ARGS)

test-py:
	$(PYTEST) tests/ -v

test: test-py test-dbt

# ── dbt docs (opens browser) ─────────────────────────────────────────────────
docs:
	cd transform && $(DBT_ENV) ../$(DBT) docs generate $(DBT_ARGS)
	cd transform && $(DBT_ENV) ../$(DBT) docs serve $(DBT_ARGS)

# ── Audit agent ─────────────────────────────────────────────────────────────
audit:
	$(PYTHON) -m audit.agent

# ── Autonomous audit loop (multi-turn LLM + SQL verification) ────────────────
audit-loop:
	$(PYTHON) -m audit.loop

# ── HTML report (open in browser) ────────────────────────────────────────────
report:
	$(PYTHON) report.py

# ── Full pipeline + report ───────────────────────────────────────────────────
all: pipeline transform test audit report

clean:
	rm -f $(DB) $(DB).wal
	rm -rf transform/target transform/dbt_packages
	rm -f notes/executive_memo.md
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .dlt -exec rm -rf {} + 2>/dev/null || true
