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
# Reads $PORT the same way the CMD below does, defaulting to 8000 so this
# stays correct with no env var set (local `docker run`, and every
# docker-compose.yml service, none of which set PORT).
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://localhost:' + os.environ.get('PORT', '8000') + '/healthz', timeout=2)"]

# `sh -c` rather than exec-form `uvicorn ...`: a hosting platform that assigns
# its own port (Railway, Fly, Render, ...) injects it as $PORT, and exec form
# never expands shell variables — the container would keep listening on 8000
# while the platform's health check probed whatever port it assigned instead.
# `${PORT:-8000}` keeps `docker-compose.yml`'s explicit `command:` override
# (which doesn't set PORT) and a bare `docker run` on 8000, unchanged.
CMD ["sh", "-c", "uvicorn arie.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
