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

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, UploadFile, status
from psycopg_pool import ConnectionPool

from arie.api.ingest import ingest_lead
from arie.api.reads import fetch_lead
from arie.api.receipt import build_receipt
from arie.api.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    BatchProgressResponse,
    BatchResponse,
    BatchRowResponse,
    BatchRowsPageResponse,
    CreateApiKeyRequest,
    CreateICPProfileRequest,
    HealthResponse,
    ICPProfileResponse,
    IngestLeadRequest,
    IngestLeadResponse,
    LeadCostResponse,
    LeadResponse,
    OrganizationResponse,
    ReceiptResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewResponse,
    UpdateOrganizationRequest,
    UsageSummaryResponse,
)
from arie.apikeys import create_api_key, list_api_keys, looks_like_api_key, revoke_api_key
from arie.approval.workflow import (
    ReviewConflictError,
    ReviewNotFoundError,
    get_review,
    submit_decision,
)
from arie.auth import (
    AuthContext,
    AuthenticationError,
    InvalidApiKeyError,
    NotAMemberError,
    RevokedApiKeyError,
    resolve_api_key_context,
    resolve_auth_context,
)
from arie.batches import (
    MAX_FILE_SIZE_BYTES,
    BatchProgress,
    BatchRecord,
    MalformedCsvError,
    batch_progress,
    create_batch,
    get_batch,
    list_batch_rows,
    list_batches,
)
from arie.config import DATABASE, OBSERVABILITY
from arie.icp_profiles import (
    create_profile as create_icp_profile_row,
)
from arie.icp_profiles import (
    get_active_profile,
    get_profile_by_version,
    list_profiles,
)
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger
from arie.migrations import MigrationsDirectoryError, pending_migrations
from arie.observability.tracing import configure_tracing, shutdown_tracing
from arie.organizations import (
    InvalidOrganizationSettingsError,
    get_organization,
    update_organization,
)
from arie.statemachine.apply import OptimisticConcurrencyError
from arie.usage import get_usage_summary

_LOGGER = logging.getLogger("arie.api")


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
    register_error_shaping(app)
    instrument(app)
    return app


def register_error_shaping(app: FastAPI) -> None:
    """Every 5xx this API emits carries a JSON ``detail``, like its 4xxs do.

    Without these, an unhandled exception falls through to Starlette's
    ServerErrorMiddleware, which answers with *plain-text* "Internal Server
    Error" — the one response shape the frontend's message extraction can't
    read, so users saw the raw fallback string "ARIE request failed (500)".
    Shaping is all this does: the error is still logged with its traceback,
    and nothing is retried or swallowed.

    ``psycopg.OperationalError`` gets its own handler because it is the one
    *expected* transient (a dropped pooler connection, a database restart):
    503 says "try again shortly" where a bare 500 says "something broke".
    """

    @app.exception_handler(psycopg.OperationalError)
    async def _database_unreachable(request: Request, exc: psycopg.OperationalError) -> Response:
        _LOGGER.exception("database unreachable during %s %s", request.method, request.url.path)
        return _json_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ARIE's database was briefly unreachable. Nothing was lost — retry in a moment.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        _LOGGER.exception("unhandled error during %s %s", request.method, request.url.path)
        return _json_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "ARIE hit an unexpected internal error handling this request. "
            "It has been logged; retrying is safe.",
        )


def _json_error(code: int, detail: str) -> Response:
    return Response(
        content=json.dumps({"detail": detail}),
        media_type="application/json",
        status_code=code,
    )


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


