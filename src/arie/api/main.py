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
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from psycopg_pool import ConnectionPool

from arie.api.ingest import ingest_lead
from arie.api.reads import fetch_lead
from arie.api.receipt import DecisionReceipt, build_receipt
from arie.api.schemas import (
    AcceptInvitationRequest,
    AcceptProposalRequest,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    BatchProgressResponse,
    BatchResponse,
    BatchRowResponse,
    BatchRowsPageResponse,
    BillingPortalRequest,
    BillingPortalResponse,
    BillingResponse,
    CanonicalFieldResponse,
    CheckoutSessionResponse,
    CreateApiKeyRequest,
    CreateICPProfileRequest,
    CreateInvitationRequest,
    CreateOrganizationRequest,
    CreateOrganizationResponse,
    EffectiveEntitlementsResponse,
    FeedbackResponse,
    HealthResponse,
    ICPProfileResponse,
    IngestLeadRequest,
    IngestLeadResponse,
    InvitationCreatedResponse,
    InvitationResponse,
    LeadCostResponse,
    LeadExplanationResponse,
    LeadRecommendationResponse,
    LeadResponse,
    MappedColumnResponse,
    MappingPreviewResponse,
    MemberResponse,
    OnboardingStatusResponse,
    OrganizationBillingResponse,
    OrganizationResponse,
    OutcomeAnalysisResponse,
    OutcomeGroupResponse,
    ProposalResponse,
    ProposedChangeResponse,
    ProviderStatusResponse,
    ReceiptResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewResponse,
    ScoringDimensionSummary,
    SetProviderCredentialRequest,
    SetProviderEnabledRequest,
    StartCheckoutRequest,
    SubmitFeedbackRequest,
    TargetingConfirmRequest,
    TargetingDraftRequest,
    TargetingDraftResponse,
    TargetingVocabulariesResponse,
    UpdateExecutionModeRequest,
    UpdateMemberRoleRequest,
    UpdateOrganizationRequest,
    UsageAgainstLimitsResponse,
    UsageSummaryResponse,
    WorkerHealthResponse,
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
    VerifiedIdentity,
    resolve_api_key_context,
    resolve_auth_context,
    resolve_verified_identity,
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
    parse_csv,
)
from arie.billing.plans import (
    MemberQuotaExceededError,
    resolve_organization_entitlements,
)
from arie.billing.repository import get_billing
from arie.billing.service import (
    NoStripeCustomerError,
    PurchasableUnknownPlanError,
    open_billing_portal,
    process_webhook_event,
    start_checkout,
)
from arie.billing.stripe_gateway import StripeNotConfiguredError, UnknownPlanError
from arie.config import DATABASE, FRONTEND, OBSERVABILITY
from arie.credential_resolver import resolve_provider_credential
from arie.feedback import get_feedback, submit_feedback
from arie.icp_profiles import (
    create_profile as create_icp_profile_row,
)
from arie.icp_profiles import (
    get_active_profile,
    get_profile_by_version,
    list_profiles,
)
from arie.identity.resolver import IdentityResolver
from arie.intelligence.csv_mapping import (
    CANONICAL_FIELDS,
    MappingPreview,
    read_headers_and_samples,
    resolve_mapping,
    validate_confirmed_mapping,
)
from arie.intelligence.explanation import generate_explanation
from arie.intelligence.outcomes import (
    OutcomeAnalysis,
    analyze_outcomes,
    interpret_outcomes,
    parse_outcome_csv,
)
from arie.intelligence.proposals import (
    ProposalRecord,
    StaleProposalError,
    accept_proposal,
    build_revision_proposal,
    create_proposal,
    get_proposal,
    list_proposals,
    reject_proposal,
)
from arie.intelligence.targeting import (
    TargetingGenerationError,
    canonical_vocabularies,
    confirm_targeting_draft,
    generate_targeting_draft,
    stored_draft,
)
from arie.invitations import (
    DuplicateInvitationError,
    GeneratedInvitation,
    InvalidInvitationRoleError,
    InvitationExpiredError,
    InvitationNotFoundError,
    MismatchedInvitationEmailError,
    accept_invitation,
    create_invitation,
    list_invitations,
    revoke_invitation,
    send_invitation_email,
)
from arie.jobs.heartbeat import fleet_status
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger
from arie.limits import (
    LimitExceededError,
    enforce_csv_row_quota,
    enforce_lead_quota,
    get_usage_against_limits,
)
from arie.llm.budget import LLMBudgetReason
from arie.llm.service import LLMService
from arie.members import (
    CannotActOnSelfError,
    InvalidMemberRoleError,
    LastOwnerError,
    list_members,
    remove_member,
    update_member_role,
)
from arie.migrations import MigrationsDirectoryError, pending_migrations
from arie.observability.tracing import configure_tracing, shutdown_tracing
from arie.onboarding import get_onboarding_status
from arie.organizations import (
    InvalidExecutionModeError,
    InvalidOrganizationSettingsError,
    get_organization,
    set_execution_mode,
    update_organization,
)
from arie.provider_configs import (
    InvalidProviderError,
    ProviderStatus,
    delete_provider_config,
    get_provider_status,
    list_provider_statuses,
    record_test_result,
    set_provider_credential,
    set_provider_enabled,
)
from arie.provider_testing import ConnectionTestResult, test_connection
from arie.provisioning import (
    InvalidOrganizationNameError,
    SlugGenerationExhaustedError,
    create_customer_organization,
)
from arie.recommendations import (
    DecisionSignal,
    LeadRecommendation,
    build_recommendation,
    score_snapshot,
)
from arie.security_notifications import (
    notify_member_removed,
    notify_member_role_changed,
    notify_provider_credential_deleted,
    notify_provider_credential_set,
)
from arie.statemachine.apply import OptimisticConcurrencyError
from arie.supabase_admin import get_user_email
from arie.turnstile import verify_turnstile_token
from arie.usage import get_usage_summary
from arie.usage_notifications import check_and_notify_usage

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


def get_llm_service(state: StateDep) -> LLMService:
    """The intelligence layer's model access, as a dependency.

    A dependency rather than a `LLMService(state.pool)` built inline in each
    handler, for the same reason `get_auth_context` is one: it is the seam a
    test overrides. `tests/integration/test_intelligence_targeting_integration
    .py` substitutes a service wired to `FakeLLMProvider`, so the targeting
    endpoints are exercisable end to end on a machine with no model credential
    — which is the whole point of having a fake provider at all.

    Constructed per request. `LLMService` builds its provider lazily and closes
    it again, so this costs nothing on a request that never reaches a model.
    """
    return LLMService(state.pool)


LLMServiceDep = Annotated[LLMService, Depends(get_llm_service)]


def _extract_bearer_token(request: Request) -> str:
    """The one place either authentication path (`get_auth_context`,
    `get_verified_identity`) reads the `Authorization` header — shared so a
    future change to the expected header shape can't drift between them."""
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header — expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization[len("Bearer ") :].strip()


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
    token = _extract_bearer_token(request)

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


