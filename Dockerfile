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

CMD ["uvicorn", "arie.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