def get_auth_context(request: Request, state: StateDep) -> AuthContext:
    """The auth boundary every customer-facing endpoint below depends on —
    which is what makes "forgetting the organization_id filter" impossible to
    do *silently*: a handler with no `AuthDep` parameter simply has no
    organization_id to scope with in the first place. `/healthz` is the one
    deliberate exception: an infra liveness probe has no caller identity to
    check.

    Two authentication paths share one bearer-token header, told apart by
    `arie.apikeys.looks_like_api_key`'s prefix check on the token itself —
    never by a second header or a client-declared type:

    * An ARIE organization API key (Productization M2A) — `organization_id`
      comes from the key alone; `X-Organization-Id` is never even read on
      this path, so it cannot be trusted (or mistrusted) for anything.
    * A Supabase user JWT (Productization M1) — `organization_id` comes from
      the required `X-Organization-Id` header, checked against the token's
      membership.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header — expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer ") :].strip()

    if looks_like_api_key(token):
        try:
            return resolve_api_key_context(state.pool, raw_key=token)
        except (InvalidApiKeyError, RevokedApiKeyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc) if isinstance(exc, RevokedApiKeyError) else "invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    org_header = request.headers.get("x-organization-id")
    if not org_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="missing X-Organization-Id header"
        )
    try:
        organization_id = UUID(org_header)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-Id is not a valid UUID",
        ) from exc

    try:
        return resolve_auth_context(state.pool, token=token, organization_id=organization_id)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except NotAMemberError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


AuthDep = Annotated[AuthContext, Depends(get_auth_context)]


def _require_scope(auth: AuthContext, scope: str) -> None:
    """Gate a data-plane action on `scope`. A no-op for a JWT session (see
    `AuthContext.has_scope`); refuses an API key that wasn't granted it."""
    if not auth.has_scope(scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"missing required scope: {scope}"
        )


def _require_org_admin(auth: AuthContext) -> None:
    """Gate an organization-management action (API key create/list/revoke
    today) on an owner/admin *JWT* session — never satisfiable by an API key,
    however it's scoped; see `AuthContext.is_org_admin`."""
    if not auth.is_org_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requires an owner or admin session (API keys cannot manage API keys)",
        )


def _require_jwt_session(auth: AuthContext) -> None:
    """Gate an action (reading ICP configuration, or any CSV batch endpoint)
    on any authenticated *human* session — owner, admin, or analyst_reviewer
    alike; unlike `_require_org_admin` this is not owner/admin-only. Refuses
    an API key outright rather than adding a new scope: no existing machine
    caller (n8n, the demo script) needs organization configuration or batch
    upload, and Productization M3's brief asks not to add a scope without a
    concrete use case."""
    if auth.auth_method != "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requires a user session (API keys cannot read organization configuration)",
        )


MAX_UPLOAD_CONTENT_LENGTH = MAX_FILE_SIZE_BYTES + 10_000
"""`arie.batches.MAX_FILE_SIZE_BYTES` plus headroom for multipart boundary
overhead — see `upload_batch`'s own comment for why this is only a coarse,
early rejection, not the authoritative limit."""


