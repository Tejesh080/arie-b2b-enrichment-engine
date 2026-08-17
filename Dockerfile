FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits don't invalidate the pip cache.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && \
    mkdir -p src/arie && touch src/arie/__init__.py && \
    pip install -e ".[service]"

COPY src/ src/
COPY migrations/ migrations/
COPY scripts/ scripts/

# Non-root runtime user.
RUN useradd --create-home --uid 1000 arie && chown -R arie:arie /app
USER arie

EXPOSE 8000

# Targets the API role's /healthz — reachable, database up, schema fully
# migrated (see arie.api.main.healthz's own docstring for why those are
# reported separately). A container built from this same image running the
# worker command instead serves no HTTP port and has nothing at :8000 to
# check; docker-compose.yml's `worker` service explicitly disables this
# rather than leaving it to fail forever. No curl in the slim base image, so
# this shells out to the interpreter already on PATH instead of adding one.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://localhost:8000/healthz', timeout=2)"]

CMD ["uvicorn", "arie.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
