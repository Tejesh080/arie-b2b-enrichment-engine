.PHONY: help install dev-install lint fmt type test test-all dataset validate-dataset bench clean db-migrate serve worker sync-supabase-migrations check-supabase-migrations

help:
	@echo "Adaptive Revenue Intelligence Engine"
	@echo ""
	@echo "  make dev-install     Install with dev + service extras"
	@echo "  make lint            Ruff lint"
	@echo "  make fmt             Ruff format"
	@echo "  make type            mypy strict"
	@echo "  make test            Unit tests (no DB, no network)"
	@echo "  make test-all        Include integration tests (needs DATABASE_URL)"
	@echo ""
	@echo "  make dataset         Generate the seeded eval dataset"
	@echo "  make validate-dataset  Assert the dataset is non-trivial (CI gate)"
	@echo "  make bench           Run the full benchmark  [NO API KEYS NEEDED]"
	@echo ""
	@echo "  make db-migrate      Apply SQL migrations (the only path that touches production)"
	@echo "  make sync-supabase-migrations   Regenerate supabase/migrations/ from migrations/"
	@echo "  make check-supabase-migrations  CI gate: fail if the mirror has drifted"
	@echo "  make serve           Run the ingestion API"
	@echo "  make worker          Run a queue worker"

install:
	pip install -e .

dev-install:
	pip install -e ".[dev,service]"

lint:
	ruff check src tests bench scripts

fmt:
	ruff format src tests bench scripts
	ruff check --fix src tests bench scripts

type:
	mypy src tests scripts

test:
	pytest -m "not integration"

test-all:
	pytest

# --- M0: the proof -----------------------------------------------------------
# Everything below runs offline, deterministically, with zero credentials.

dataset:
	python -m arie.evalgen.cli --seed 42 --out data/eval/

validate-dataset:
	pytest tests/unit/test_dataset_is_nontrivial.py -v

bench:
	python -m bench.run_benchmark --dataset data/eval/ --out bench/out/

# --- M1: the system ----------------------------------------------------------

db-migrate:
	python scripts/migrate.py

sync-supabase-migrations:
	python scripts/sync_supabase_migrations.py

check-supabase-migrations:
	python scripts/sync_supabase_migrations.py --check

serve:
	uvicorn arie.api.main:app --reload --port 8000

worker:
	python -m arie.jobs.worker

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