def _to_batch_response(record: BatchRecord, progress: BatchProgress) -> BatchResponse:
    """Combine a `BatchRecord` and a separately computed `BatchProgress` into
    one response — the same composite-response shape `create_api_key_endpoint`
    already uses for `ApiKeyCreatedResponse`, needed here because the two
    inputs are genuinely two different service calls, not one object
    `model_validate` could read straight off."""
    return BatchResponse(
        batch_id=record.batch_id,
        organization_id=record.organization_id,
        filename=record.filename,
        total_rows=record.total_rows,
        accepted_rows=record.accepted_rows,
        rejected_rows=record.rejected_rows,
        created_by_user_id=record.created_by_user_id,
        created_at=record.created_at,
        progress=BatchProgressResponse.model_validate(progress),
    )


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz", response_model=HealthResponse)
    def healthz(state: StateDep) -> Response:
        """Liveness, database connectivity, and schema readiness — reported
        separately, because they call for different fixes.

        A health check that doesn't touch the database would report healthy
        while every request 500s, which is worse than having no check at all —
        it actively suppresses the alert. But a *reachable* database with an
        *incomplete* schema is a different failure than an unreachable one:
        it's the clean-start race the Compose ``migrate`` service's
        ``service_completed_successfully`` gate exists to close, caught here
        too for deployments that don't go through Compose. Collapsing both
        into one boolean would tell an operator to restart the process for a
        problem restarting it can't fix.

        A third failure mode lands in the same ``degraded`` bucket:
        ``pending_migrations`` raising ``MigrationsDirectoryError`` means this
        process cannot even tell what should be applied — reporting ``ok`` in
        that case would be worse than reporting ``degraded`` for a real
        pending migration, because there would be no signal to act on at all.
        ``database_up`` is already ``True`` by the time that can happen, so
        the result is indistinguishable from "migrating" from the outside,
        which is the conservative direction to be wrong in.
        """
        database_up = False
        schema_ready = False
        try:
            with state.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                database_up = True
                schema_ready = not pending_migrations(conn)
        except (psycopg.Error, MigrationsDirectoryError):
            pass

        if database_up and schema_ready:
            overall, code = "ok", status.HTTP_200_OK
        elif database_up:
            overall, code = "degraded", status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            overall, code = "down", status.HTTP_503_SERVICE_UNAVAILABLE

        body = HealthResponse(status=overall, database=database_up, schema_ready=schema_ready)
        return Response(
            content=body.model_dump_json(), media_type="application/json", status_code=code
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
        auth: AuthDep,
    ) -> IngestLeadResponse:
        _require_scope(auth, "leads:write")
        with _transaction(state.pool) as conn:
            result = ingest_lead(
                conn,
                resolver=state.resolver,
                queue=state.queue,
                command=payload.to_command(organization_id=auth.organization_id),
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
            job_requeued=result.job_requeued,
            is_shadow=result.is_shadow,
        )

    @app.get("/leads/{lead_id}", response_model=LeadResponse)
    def get_lead(lead_id: UUID, state: StateDep, auth: AuthDep) -> LeadResponse:
        _require_scope(auth, "leads:read")
        with state.pool.connection() as conn:
            record = fetch_lead(conn, lead_id, organization_id=auth.organization_id)
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
            is_shadow=record.is_shadow,
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

    @app.get("/leads/{lead_id}/receipt", response_model=ReceiptResponse)
    def get_lead_receipt(lead_id: UUID, state: StateDep, auth: AuthDep) -> ReceiptResponse:
        """The Decision Receipt — why ARIE stopped spending and what it decided.

        Never 404s for a lead that exists but hasn't reached a decision yet;
        ``status`` distinguishes "pending" (still mid-pipeline), "processing_failed"
        (dead-lettered before a decision), and "decided" — see
        ``arie.api.receipt.DecisionReceipt``.
        """
        _require_scope(auth, "leads:read")
        with state.pool.connection() as conn:
            receipt = build_receipt(
                conn, state.ledger, lead_id, organization_id=auth.organization_id
            )
        if receipt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}")
        return ReceiptResponse.from_receipt(receipt)

    @app.get("/reviews/{review_id}", response_model=ReviewResponse)
    def get_review_endpoint(review_id: UUID, state: StateDep, auth: AuthDep) -> ReviewResponse:
        _require_scope(auth, "reviews:read")
        with state.pool.connection() as conn:
            record = get_review(conn, review_id, organization_id=auth.organization_id)
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
        auth: AuthDep,
    ) -> ReviewDecisionResponse:
        _require_scope(auth, "reviews:write")
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
                    organization_id=auth.organization_id,
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

    # -------------------------------------------------- organization settings --
    #
    # Productization M4 Part 1. Same permission split as ICP configuration
    # just below: read open to any active member (`_require_jwt_session`),
    # write owner/admin-only (`_require_org_admin`). No API-key scope for
    # either — organization settings are not a data-plane concern any
    # existing machine caller touches.

    @app.get("/organization", response_model=OrganizationResponse)
    def get_organization_endpoint(state: StateDep, auth: AuthDep) -> OrganizationResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            record = get_organization(conn, organization_id=auth.organization_id)
        if record is None:  # pragma: no cover - an authenticated org always exists
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        return OrganizationResponse.model_validate(record)

    @app.patch("/organization", response_model=OrganizationResponse)
    def update_organization_endpoint(
        payload: UpdateOrganizationRequest, state: StateDep, auth: AuthDep
    ) -> OrganizationResponse:
        _require_org_admin(auth)
        updates = payload.model_dump(exclude_unset=True)
        try:
            with _transaction(state.pool) as conn:
                record = update_organization(
                    conn, organization_id=auth.organization_id, updates=updates
                )
        except InvalidOrganizationSettingsError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        if record is None:  # pragma: no cover - an authenticated org always exists
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        return OrganizationResponse.model_validate(record)

    # ------------------------------------------------------- icp profiles --
    #
    # Productization M3. Config-write requires an owner/admin *JWT* session
    # (`_require_org_admin`, same rule as api-keys below); config-read only
    # requires any authenticated human session (`_require_jwt_session`) — an
    # active member of any role may see what "a good lead" currently means
    # for their organization, only owner/admin may change it. No API-key
    # scope exists for either: no machine caller needs this today.
    #
    # `/organization/icp/versions` is registered before `/organization/icp/
    # {version}` deliberately — Starlette matches routes in registration
    # order, and `{version}` is typed `int`, so registering it first would
    # make a request for the literal path `versions` fail int coercion
    # (422) instead of falling through to the static route below it.

    @app.get("/organization/icp", response_model=ICPProfileResponse)
    def get_active_icp_profile(state: StateDep, auth: AuthDep) -> ICPProfileResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            record = get_active_profile(conn, organization_id=auth.organization_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="this organization has no active ICP profile",
            )
        return ICPProfileResponse.model_validate(record)

    @app.get("/organization/icp/versions", response_model=list[ICPProfileResponse])
    def list_icp_profiles(state: StateDep, auth: AuthDep) -> list[ICPProfileResponse]:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            records = list_profiles(conn, organization_id=auth.organization_id)
        return [ICPProfileResponse.model_validate(record) for record in records]

    @app.get("/organization/icp/{version}", response_model=ICPProfileResponse)
    def get_icp_profile_version(version: int, state: StateDep, auth: AuthDep) -> ICPProfileResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            record = get_profile_by_version(
                conn, organization_id=auth.organization_id, version=version
            )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no ICP profile version {version}"
            )
        return ICPProfileResponse.model_validate(record)

    @app.post(
        "/organization/icp", response_model=ICPProfileResponse, status_code=status.HTTP_201_CREATED
    )
    def create_icp_profile(
        payload: CreateICPProfileRequest, state: StateDep, auth: AuthDep
    ) -> ICPProfileResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        with _transaction(state.pool) as conn:
            record = create_icp_profile_row(
                conn,
                organization_id=auth.organization_id,
                created_by_user_id=auth.user_id,
                name=payload.name,
                config=payload.config.model_dump(mode="json"),
            )
        return ICPProfileResponse.model_validate(record)

    # ------------------------------------------------------------ batches --
    #
    # Productization M3, Part 4-6: CSV bulk lead upload. Gated on any
    # authenticated human session (`_require_jwt_session`) — the same
    # permission tier as `POST /leads` itself (any JWT role can already
    # create leads one at a time; uploading many at once needs no more
    # authority than that) — never an API key, since no machine caller needs
    # this in this milestone.
    #
    # `MAX_UPLOAD_CONTENT_LENGTH` is a coarse, spoofable `Content-Length`
    # pre-check purely to avoid reading an obviously oversized body into
    # memory at all; `arie.batches.parse_csv`'s own post-read size check is
    # the real, authoritative limit.

    @app.post("/batches", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
    def upload_batch(
        request: Request, state: StateDep, auth: AuthDep, file: UploadFile
    ) -> BatchResponse:
        _require_jwt_session(auth)
        assert auth.user_id is not None  # guaranteed by auth_method == "jwt"
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_UPLOAD_CONTENT_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds the {MAX_UPLOAD_CONTENT_LENGTH}-byte limit",
            )
        content = (
            file.file.read()
        )  # sync read of the underlying spooled file — see module docstring
        try:
            with _transaction(state.pool) as conn:
                record = create_batch(
                    conn,
                    resolver=state.resolver,
                    queue=state.queue,
                    organization_id=auth.organization_id,
                    created_by_user_id=auth.user_id,
                    filename=file.filename or "upload.csv",
                    content=content,
                )
                progress = batch_progress(conn, organization_id=auth.organization_id, batch=record)
        except MalformedCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return _to_batch_response(record, progress)

    @app.get("/batches", response_model=list[BatchResponse])
    def list_batches_endpoint(
        state: StateDep,
        auth: AuthDep,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[BatchResponse]:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            records = list_batches(
                conn, organization_id=auth.organization_id, limit=limit, offset=offset
            )
            return [
                _to_batch_response(
                    record, batch_progress(conn, organization_id=auth.organization_id, batch=record)
                )
                for record in records
            ]

    @app.get("/batches/{batch_id}", response_model=BatchResponse)
    def get_batch_endpoint(batch_id: UUID, state: StateDep, auth: AuthDep) -> BatchResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            record = get_batch(conn, organization_id=auth.organization_id, batch_id=batch_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"no batch {batch_id}"
                )
            progress = batch_progress(conn, organization_id=auth.organization_id, batch=record)
        return _to_batch_response(record, progress)

    @app.get("/batches/{batch_id}/leads", response_model=BatchRowsPageResponse)
    def list_batch_rows_endpoint(
        batch_id: UUID,
        state: StateDep,
        auth: AuthDep,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> BatchRowsPageResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            batch = get_batch(conn, organization_id=auth.organization_id, batch_id=batch_id)
            if batch is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"no batch {batch_id}"
                )
            rows = list_batch_rows(
                conn,
                organization_id=auth.organization_id,
                batch_id=batch_id,
                limit=limit,
                offset=offset,
            )
        return BatchRowsPageResponse(
            items=[BatchRowResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
            total=batch.total_rows,
        )

    # -------------------------------------------------------------- usage --
    #
    # Productization M3, Part 7. Read-gated identically to ICP configuration
    # and batches (`_require_jwt_session`) — any active member, no API key.
    # `from_`/`to` default to the trailing 30 days ending now when omitted,
    # so the endpoint is usable with no query parameters at all.

    @app.get("/usage", response_model=UsageSummaryResponse)
    def get_usage(
        state: StateDep,
        auth: AuthDep,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: datetime | None = None,
    ) -> UsageSummaryResponse:
        _require_jwt_session(auth)
        to_at = to if to is not None else datetime.now(UTC)
        from_at = from_ if from_ is not None else to_at - timedelta(days=30)
        if from_at >= to_at:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'from' must be strictly before 'to'",
            )
        with state.pool.connection() as conn:
            summary = get_usage_summary(
                conn, organization_id=auth.organization_id, from_at=from_at, to_at=to_at
            )
        return UsageSummaryResponse.model_validate(summary)

    # ---------------------------------------------------------- api keys --
    #
    # Productization M2A. Every route here requires an owner/admin *JWT*
    # session (`_require_org_admin`) — an API key can never manage API keys,
    # including the one authenticating the request that asks, regardless of
    # its own scopes. `organization_id` is always `auth.organization_id`,
    # never a path parameter, matching every other tenant-scoped route in
    # this API — there is no way to address another organization's keys at
    # all, not even one that would be rejected.

    @app.post(
        "/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED
    )
    def create_api_key_endpoint(
        payload: CreateApiKeyRequest, state: StateDep, auth: AuthDep
    ) -> ApiKeyCreatedResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        with _transaction(state.pool) as conn:
            record, raw_key = create_api_key(
                conn,
                organization_id=auth.organization_id,
                created_by_user_id=auth.user_id,
                label=payload.label,
                scopes=payload.scopes,
            )
        base = ApiKeyResponse.model_validate(record)
        return ApiKeyCreatedResponse(**base.model_dump(), raw_key=raw_key)

    @app.get("/api-keys", response_model=list[ApiKeyResponse])
    def list_api_keys_endpoint(state: StateDep, auth: AuthDep) -> list[ApiKeyResponse]:
        _require_org_admin(auth)
        with state.pool.connection() as conn:
            records = list_api_keys(conn, organization_id=auth.organization_id)
        return [ApiKeyResponse.model_validate(record) for record in records]

    @app.post("/api-keys/{key_id}/revoke", response_model=ApiKeyResponse)
    def revoke_api_key_endpoint(key_id: UUID, state: StateDep, auth: AuthDep) -> ApiKeyResponse:
        _require_org_admin(auth)
        with _transaction(state.pool) as conn:
            record = revoke_api_key(conn, organization_id=auth.organization_id, key_id=key_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no API key {key_id}"
            )
        return ApiKeyResponse.model_validate(record)


# Module-level app for `uvicorn arie.api.main:app` (see the Makefile's `serve`
# target). Construction touches no I/O — the pool opens in the lifespan — so
# importing this module in a test or a type check costs nothing and needs no
# database.
app = create_app()
