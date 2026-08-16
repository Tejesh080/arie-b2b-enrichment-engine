"""The ingestion / runtime API.

One write endpoint and two reads, which is the whole of M1 Step 9's HTTP
surface. ``POST /leads`` is the interesting one: it is the first thing in this
system to call identity resolution in a request path, and the first to create
work for the queue.

**Route handlers are sync (``def``, not ``async def``) on purpose.** psycopg is
a blocking driver; an ``async def`` handler calling it would block the event
loop and serialize every concurrent request behind one database round trip.
Declaring them sync makes Starlette run each in a worker thread, where blocking
is exactly what is expected. An async driver would be the other valid answer,
but not one worth adopting for three endpoints — and mixing the two is how you
get an app that is fast in tests and pathological under load.

Transaction handling reads as explicit ``conn.commit()`` calls, matching the
rest of the codebase. psycopg's pooled ``connection()`` context manager also
rolls back automatically when the block exits with an exception, which is what
makes the failure path safe without a ``try/except`` around every handler: an
error anywhere inside ingestion leaves no partial row behind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from psycopg_pool import ConnectionPool

from arie.api.ingest import ingest_lead
from arie.api.reads import fetch_lead
from arie.api.schemas import (
    HealthResponse,
    IngestLeadRequest,
    IngestLeadResponse,
    LeadCostResponse,
    LeadResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewResponse,
)
from arie.approval.workflow import (
    ReviewConflictError,
    ReviewNotFoundError,
    get_review,
    submit_decision,
)
from arie.config import DATABASE, OBSERVABILITY
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger
from arie.observability.tracing import configure_tracing, shutdown_tracing
from arie.statemachine.apply import OptimisticConcurrencyError


@dataclass(frozen=True)
class AppState:
    """The long-lived collaborators, built once at startup.

    All four share a single connection pool. They have to: ingestion resolves
    identity and enqueues a job on the *same* connection so both commit
    together, which is only possible if they aren't each holding their own.
    """

    pool: ConnectionPool
    resolver: IdentityResolver
    queue: PostgresJobQueue
    ledger: PostgresCostLedger


def build_state(conninfo: str, *, min_size: int = 1, max_size: int = 10) -> AppState:
    pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=True)
    return AppState(
        pool=pool,
        resolver=IdentityResolver(pool),
        queue=PostgresJobQueue(pool),
        ledger=PostgresCostLedger(pool),
    )


def get_state(request: Request) -> AppState:
    state: AppState | None = getattr(request.app.state, "arie", None)
    if state is None:  # pragma: no cover - only reachable if lifespan didn't run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="application not initialised"
        )
    return state


@contextmanager
def _transaction(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """A pooled connection whose transaction is rolled back if the body raises."""
    with pool.connection() as conn:
        yield conn


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not DATABASE.url:
        raise RuntimeError("DATABASE_URL is not set — see .env.example")

    configure_tracing()
    state = build_state(DATABASE.url)
    app.state.arie = state
    try:
        yield
    finally:
        state.pool.close()
        shutdown_tracing()


def create_app(*, state: AppState | None = None) -> FastAPI:
    """Build the app. Pass `state` to supply an already-built pool (tests do).

    When `state` is given the lifespan is skipped entirely — the caller owns
    the pool's lifetime and tracing setup, which is what lets a test point the
    app at its own fixtures without racing the real startup path.
    """
    app = FastAPI(
        title="Adaptive Revenue Intelligence Engine",
        version="0.1.0",
        summary="Lead ingestion and runtime API",
        lifespan=None if state is not None else lifespan,
    )
    if state is not None:
        app.state.arie = state

    register_routes(app)
    instrument(app)
    return app


def instrument(app: FastAPI) -> None:
    """Attach OTel's ASGI middleware, if tracing is configured.

    Guarded on a configured endpoint rather than attached unconditionally: with
    no provider installed every span would be a no-op anyway, and the
    middleware's per-request work would be pure overhead on the hot path.
    """
    if not OBSERVABILITY.enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


# `Annotated[..., Depends(...)]` rather than a `= Depends(...)` default: the
# default-argument form evaluates a call at function-definition time, which is
# the mutable-default-argument footgun ruff's B008 exists to catch. FastAPI
# treats the two identically and documents Annotated as the current style.
StateDep = Annotated[AppState, Depends(get_state)]


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz", response_model=HealthResponse)
    def healthz(state: StateDep) -> Response:
        """Liveness plus a real database round trip.

        A health check that doesn't touch the database would report healthy
        while every request 500s, which is worse than having no check at all —
        it actively suppresses the alert.
        """
        try:
            with state.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            database_up = True
        except psycopg.Error:
            database_up = False

        body = HealthResponse(status="ok" if database_up else "degraded", database=database_up)
        return Response(
            content=body.model_dump_json(),
            media_type="application/json",
            status_code=status.HTTP_200_OK if database_up else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.post(
        "/leads",
        response_model=IngestLeadResponse,
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "Lead already existed; the same lead is returned"}},
    )
    def post_lead(
        payload: IngestLeadRequest,
        response: Response,
        state: StateDep,
    ) -> IngestLeadResponse:
        with _transaction(state.pool) as conn:
            result = ingest_lead(
                conn, resolver=state.resolver, queue=state.queue, command=payload.to_command()
            )
            conn.commit()

        # 200 rather than 201 for a duplicate: the request was accepted and the
        # lead exists, but this call did not create it. A caller retrying a
        # webhook can tell the two apart without parsing the body, and neither
        # is an error — returning 409 would push idempotent redelivery into a
        # failure path it doesn't belong in.
        if not result.created:
            response.status_code = status.HTTP_200_OK

        return IngestLeadResponse(
            lead_id=result.lead_id,
            status=result.status,
            created=result.created,
            company_id=result.company_id,
            person_id=result.person_id,
            job_id=result.job_id,
            job_created=result.job_created,
        )

    @app.get("/leads/{lead_id}", response_model=LeadResponse)
    def get_lead(lead_id: UUID, state: StateDep) -> LeadResponse:
        with state.pool.connection() as conn:
            record = fetch_lead(conn, lead_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}")

        cost = state.ledger.lead_cost(lead_id)
        assert cost is not None  # v_lead_cost is LEFT JOINed off leads; the row exists

        return LeadResponse(
            lead_id=record.lead_id,
            status=record.status,
            version=record.version,
            source=record.source,
            external_ref=record.external_ref,
            company_id=record.company_id,
            person_id=record.person_id,
            budget_usd_cap=record.budget_usd_cap,
            created_at=record.created_at,
            updated_at=record.updated_at,
            cost=LeadCostResponse(
                provider_cost_usd=cost.provider_cost_usd,
                model_cost_usd=cost.model_cost_usd,
                total_cost_usd=cost.total_cost_usd,
                provider_calls=cost.provider_calls,
                cache_hits=cost.cache_hits,
                provider_latency_ms=cost.provider_latency_ms,
            ),
        )

    @app.get("/reviews/{review_id}", response_model=ReviewResponse)
    def get_review_endpoint(review_id: UUID, state: StateDep) -> ReviewResponse:
        with state.pool.connection() as conn:
            record = get_review(conn, review_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no review {review_id}"
            )

        return ReviewResponse(
            review_id=record.review_id,
            lead_id=record.lead_id,
            requested_at=record.requested_at,
            reviewer=record.reviewer,
            original_decision=record.original_decision,
            final_decision=record.final_decision,
            notes=record.notes,
            responded_at=record.responded_at,
            is_pending=record.is_pending,
            lead_status=record.lead_status,
            lead_version=record.lead_version,
        )

    @app.post("/reviews/{review_id}/decision", response_model=ReviewDecisionResponse)
    def submit_review_decision(
        review_id: UUID,
        payload: ReviewDecisionRequest,
        state: StateDep,
    ) -> ReviewDecisionResponse:
        # Every failure mode below rolls back the whole transaction, including
        # the review's own compare-and-swap update if it got that far — see
        # `arie.approval.workflow.submit_decision`'s docstring. `_transaction`'s
        # pooled connection rolls back on any exception leaving this block,
        # matching `post_lead`'s rollback story exactly.
        with _transaction(state.pool) as conn:
            try:
                result = submit_decision(
                    conn,
                    review_id=review_id,
                    action=payload.action,
                    reviewer=payload.reviewer,
                    notes=payload.notes,
                    expected_lead_version=payload.expected_lead_version,
                )
            except ReviewNotFoundError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except ReviewConflictError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except OptimisticConcurrencyError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            conn.commit()

        return ReviewDecisionResponse(
            review_id=result.review_id,
            lead_id=result.lead_id,
            action=result.action,
            final_decision=result.final_decision,
            reviewer=result.reviewer,
            notes=result.notes,
            responded_at=result.responded_at,
            lead_status=result.lead_status,
            lead_version=result.lead_version,
            already_applied=result.already_applied,
        )


# Module-level app for `uvicorn arie.api.main:app` (see the Makefile's `serve`
# target). Construction touches no I/O — the pool opens in the lifespan — so
# importing this module in a test or a type check costs nothing and needs no
# database.
app = create_app()
