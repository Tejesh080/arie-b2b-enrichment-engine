# ARIE Decision Copilot Runtime — AgentCore HTTP protocol, ARM64.
#
# ARM64 is not a preference. AgentCore Runtime executes on Graviton and an
# x86 image will not start at all, so this must be built with
# `docker buildx build --platform linux/arm64`.
#
# The image carries `arie.llm` and `arie.ledger.pricing` and nothing that
# talks to a database: no psycopg, no pandas, no scikit-learn. That is
# verified rather than assumed — see
# tests/unit/test_copilot_runtime.py::test_runtime_imports_no_database_driver.
# Keeping it true is what makes "this container cannot reach tenant data" a
# property of the build rather than a promise in a README.

FROM --platform=linux/arm64 python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Pinned rather than floating: an image AgentCore pulls months from now must
# behave the way the smoke test behaved, and `boto3>=1.35` silently picking up
# a new Converse contract is exactly the drift this service cannot detect on
# its own.
RUN pip install --no-cache-dir \
      "fastapi==0.120.2" \
      "uvicorn==0.38.0" \
      "boto3==1.43.100" \
      "pydantic==2.12.4" \
      "opentelemetry-sdk==1.40.0" \
      "python-dotenv==1.2.1"
# python-dotenv is imported by arie.config at module scope. There is no .env in
# this image and there must not be: configuration arrives as AgentCore
# environment variables, and secrets do not arrive at all.

# Only the two packages the runtime actually imports. Copying all of src/
# would pull in arie.api, arie.jobs and arie.live — modules that expect a
# database and have no business being reachable from this container.
COPY src/arie/__init__.py            /app/arie/__init__.py
COPY src/arie/config.py              /app/arie/config.py
COPY src/arie/llm/                   /app/arie/llm/
COPY src/arie/ledger/__init__.py     /app/arie/ledger/__init__.py
COPY src/arie/ledger/pricing.py      /app/arie/ledger/pricing.py
COPY src/arie/observability/         /app/arie/observability/
COPY services/copilot-runtime/app.py /app/app.py

# Non-root. AgentCore terminates TLS at its load balancer and the container
# only ever sees plaintext HTTP on the loopback side of that boundary, so the
# container's own privileges are the exposure that remains.
RUN useradd --create-home --uid 10001 runtime && chown -R runtime:runtime /app
USER runtime

EXPOSE 8080

# Single worker: AgentCore scales by adding instances, so a second worker here
# would double the memory billed per instance for no added concurrency the
# platform cannot already provide.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