def get_verified_identity(request: Request) -> VerifiedIdentity:
    """The auth boundary for `POST /invitations/accept` alone — deliberately
    not `AuthDep`/`get_auth_context`: that path always requires an existing
    organization membership (`resolve_auth_context` raises `NotAMemberError`
    without one), which is exactly what accepting an invitation does not yet
    have. Requires a real Supabase JWT; an ARIE API key is refused outright
    — a machine credential has no email to match an invitation against and
    must never be able to join an organization as a member.
    """
    token = _extract_bearer_token(request)
    if looks_like_api_key(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requires a user session (API keys cannot accept invitations)",
        )
    try:
        return resolve_verified_identity(token)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


IdentityDep = Annotated[VerifiedIdentity, Depends(get_verified_identity)]


def _require_scope(auth: AuthContext, scope: str) -> None:
    """Gate a data-plane action on `scope`. A no-op for a JWT session (see
    `AuthContext.has_scope`); refuses an API key that wasn't granted it."""
    if not auth.has_scope(scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"missing required scope: {scope}"
        )


_TARGETING_FAILURE_STATUS: dict[LLMBudgetReason, int] = {
    LLMBudgetReason.PROVIDER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    LLMBudgetReason.LLM_DISABLED: status.HTTP_409_CONFLICT,
    LLMBudgetReason.BATCH_CALL_LIMIT_REACHED: status.HTTP_429_TOO_MANY_REQUESTS,
    LLMBudgetReason.BATCH_COST_LIMIT_REACHED: status.HTTP_429_TOO_MANY_REQUESTS,
    LLMBudgetReason.MONTHLY_COST_LIMIT_REACHED: status.HTTP_429_TOO_MANY_REQUESTS,
    LLMBudgetReason.ALLOWED: status.HTTP_502_BAD_GATEWAY,
}
"""How an AI-generation failure reaches the client. None of these is a 5xx by
accident: a budget ceiling is a 429 (the same shape `arie.limits`'
`LimitExceededError` gets), an organization that switched AI off is a 409
(nothing is wrong and retrying will not help), an unconfigured or unreachable
provider is a 503, and `ALLOWED` — the budget said yes and the model still
produced nothing usable — is a 502, because the failure really is upstream."""

_TARGETING_FAILURE_DETAIL: dict[LLMBudgetReason, str] = {
    LLMBudgetReason.PROVIDER_UNAVAILABLE: (
        "AI targeting generation is not configured for this deployment. You can "
        "still set targeting up directly through the ICP configuration."
    ),
    LLMBudgetReason.LLM_DISABLED: (
        "AI assistance is switched off for this organization. An owner or admin "
        "can raise the AI limits in organization settings."
    ),
    LLMBudgetReason.ALLOWED: (
        "AI targeting generation is temporarily unavailable. Please try again."
    ),
}
"""Customer-facing replacements for the reasons whose internal detail would
either say too little or too much. The budget-ceiling reasons are deliberately
absent: `arie.llm.budget` already writes those messages with the organization's
own figures in them, which is more useful than anything fixed here, and they
contain nothing a member cannot already see on their settings page. No branch
here can emit a prompt, a provider response, or a credential — the only strings
available are these constants and `arie.llm.budget`'s own."""


def _to_mapping_preview_response(preview: MappingPreview) -> MappingPreviewResponse:
    """Render a mapping preview for the console, labels included.

    The label is resolved here rather than in the console so a canonical field
    has exactly one customer-facing name, and a frontend build cannot drift
    into showing `company_domain` to somebody.
    """
    return MappingPreviewResponse(
        columns=[
            MappedColumnResponse(
                source_column=column.source_column,
                canonical_field=column.canonical_field,
                label=(
                    CANONICAL_FIELDS[column.canonical_field].label
                    if column.canonical_field
                    else None
                ),
                confidence=str(column.confidence),
                reason=column.reason,
                requires_confirmation=column.requires_confirmation,
                candidates=list(column.candidates),
            )
            for column in preview.columns
        ],
        field_map=dict(preview.field_map),
        ignored_columns=list(preview.ignored_columns),
        conflicts=list(preview.conflicts),
        warnings=list(preview.warnings),
        requires_confirmation=preview.requires_confirmation,
        usable=preview.usable,
        mapping_method=str(preview.method),
        available_fields=[
            CanonicalFieldResponse(
                name=field.name,
                label=field.label,
                description=field.description,
                required=field.required,
            )
            for field in CANONICAL_FIELDS.values()
        ],
        llm_provider=preview.llm_provider,
        llm_model=preview.llm_model,
        llm_cost_usd=preview.llm_cost_usd,
        llm_unavailable_reason=preview.llm_unavailable_reason,
    )


def _confirmed_field_map(content: bytes, mapping: str | None) -> dict[str, str] | None:
    """Parse and revalidate a client-supplied column mapping.

    Returns ``None`` when no mapping was sent, which leaves `arie.batches`'
    own alias matching in charge — the pre-M7 behaviour every existing caller
    still gets.

    Raises a 422 rather than silently ignoring a broken mapping: a customer who
    just confirmed which column holds their email addresses must not have that
    answer quietly discarded and the file ingested some other way.
    """
    if mapping is None:
        return None
    try:
        parsed = json.loads(mapping)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the column mapping was not valid JSON",
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the column mapping must be an object of field name to column name",
        )

    try:
        headers, _ = read_headers_and_samples(content)
    except MalformedCsvError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    validated, problems = validate_confirmed_mapping(headers, parsed)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=" ".join(problems)
        )
    return validated


def _to_outcome_analysis_response(
    analysis: OutcomeAnalysis,
    *,
    interpretation: str | None,
    caveats: list[str],
    proposal_id: UUID | None,
) -> OutcomeAnalysisResponse:
    """Render a deterministic analysis for the console.

    Every figure came from `arie.intelligence.outcomes`; `interpretation` is the
    only field a model contributed, and its absence is normal rather than an
    error state a client has to handle specially.
    """
    return OutcomeAnalysisResponse(
        total_rows=analysis.total_rows,
        labelled_rows=analysis.labelled_rows,
        positive_count=analysis.positive_count,
        negative_count=analysis.negative_count,
        baseline_rate=analysis.baseline_rate,
        groups=[
            OutcomeGroupResponse(
                dimension=group.dimension,
                group_key=group.group_key,
                group_label=group.group_label,
                sample_size=group.sample_size,
                positive_count=group.positive_count,
                negative_count=group.negative_count,
                positive_rate=group.positive_rate,
                baseline_rate=group.baseline_rate,
                rate_difference=group.rate_difference,
                signal=str(group.signal),
                sentence=group.sentence(),
            )
            for group in analysis.groups
        ],
        unrecognised_labels=dict(analysis.unrecognised_labels),
        warnings=list(analysis.warnings),
        revenue_total_usd=(
            str(analysis.revenue_total_usd) if analysis.revenue_total_usd is not None else None
        ),
        interpretation=interpretation,
        caveats=caveats,
        proposal_id=proposal_id,
    )


