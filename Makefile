.PHONY: help install dev-install lint fmt type test test-all test-db test-db-status dataset validate-dataset bench clean db-migrate-test db-migrate-prod-check db-migrate-prod serve worker sync-supabase-migrations check-supabase-migrations

help:
	@echo "Adaptive Revenue Intelligence Engine"
	@echo ""
	@echo "  make dev-install     Install with dev + service extras"
	@echo "  make lint            Ruff lint"
	@echo "  make fmt             Ruff format"
	@echo "  make type            mypy strict"
	@echo "  make test            Unit tests (no DB, no network)"
	@echo "  make test-all        Include integration tests (needs TEST_DATABASE_URL)"
	@echo "  make test-db         Designate a disposable integration-test database"
	@echo "  make test-db-status  Show which database the integration suite would use"
	@echo ""
	@echo "  make dataset         Generate the seeded eval dataset"
	@echo "  make validate-dataset  Assert the dataset is non-trivial (CI gate)"
	@echo "  make bench           Run the full benchmark  [NO API KEYS NEEDED]"
	@echo ""
	@echo "  make db-migrate-test       Apply SQL migrations to TEST_DATABASE_URL"
	@echo "  make db-migrate-prod-check Dry-run production migrations (read-only)"
	@echo "  make db-migrate-prod       Apply SQL migrations to production (writes!)"
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

# Integration tests read TEST_DATABASE_URL and never fall back to DATABASE_URL,
# so a developer machine with a production .env cannot aim them at a deployment.
# `test-db` creates the database if needed and stamps the designation marker the
# suite requires; it refuses to stamp anything matching DATABASE_URL or holding
# existing data. See scripts/test_db.py.
test-db:
	python scripts/test_db.py designate

test-db-status:
	python scripts/test_db.py status

# --- M0: the proof -----------------------------------------------------------
# Everything below runs offline, deterministically, with zero credentials.

dataset:
	python -m arie.evalgen.cli --seed 42 --out data/eval/

validate-dataset:
	pytest tests/unit/test_dataset_is_nontrivial.py -v

bench:
	python -m bench.run_benchmark --dataset data/eval/ --out bench/out/

# --- M1: the system ----------------------------------------------------------

# Three explicit targets, no default. `scripts/migrate.py` refuses a bare
# invocation, so there is deliberately no `make db-migrate` that could write to
# production by being the shortest thing to type — see that script's docstring
# for the incident that removed it.
db-migrate-test:
	python scripts/migrate.py --target test --apply

db-migrate-prod-check:
	python scripts/migrate.py --target production --dry-run

db-migrate-prod:
	python scripts/migrate.py --target production --apply --confirm-production-write

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