def _to_proposal_response(record: ProposalRecord) -> ProposalResponse:
    payload = record.proposal
    return ProposalResponse(
        proposal_id=record.proposal_id,
        organization_id=record.organization_id,
        profile_id=record.profile_id,
        profile_version=record.profile_version,
        source=record.source,
        status=record.status,
        summary=record.summary,
        changes=[ProposedChangeResponse(**change) for change in payload.get("changes", [])],
        observations=list(payload.get("observations", [])),
        caveats=list(payload.get("caveats", [])),
        supporting_statistics=record.supporting_statistics,
        evidence_strength=record.evidence_strength,
        sample_size=record.sample_size,
        created_at=record.created_at,
        resolved_at=record.resolved_at,
        resulting_profile_id=record.resulting_profile_id,
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


def _dispatch_invitation_email(
    state: AppState, auth: AuthContext, generated: GeneratedInvitation
) -> None:
    """Best-effort side effect after `create_invitation`/`resend_invitation
    _endpoint`'s own transaction has already committed the invitation row
    itself (Productization M6 Part 14 — the invitation must exist regardless
    of whether the email attempt succeeds). `send_invitation_email` never
    raises for a delivery failure; this wrapper exists only to resolve the
    organization name and inviter email the route handlers don't otherwise
    need."""
    assert auth.user_id is not None  # every caller is _require_org_admin-gated
    with state.pool.connection() as conn:
        organization = get_organization(conn, organization_id=auth.organization_id)
        assert organization is not None
        inviter_email = get_user_email(auth.user_id) or "An ARIE administrator"
        accept_url = f"{FRONTEND.base_url}/invite/accept?token={generated.raw_token}"
        send_invitation_email(
            conn,
            invitation=generated.record,
            organization_name=organization.name,
            inviter_email=inviter_email,
            accept_url=accept_url,
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

    @app.get("/healthz/worker", response_model=WorkerHealthResponse)
    def worker_healthz(state: StateDep) -> WorkerHealthResponse:
        """Productization M6 Part 28/34. Deliberately unauthenticated (same
        as `/healthz` — an infra liveness probe has no caller identity) and
        deliberately never folded into `/healthz`'s own status code: a dead
        worker fleet is a real operational problem, but it must not flap the
        API's own liveness monitor, which can do nothing to fix it. Reads
        `worker_heartbeats`, written by every `arie.jobs.worker.main` process
        roughly every `WORKER_HEARTBEAT_INTERVAL_SECONDS`.
        """
        with state.pool.connection() as conn:
            result = fleet_status(conn)
        return WorkerHealthResponse.model_validate(result)

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
            try:
                enforce_lead_quota(
                    conn, organization_id=auth.organization_id, now=datetime.now(UTC)
                )
            except LimitExceededError as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
                ) from exc
            result = ingest_lead(
                conn,
                resolver=state.resolver,
                queue=state.queue,
                command=payload.to_command(organization_id=auth.organization_id),
            )
            conn.commit()

        if result.created:
            # Best-effort, on a fresh connection after this request's own
            # transaction has committed — see arie.usage_notifications' own
            # docstring for why it must never fail the ingestion response.
            with state.pool.connection() as usage_conn:
                check_and_notify_usage(
                    usage_conn, organization_id=auth.organization_id, now=datetime.now(UTC)
                )

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

    # --------------------------------------------------- lead recommendation --
    #
    # M7 Slice 4. `GET /recommendation` is the customer-facing payoff of every
    # prior slice — deterministic, no LLM call, no cost — built from the exact
    # same `DecisionReceipt` the (still-available, now "Advanced Details")
    # receipt endpoint above already reads. `POST /explanation` is the one
    # surface in this section allowed to spend an AI budget, and only because
    # a caller explicitly asked for it (Part F's "never one call per row" rule).

    def _load_recommendation(
        conn: psycopg.Connection,
        ledger: PostgresCostLedger,
        *,
        lead_id: UUID,
        organization_id: UUID,
    ) -> tuple[LeadRecommendation, DecisionReceipt] | None:
        receipt = build_receipt(conn, ledger, lead_id, organization_id=organization_id)
        if receipt is None:
            return None
        recommendation = build_recommendation(lead_id, DecisionSignal.from_receipt(receipt))
        return recommendation, receipt

    @app.get("/leads/{lead_id}/recommendation", response_model=LeadRecommendationResponse)
    def get_lead_recommendation(
        lead_id: UUID, state: StateDep, auth: AuthDep
    ) -> LeadRecommendationResponse:
        """What ARIE tells a customer to do about this lead — priority, next
        action, a deterministic reason, and no AI cost. See
        `arie.recommendations` for why every field is derived rather than
        stored, and `POST /leads/{lead_id}/explanation` for the richer,
        evidence-cited prose version this endpoint never generates on its own.
        """
        _require_scope(auth, "leads:read")
        with state.pool.connection() as conn:
            loaded = _load_recommendation(
                conn, state.ledger, lead_id=lead_id, organization_id=auth.organization_id
            )
        if loaded is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}")
        recommendation, _ = loaded
        return LeadRecommendationResponse.from_recommendation(recommendation)

    @app.post("/leads/{lead_id}/explanation", response_model=LeadExplanationResponse)
    def post_lead_explanation(
        lead_id: UUID, state: StateDep, auth: AuthDep, llm: LLMServiceDep
    ) -> LeadExplanationResponse:
        """One on-demand, evidence-grounded explanation of the recommendation
        above. Always returns 200 — an unavailable or misbehaving model
        degrades to `arie.intelligence.explanation.deterministic_explanation`
        rather than failing the request, per M7's standing rule that a
        model's absence never breaks the product. `source` in the response
        tells the caller which one it got.
        """
        _require_scope(auth, "leads:read")
        with state.pool.connection() as conn:
            record = fetch_lead(conn, lead_id, organization_id=auth.organization_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}"
                )
            loaded = _load_recommendation(
                conn, state.ledger, lead_id=lead_id, organization_id=auth.organization_id
            )
            assert loaded is not None  # `record` above already proved the lead exists
            recommendation, receipt = loaded
            profile_name = "your targeting profile"
            if receipt.versions is not None and receipt.versions.icp_profile_version is not None:
                profile = get_profile_by_version(
                    conn,
                    organization_id=auth.organization_id,
                    version=receipt.versions.icp_profile_version,
                )
                if profile is not None:
                    profile_name = profile.name
            outcome = generate_explanation(
                llm,
                conn,
                organization_id=auth.organization_id,
                lead_id=lead_id,
                company_id=record.company_id,
                person_id=record.person_id,
                recommendation=recommendation,
                profile_name=profile_name,
                now=datetime.now(UTC),
            )
        return LeadExplanationResponse.from_outcome(outcome)

    # ------------------------------------------------------------- feedback --
    #
    # M7 Slice 4, Part I. An observation on a recommendation, never a
    # mutation — see `migrations/0036_lead_recommendation_feedback.sql`.
    # Human-only (`_require_jwt_session`): a machine API key has no identity
    # to attribute an opinion to.

    @app.post("/leads/{lead_id}/feedback", response_model=FeedbackResponse)
    def post_lead_feedback(
        lead_id: UUID, payload: SubmitFeedbackRequest, state: StateDep, auth: AuthDep
    ) -> FeedbackResponse:
        _require_jwt_session(auth)
        assert auth.user_id is not None  # guaranteed by the JWT check above
        with _transaction(state.pool) as conn:
            loaded = _load_recommendation(
                conn, state.ledger, lead_id=lead_id, organization_id=auth.organization_id
            )
            if loaded is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}"
                )
            recommendation, _ = loaded
            record = submit_feedback(
                conn,
                organization_id=auth.organization_id,
                lead_id=lead_id,
                user_id=auth.user_id,
                sentiment=payload.sentiment,
                reason=payload.reason,
                note=payload.note,
                priority=recommendation.priority,
                next_action=recommendation.next_action,
                profile_version=recommendation.profile_version,
                score_snapshot=score_snapshot(recommendation),
            )
        return FeedbackResponse.from_record(record)

    @app.get("/leads/{lead_id}/feedback", response_model=FeedbackResponse | None)
    def get_lead_feedback(lead_id: UUID, state: StateDep, auth: AuthDep) -> FeedbackResponse | None:
        _require_jwt_session(auth)
        assert auth.user_id is not None
        with state.pool.connection() as conn:
            record = fetch_lead(conn, lead_id, organization_id=auth.organization_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"no lead {lead_id}"
                )
            feedback = get_feedback(
                conn, organization_id=auth.organization_id, lead_id=lead_id, user_id=auth.user_id
            )
        return FeedbackResponse.from_record(feedback) if feedback is not None else None

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

    # ------------------------------------------------------- provisioning --
    #
    # Productization M6 Parts 10/11/12. Self-service organization creation
    # for an already-authenticated, already email-verified Supabase user —
    # `IdentityDep`, not `AuthDep`, the same "no organization membership
    # required yet" auth boundary `POST /invitations/accept` uses, and for
    # the identical reason.

    @app.post(
        "/organizations",
        response_model=CreateOrganizationResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_organization_endpoint(
        payload: CreateOrganizationRequest, request: Request, state: StateDep, identity: IdentityDep
    ) -> CreateOrganizationResponse:
        client_ip = request.client.host if request.client else None
        if not verify_turnstile_token(payload.turnstile_token, remote_ip=client_ip):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="CAPTCHA verification failed"
            )
        try:
            with _transaction(state.pool) as conn:
                result = create_customer_organization(
                    conn, owner_user_id=identity.user_id, organization_name=payload.name
                )
        except InvalidOrganizationNameError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except SlugGenerationExhaustedError as exc:  # pragma: no cover - vanishingly unlikely
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return CreateOrganizationResponse.model_validate(result)

    # ------------------------------------------------------------- billing --
    #
    # Productization M6 Parts 2-9/17-19/25-26. `GET /billing` and the
    # Checkout/Portal routes require an owner/admin JWT session — billing
    # management is never available to an analyst_reviewer or any API key,
    # matching Part 37 exactly. `POST /billing/webhook` is the one
    # unauthenticated route in this whole API besides `/healthz*`: Stripe
    # itself is the caller, and `arie.billing.service.process_webhook_event`
    # is what actually authenticates the request, via signature
    # verification — see that function's own docstring.

    @app.get("/billing", response_model=BillingResponse)
    def get_billing_endpoint(state: StateDep, auth: AuthDep) -> BillingResponse:
        _require_org_admin(auth)
        with state.pool.connection() as conn:
            billing = get_billing(conn, organization_id=auth.organization_id)
            entitlements = resolve_organization_entitlements(
                conn, organization_id=auth.organization_id
            )
        return BillingResponse(
            billing=OrganizationBillingResponse.model_validate(billing),
            entitlements=EffectiveEntitlementsResponse.model_validate(entitlements),
        )

    @app.post("/billing/checkout", response_model=CheckoutSessionResponse)
    def start_checkout_endpoint(
        payload: StartCheckoutRequest, state: StateDep, auth: AuthDep
    ) -> CheckoutSessionResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        actor_email = get_user_email(auth.user_id)
        if actor_email is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="could not resolve the caller's account email",
            )
        try:
            with _transaction(state.pool) as conn:
                checkout_url = start_checkout(
                    conn,
                    organization_id=auth.organization_id,
                    actor_user_id=auth.user_id,
                    actor_email=actor_email,
                    plan=payload.plan,
                    success_url=payload.success_url,
                    cancel_url=payload.cancel_url,
                )
        except PurchasableUnknownPlanError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except (StripeNotConfiguredError, UnknownPlanError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return CheckoutSessionResponse(checkout_url=checkout_url)

    @app.post("/billing/portal", response_model=BillingPortalResponse)
    def open_billing_portal_endpoint(
        payload: BillingPortalRequest, state: StateDep, auth: AuthDep
    ) -> BillingPortalResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                portal_url = open_billing_portal(
                    conn,
                    organization_id=auth.organization_id,
                    actor_user_id=auth.user_id,
                    return_url=payload.return_url,
                )
        except NoStripeCustomerError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except StripeNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        return BillingPortalResponse(portal_url=portal_url)

    @app.post("/billing/webhook", include_in_schema=False)
    async def stripe_webhook_endpoint(request: Request, state: StateDep) -> Response:
        """`async def`, the one deliberate exception to this module's
        sync-handler convention (see the module docstring). Webhook traffic
        is low-volume and Stripe's own signature verification needs the
        exact raw bytes Starlette received — `await request.body()` is only
        reachable from an async route — so the small amount of blocking DB
        work `process_webhook_event` does runs directly on the event loop
        here rather than being threaded through `run_in_threadpool` for one
        endpoint. Status codes are Stripe's own retry contract: 400 for a
        signature that doesn't verify, 503 when this deployment has no
        signing secret configured to verify it *with*, 200 for anything
        processed (including a harmlessly-ignored or already-seen event),
        500 for a transient failure worth Stripe retrying.
        """
        raw_body = await request.body()
        signature_header = request.headers.get("stripe-signature", "")
        result = process_webhook_event(
            state.pool, raw_body=raw_body, signature_header=signature_header
        )
        return (
            _json_error(result.status_code, result.detail)
            if result.status_code != 200
            else Response(
                content='{"received": true}', media_type="application/json", status_code=200
            )
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

    @app.patch("/organization/execution-mode", response_model=OrganizationResponse)
    def update_execution_mode_endpoint(
        payload: UpdateExecutionModeRequest, state: StateDep, auth: AuthDep
    ) -> OrganizationResponse:
        """Productization M5 Part 14. Deliberately a separate route from
        `PATCH /organization` — see `UpdateExecutionModeRequest`'s own
        docstring. Owner/admin-only, same as every other organization-
        settings write; the request body's own validator already rejects a
        value outside `arie.organizations.EXECUTION_MODES` as a 422 before
        this ever reaches `set_execution_mode`, so the `InvalidExecutionMode
        Error` branch below only guards a race no request-time validation
        can catch on its own.

        Productization M6 Part 20: moving away from `simulated` additionally
        requires this organization's plan to entitle it to live provider
        features (`arie.billing.plans.is_live_provider_feature_allowed`) —
        checked *alongside*, never instead of, every M5 execution-safety
        guard. A billing entitlement can make this route reachable; it can
        never itself place a real provider call.
        """
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        if payload.execution_mode != "simulated":
            with state.pool.connection() as entitlement_conn:
                entitlements = resolve_organization_entitlements(
                    entitlement_conn, organization_id=auth.organization_id
                )
            if not entitlements.live_provider_feature_allowed:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"the {entitlements.plan} plan does not include live provider execution",
                )
        try:
            with _transaction(state.pool) as conn:
                record = set_execution_mode(
                    conn,
                    organization_id=auth.organization_id,
                    execution_mode=payload.execution_mode,
                    actor_user_id=auth.user_id,
                )
        except InvalidExecutionModeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        if record is None:  # pragma: no cover - an authenticated org always exists
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
            )
        return OrganizationResponse.model_validate(record)

    # ---------------------------------------------------------- membership --
    #
    # Productization M4 Part 2. Reads and writes both owner/admin-only
    # (`_require_org_admin`) — see the schemas module's own banner comment
    # for why this differs from ICP configuration's read-open split. No
    # API-key scope for any of it: a machine credential must never manage
    # who belongs to an organization, the same rule API keys already follow
    # for managing API keys themselves.

    @app.get("/organization/members", response_model=list[MemberResponse])
    def list_members_endpoint(state: StateDep, auth: AuthDep) -> list[MemberResponse]:
        _require_org_admin(auth)
        with state.pool.connection() as conn:
            records = list_members(conn, organization_id=auth.organization_id)
        return [MemberResponse.model_validate(record) for record in records]

    @app.patch("/organization/members/{user_id}", response_model=MemberResponse)
    def update_member_role_endpoint(
        user_id: UUID, payload: UpdateMemberRoleRequest, state: StateDep, auth: AuthDep
    ) -> MemberResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                record = update_member_role(
                    conn,
                    organization_id=auth.organization_id,
                    target_user_id=user_id,
                    new_role=payload.role,
                    actor_user_id=auth.user_id,
                )
        except InvalidMemberRoleError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except CannotActOnSelfError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot change your own role",
            ) from exc
        except LastOwnerError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot demote the organization's only remaining owner",
            ) from exc
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
        # Best-effort, on a fresh connection after the transaction above has
        # committed — see arie.security_notifications' own docstring. A role
        # change is one of the four actions that can silently hand someone
        # control of an organization, so every owner/admin hears about it.
        with state.pool.connection() as notify_conn:
            notify_member_role_changed(
                notify_conn,
                organization_id=auth.organization_id,
                target_user_id=user_id,
                new_role=payload.role,
                actor_user_id=auth.user_id,
            )
        return MemberResponse.model_validate(record)

    @app.delete("/organization/members/{user_id}", response_model=MemberResponse)
    def remove_member_endpoint(user_id: UUID, state: StateDep, auth: AuthDep) -> MemberResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                record = remove_member(
                    conn,
                    organization_id=auth.organization_id,
                    target_user_id=user_id,
                    actor_user_id=auth.user_id,
                )
        except CannotActOnSelfError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cannot remove yourself",
            ) from exc
        except LastOwnerError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot remove the organization's only remaining owner",
            ) from exc
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
        with state.pool.connection() as notify_conn:
            notify_member_removed(
                notify_conn,
                organization_id=auth.organization_id,
                target_user_id=user_id,
                actor_user_id=auth.user_id,
            )
        return MemberResponse.model_validate(record)

    # -------------------------------------------------------- invitations --
    #
    # `/organization/invitations/*` (list/create/revoke) are owner/admin-only
    # JWT-session actions, same tier as membership above. `POST /invitations
    # /accept` is deliberately NOT under `/organization/` and does not take
    # `AuthDep` at all — the accepting user has no organization membership
    # yet, which `AuthDep`'s `X-Organization-Id` + membership check always
    # requires; see `get_verified_identity`'s own docstring.

    @app.get("/organization/invitations", response_model=list[InvitationResponse])
    def list_invitations_endpoint(state: StateDep, auth: AuthDep) -> list[InvitationResponse]:
        _require_org_admin(auth)
        with state.pool.connection() as conn:
            records = list_invitations(conn, organization_id=auth.organization_id)
        return [InvitationResponse.model_validate(record) for record in records]

    @app.post(
        "/organization/invitations",
        response_model=InvitationCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_invitation_endpoint(
        payload: CreateInvitationRequest, state: StateDep, auth: AuthDep
    ) -> InvitationCreatedResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                generated = create_invitation(
                    conn,
                    organization_id=auth.organization_id,
                    invited_by_user_id=auth.user_id,
                    email=payload.email,
                    role=payload.role,
                )
        except DuplicateInvitationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except InvalidInvitationRoleError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except MemberQuotaExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
            ) from exc
        _dispatch_invitation_email(state, auth, generated)
        base = InvitationResponse.model_validate(generated.record)
        return InvitationCreatedResponse(**base.model_dump(), raw_token=generated.raw_token)

    @app.delete("/organization/invitations/{invitation_id}", response_model=InvitationResponse)
    def revoke_invitation_endpoint(
        invitation_id: UUID, state: StateDep, auth: AuthDep
    ) -> InvitationResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        with _transaction(state.pool) as conn:
            record = revoke_invitation(
                conn,
                organization_id=auth.organization_id,
                invitation_id=invitation_id,
                actor_user_id=auth.user_id,
            )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found"
            )
        return InvitationResponse.model_validate(record)

    @app.post(
        "/organization/invitations/{invitation_id}/resend",
        response_model=InvitationCreatedResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def resend_invitation_endpoint(
        invitation_id: UUID, state: StateDep, auth: AuthDep
    ) -> InvitationCreatedResponse:
        """Productization M6 Part 14 ("allow resend by creating/reissuing
        safely"). A raw invitation token is never persisted (see
        `arie.invitations`' own docstring), so a literal resend of the same
        link is impossible by design — this revokes the existing pending
        invitation and issues a fresh one for the same email/role, which is
        the safe reissue the brief asks for. 404s for anything not currently
        `pending`, the same as `DELETE .../invitations/{id}` does.
        """
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        with _transaction(state.pool) as conn:
            existing = revoke_invitation(
                conn,
                organization_id=auth.organization_id,
                invitation_id=invitation_id,
                actor_user_id=auth.user_id,
            )
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="no pending invitation with that id",
                )
            generated = create_invitation(
                conn,
                organization_id=auth.organization_id,
                invited_by_user_id=auth.user_id,
                email=existing.email_normalized,
                role=existing.role,
            )
        _dispatch_invitation_email(state, auth, generated)
        base = InvitationResponse.model_validate(generated.record)
        return InvitationCreatedResponse(**base.model_dump(), raw_token=generated.raw_token)

    @app.post("/invitations/accept", response_model=InvitationResponse)
    def accept_invitation_endpoint(
        payload: AcceptInvitationRequest, state: StateDep, identity: IdentityDep
    ) -> InvitationResponse:
        try:
            with _transaction(state.pool) as conn:
                record = accept_invitation(
                    conn,
                    raw_token=payload.token,
                    verified_email=identity.email,
                    user_id=identity.user_id,
                )
        except InvitationNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found"
            ) from exc
        except InvitationExpiredError as exc:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
        except MemberQuotaExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
            ) from exc
        except MismatchedInvitationEmailError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="this invitation was sent to a different email address",
            ) from exc
        return InvitationResponse.model_validate(record)

    # ------------------------------------------------------ provider configs --
    #
    # Productization M4 Parts 3-5. Reads open to any active member
    # (`_require_jwt_session`) — see the schemas module's own banner
    # comment for why (never a secret in the response shape). Writes
    # (save/replace credential, enable/disable, remove, test) are
    # owner/admin-only. No API-key scope for any of it — a machine
    # credential must never manage another credential.

    @app.get("/organization/providers", response_model=list[ProviderStatusResponse])
    def list_provider_statuses_endpoint(
        state: StateDep, auth: AuthDep
    ) -> list[ProviderStatusResponse]:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            statuses = list_provider_statuses(conn, organization_id=auth.organization_id)
        return [ProviderStatusResponse.model_validate(s) for s in statuses]

    @app.get("/organization/providers/{provider}", response_model=ProviderStatusResponse)
    def get_provider_status_endpoint(
        provider: str, state: StateDep, auth: AuthDep
    ) -> ProviderStatusResponse:
        _require_jwt_session(auth)
        try:
            with state.pool.connection() as conn:
                result = get_provider_status(
                    conn, organization_id=auth.organization_id, provider=provider
                )
        except InvalidProviderError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return ProviderStatusResponse.model_validate(result)

    @app.put("/organization/providers/{provider}", response_model=ProviderStatusResponse)
    def set_provider_credential_endpoint(
        provider: str,
        payload: SetProviderCredentialRequest,
        state: StateDep,
        auth: AuthDep,
    ) -> ProviderStatusResponse:
        """Productization M6 Part 20: configuring a BYOK credential requires
        the same plan entitlement `PATCH /organization/execution-mode`
        requires for going live — see that route's own docstring for why
        this is additive to, never a substitute for, M5's execution-safety
        guards.
        """
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        with state.pool.connection() as entitlement_conn:
            entitlements = resolve_organization_entitlements(
                entitlement_conn, organization_id=auth.organization_id
            )
        if not entitlements.live_provider_feature_allowed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"the {entitlements.plan} plan does not include live provider configuration",
            )
        try:
            with _transaction(state.pool) as conn:
                record = set_provider_credential(
                    conn,
                    organization_id=auth.organization_id,
                    provider=provider,
                    raw_credential=payload.credential,
                    actor_user_id=auth.user_id,
                )
        except InvalidProviderError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        # The notice names the provider and never the credential — not a
        # prefix, not a length. See arie.security_notifications' docstring.
        with state.pool.connection() as notify_conn:
            notify_provider_credential_set(
                notify_conn,
                organization_id=auth.organization_id,
                provider=provider,
                actor_user_id=auth.user_id,
            )
        return ProviderStatusResponse.model_validate(ProviderStatus.from_record(record))

    @app.patch("/organization/providers/{provider}", response_model=ProviderStatusResponse)
    def set_provider_enabled_endpoint(
        provider: str,
        payload: SetProviderEnabledRequest,
        state: StateDep,
        auth: AuthDep,
    ) -> ProviderStatusResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                record = set_provider_enabled(
                    conn,
                    organization_id=auth.organization_id,
                    provider=provider,
                    enabled=payload.enabled,
                    actor_user_id=auth.user_id,
                )
        except InvalidProviderError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{provider} has not been configured for this organization",
            )
        return ProviderStatusResponse.model_validate(ProviderStatus.from_record(record))

    @app.delete("/organization/providers/{provider}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_provider_config_endpoint(provider: str, state: StateDep, auth: AuthDep) -> None:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check
        try:
            with _transaction(state.pool) as conn:
                deleted = delete_provider_config(
                    conn,
                    organization_id=auth.organization_id,
                    provider=provider,
                    actor_user_id=auth.user_id,
                )
        except InvalidProviderError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{provider} has not been configured for this organization",
            )
        with state.pool.connection() as notify_conn:
            notify_provider_credential_deleted(
                notify_conn,
                organization_id=auth.organization_id,
                provider=provider,
                actor_user_id=auth.user_id,
            )

    @app.post("/organization/providers/{provider}/test", response_model=ProviderStatusResponse)
    def test_provider_connection_endpoint(
        provider: str, state: StateDep, auth: AuthDep
    ) -> ProviderStatusResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s auth_method == "jwt" check

        try:
            with state.pool.connection() as conn:
                current = get_provider_status(
                    conn, organization_id=auth.organization_id, provider=provider
                )
        except InvalidProviderError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if not current.configured:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{provider} has not been configured for this organization",
            )

        with state.pool.connection() as conn:
            raw_credential = resolve_provider_credential(
                conn, organization_id=auth.organization_id, provider=provider
            )
        # `current.configured` already confirmed a row exists; the only way
        # this is still None is a genuinely orphaned Vault secret, which
        # `arie.provider_configs`'s single-transaction writes are designed
        # to make impossible — treated as a transport-shaped test failure
        # rather than a 5xx, so the admin sees "test failed," not a crash.
        if raw_credential is None:
            result = ConnectionTestResult(success=False, sanitized_error="credential_unavailable")
        else:
            result = test_connection(provider, raw_credential)

        with _transaction(state.pool) as conn:
            record = record_test_result(
                conn,
                organization_id=auth.organization_id,
                provider=provider,
                success=result.success,
                sanitized_error=result.sanitized_error,
                actor_user_id=auth.user_id,
            )
        if record is None:  # a concurrent removal raced this test to completion
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{provider} has not been configured for this organization",
            )
        return ProviderStatusResponse.model_validate(ProviderStatus.from_record(record))

    # ----------------------------------------------------------- onboarding --
    #
    # Productization M4 Part 8. Read-only, any active member.

    @app.get("/organization/onboarding", response_model=OnboardingStatusResponse)
    def get_onboarding_status_endpoint(state: StateDep, auth: AuthDep) -> OnboardingStatusResponse:
        _require_jwt_session(auth)
        with _transaction(state.pool) as conn:
            result = get_onboarding_status(conn, organization_id=auth.organization_id)
        return OnboardingStatusResponse.model_validate(result)

    # --------------------------------------------------------------- limits --
    #
    # Productization M4 Part 9. Read-only, any active member — see the
    # schemas module's own banner comment for why there is no write
    # endpoint yet.

    @app.get("/organization/limits", response_model=UsageAgainstLimitsResponse)
    def get_limits_endpoint(state: StateDep, auth: AuthDep) -> UsageAgainstLimitsResponse:
        """Productization M6 Part 23 added `plan`/`members_used`/
        `members_limit` alongside the M4 usage figures — built explicitly
        from two separately computed objects (`arie.limits
        .get_usage_against_limits`, `arie.billing.plans
        .resolve_organization_entitlements`) plus an active-member count,
        rather than a single `model_validate`."""
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            usage = get_usage_against_limits(
                conn, organization_id=auth.organization_id, now=datetime.now(UTC)
            )
            entitlements = resolve_organization_entitlements(
                conn, organization_id=auth.organization_id
            )
            members_used = len(list_members(conn, organization_id=auth.organization_id))
        return UsageAgainstLimitsResponse(
            leads_used=usage.leads_used,
            leads_limit=usage.leads_limit,
            leads_remaining=usage.leads_remaining,
            modeled_spend_used_usd=usage.modeled_spend_used_usd,
            modeled_spend_limit_usd=usage.modeled_spend_limit_usd,
            modeled_spend_remaining_usd=usage.modeled_spend_remaining_usd,
            max_csv_rows_per_upload=usage.max_csv_rows_per_upload,
            period_start=usage.period_start,
            period_end=usage.period_end,
            plan=entitlements.plan,
            members_used=members_used,
            members_limit=entitlements.max_members,
        )

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

    # --------------------------------------------- intelligence: targeting --
    #
    # M7 Slice 2. The customer-facing way to configure targeting: describe the
    # business in normal English instead of assigning six point weights by
    # hand. `POST /organization/icp` is untouched and remains the way to submit
    # a configuration directly.
    #
    # Both write-shaped routes require an owner/admin JWT session
    # (`_require_org_admin`), matching `POST /organization/icp` — confirming
    # *is* that operation, and drafting spends the organization's AI budget, so
    # neither is something a read-only member should be able to do. No API-key
    # scope exists for either: a machine caller has no business describing a
    # company's ideal customer in prose, and the M3 comment above says the same
    # thing about ICP configuration generally.
    #
    # Drafting is a POST despite not writing anything. It is not idempotent in
    # the way a GET promises — it spends money and returns a different
    # interpretation each time — and its input is two paragraphs of free text
    # that have no business in a query string.

    @app.get("/intelligence/targeting/vocabularies", response_model=TargetingVocabulariesResponse)
    def get_targeting_vocabularies(auth: AuthDep) -> TargetingVocabulariesResponse:
        _require_jwt_session(auth)
        vocabularies = canonical_vocabularies()
        return TargetingVocabulariesResponse(
            industries=list(vocabularies["industries"]),
            seniorities=list(vocabularies["seniorities"]),
            functions=list(vocabularies["functions"]),
            objectives=list(vocabularies["objectives"]),
            preference_levels=list(vocabularies["preference_levels"]),
            scoring_dimensions=list(vocabularies["scoring_dimensions"]),
        )

    @app.post("/intelligence/targeting/draft", response_model=TargetingDraftResponse)
    def draft_targeting_profile(
        payload: TargetingDraftRequest, auth: AuthDep, llm: LLMServiceDep
    ) -> TargetingDraftResponse:
        _require_org_admin(auth)
        try:
            draft = generate_targeting_draft(
                llm,
                organization_id=auth.organization_id,
                what_you_sell=payload.what_you_sell,
                who_you_want=payload.who_you_want,
                objective=payload.objective,
                now=datetime.now(UTC),
            )
        except TargetingGenerationError as exc:
            raise HTTPException(
                status_code=_TARGETING_FAILURE_STATUS.get(
                    exc.reason, status.HTTP_503_SERVICE_UNAVAILABLE
                ),
                detail=_TARGETING_FAILURE_DETAIL.get(exc.reason, exc.detail),
            ) from exc
        return TargetingDraftResponse(
            objective=draft.objective,
            profile=draft.profile,
            scoring_config=draft.scoring_config,
            allocation=[ScoringDimensionSummary.model_validate(row) for row in draft.allocation],
            llm_provider=draft.provider,
            llm_model=draft.model,
            llm_cost_usd=draft.cost_usd,
        )

    @app.post(
        "/intelligence/targeting/confirm",
        response_model=ICPProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def confirm_targeting_profile(
        payload: TargetingConfirmRequest, state: StateDep, auth: AuthDep
    ) -> ICPProfileResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by is_org_admin()'s JWT check
        with _transaction(state.pool) as conn:
            record = confirm_targeting_draft(
                conn,
                organization_id=auth.organization_id,
                created_by_user_id=auth.user_id,
                name=payload.name,
                profile=payload.profile,
                objective=payload.objective,
                now=datetime.now(UTC),
                provider=payload.llm_provider,
                model=payload.llm_model,
            )
        return ICPProfileResponse.model_validate(record)

    # ---------------------------------------------- intelligence: outcomes --
    #
    # M7 Slice 3. Optional throughout: ARIE is useful with no historical data,
    # and every deterministic statistic below is computed with no model and no
    # cost. A model is reached at most once, after the arithmetic, to write
    # prose about aggregates it did not produce — and its absence changes
    # nothing but the prose.
    #
    # Analysing writes at most one `profile_revision_proposals` row, which
    # changes no scoring at all. Accepting one does, which is why accept and
    # reject are owner/admin like every other targeting write.

    @app.post("/intelligence/outcomes/analyze", response_model=OutcomeAnalysisResponse)
    def analyze_historical_outcomes(
        request: Request, state: StateDep, auth: AuthDep, llm: LLMServiceDep, file: UploadFile
    ) -> OutcomeAnalysisResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None  # guaranteed by the JWT check above
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_UPLOAD_CONTENT_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds the {MAX_UPLOAD_CONTENT_LENGTH}-byte limit",
            )
        try:
            dataset = parse_outcome_csv(file.file.read())
        except MalformedCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        analysis = analyze_outcomes(dataset)
        now = datetime.now(UTC)

        with state.pool.connection() as conn:
            profile = get_active_profile(conn, organization_id=auth.organization_id)
        draft = stored_draft(profile.config) if profile is not None else None

        interpretation = None
        if analysis.usable and profile is not None:
            interpretation = interpret_outcomes(
                llm,
                organization_id=auth.organization_id,
                analysis=analysis,
                profile_summary=profile.name,
                now=now,
            )

        proposal_id: UUID | None = None
        caveats: list[str] = list(interpretation.caveats) if interpretation else []

        if analysis.usable and profile is not None and draft is not None:
            proposal = build_revision_proposal(analysis, draft, interpretation=interpretation)
            if proposal is not None:
                caveats = proposal.caveats
                with _transaction(state.pool) as conn:
                    record = create_proposal(
                        conn,
                        organization_id=auth.organization_id,
                        created_by_user_id=auth.user_id,
                        profile=profile,
                        proposal=proposal,
                    )
                proposal_id = record.proposal_id
        elif analysis.usable and draft is None:
            # Nothing to apply changes *to*: this organization's targeting was
            # written directly rather than described in words, so there is no
            # draft to adjust. The statistics are still worth showing.
            caveats = [
                "ARIE can show you these patterns but cannot suggest a targeting "
                "change, because this organization's targeting was set up directly "
                "rather than described in words."
            ]

        return _to_outcome_analysis_response(
            analysis,
            interpretation=interpretation.summary if interpretation else None,
            caveats=caveats,
            proposal_id=proposal_id,
        )

    @app.get("/intelligence/proposals", response_model=list[ProposalResponse])
    def list_revision_proposals(
        state: StateDep,
        auth: AuthDep,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[ProposalResponse]:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            records = list_proposals(conn, organization_id=auth.organization_id, limit=limit)
        return [_to_proposal_response(record) for record in records]

    @app.get("/intelligence/proposals/{proposal_id}", response_model=ProposalResponse)
    def get_revision_proposal(
        proposal_id: UUID, state: StateDep, auth: AuthDep
    ) -> ProposalResponse:
        _require_jwt_session(auth)
        with state.pool.connection() as conn:
            record = get_proposal(
                conn, organization_id=auth.organization_id, proposal_id=proposal_id
            )
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such suggestion")
        return _to_proposal_response(record)

    @app.post("/intelligence/proposals/{proposal_id}/reject", response_model=ProposalResponse)
    def reject_revision_proposal(
        proposal_id: UUID, state: StateDep, auth: AuthDep
    ) -> ProposalResponse:
        _require_org_admin(auth)
        assert auth.user_id is not None
        with _transaction(state.pool) as conn:
            record = reject_proposal(
                conn,
                organization_id=auth.organization_id,
                proposal_id=proposal_id,
                user_id=auth.user_id,
            )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such open suggestion"
            )
        return _to_proposal_response(record)

    @app.post(
        "/intelligence/proposals/{proposal_id}/accept",
        response_model=ICPProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def accept_revision_proposal(
        proposal_id: UUID, payload: AcceptProposalRequest, state: StateDep, auth: AuthDep
    ) -> ICPProfileResponse:
        """Apply a suggestion as a new immutable targeting version.

        Returns the created profile rather than the proposal, because the
        profile is what changed; a client wanting the resolved proposal reads it
        back by id.
        """
        _require_org_admin(auth)
        assert auth.user_id is not None
        try:
            with _transaction(state.pool) as conn:
                _, created = accept_proposal(
                    conn,
                    organization_id=auth.organization_id,
                    proposal_id=proposal_id,
                    user_id=auth.user_id,
                    name=payload.name,
                    now=datetime.now(UTC),
                )
        except StaleProposalError as exc:
            # 409, not 404 and not 500: the suggestion exists, nothing is
            # broken, and the request cannot be satisfied because the world
            # moved on underneath it.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return ICPProfileResponse.model_validate(created)

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

    @app.post("/batches/mapping-preview", response_model=MappingPreviewResponse)
    def preview_batch_mapping(
        request: Request, state: StateDep, auth: AuthDep, llm: LLMServiceDep, file: UploadFile
    ) -> MappingPreviewResponse:
        """What ARIE thinks this file's columns are, without ingesting anything.

        Registered before `POST /batches` only for readability — the paths do
        not collide. Costs nothing for a file whose headers are all recognised;
        an ambiguous one spends at most one AI call, and a customer whose
        budget is exhausted still gets the deterministic answer plus a note.
        """
        _require_jwt_session(auth)
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_UPLOAD_CONTENT_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds the {MAX_UPLOAD_CONTENT_LENGTH}-byte limit",
            )
        content = file.file.read()
        try:
            preview = resolve_mapping(
                content,
                service=llm,
                organization_id=auth.organization_id,
                now=datetime.now(UTC),
            )
        except MalformedCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return _to_mapping_preview_response(preview)

    @app.post("/batches", response_model=BatchResponse, status_code=status.HTTP_201_CREATED)
    def upload_batch(
        request: Request,
        state: StateDep,
        auth: AuthDep,
        file: UploadFile,
        mapping: Annotated[str | None, Form()] = None,
    ) -> BatchResponse:
        """`mapping` is an optional JSON object of canonical field -> the column
        header in this file, as confirmed on the mapping-preview screen.

        Revalidated here rather than trusted: a client could name a field ARIE
        cannot store or a column that is not in the file, and the first of those
        would be a mapping ingestion silently drops while the customer believed
        it applied. Omitting it leaves `arie.batches`' own alias matching in
        charge, exactly as before this milestone — every existing caller is
        unaffected.
        """
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

        # Parsed once here purely to count rows for the quota check below,
        # and again inside `create_batch` — a second parse of a file already
        # capped at MAX_FILE_SIZE_BYTES/MAX_ROWS is cheap, and keeps this
        # quota logic fully outside `arie.batches`' own, already-tested
        # parse/create/enqueue flow rather than threading a new concern
        # through it.
        field_map = _confirmed_field_map(content, mapping)

        try:
            preview = parse_csv(content, organization_id=auth.organization_id, field_map=field_map)
        except MalformedCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        with state.pool.connection() as conn:
            try:
                enforce_csv_row_quota(
                    conn, organization_id=auth.organization_id, row_count=len(preview)
                )
                enforce_lead_quota(
                    conn, organization_id=auth.organization_id, now=datetime.now(UTC)
                )
            except LimitExceededError as exc:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
                ) from exc

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
                    field_map=field_map,
                )
                progress = batch_progress(conn, organization_id=auth.organization_id, batch=record)
        except MalformedCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        with state.pool.connection() as usage_conn:
            check_and_notify_usage(
                usage_conn, organization_id=auth.organization_id, now=datetime.now(UTC)
            )
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
