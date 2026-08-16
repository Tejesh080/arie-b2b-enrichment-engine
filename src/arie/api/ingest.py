"""Lead ingestion — the one write path that turns a raw inbound record into work.

This is where three previously unconnected M1 pieces meet: identity resolution
(Step 7) decides *who* the lead is, the `leads` table records that a decision is
owed, and the job queue (Step 8) schedules the work that will make it. Until
now nothing called any of them in the request path.

**It is one transaction, deliberately.** Identity rows, the lead, its first
event, and its first job all commit together or not at all. The tempting
alternative — resolve identity, commit, insert the lead, commit, then enqueue —
has a failure window that is genuinely nasty: a crash after the lead insert
leaves a lead sitting in ``NEW`` that no worker will ever claim, and a lead with
no job is indistinguishable from a lead whose job simply hasn't run yet. Nothing
alerts, nothing retries, and the lead is quietly lost. Committing once removes
the window rather than monitoring for it.

**Idempotency is the upstream record's own key**, ``(source, external_ref)``,
which ``0001_init.sql`` already made a partial unique index for exactly this
purpose. Re-delivering the same webhook returns the same ``lead_id`` and creates
no second job. A lead posted *without* an ``external_ref`` cannot be deduplicated
and every POST creates a new lead — the same NULL-never-conflicts semantics
``jobs.idempotency_key`` already documents, surfaced in the response as
``created`` so a caller can tell which happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from arie.config import POLICY
from arie.core.types import LeadStatus
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.observability.tracing import get_tracer, inject_trace_context, traced
from arie.statemachine.transitions import job_type_for

_TRACER = get_tracer("arie.api.ingest")

# `ON CONFLICT (source, external_ref) WHERE external_ref IS NOT NULL` names the
# partial unique index from 0001_init.sql. Rows with a NULL external_ref aren't
# in that index at all, so they simply never conflict and always insert.
#
# DO UPDATE rather than DO NOTHING for the same reason as everywhere else in
# this codebase: DO NOTHING skips RETURNING, and a duplicate request still needs
# the existing lead_id to answer with. `updated_at` is a genuine update here —
# the upstream system did re-send this record, and that is worth timestamping.
_INSERT_LEAD = """
    INSERT INTO leads (person_id, company_id, source, external_ref, budget_usd_cap)
    VALUES (%(person_id)s, %(company_id)s, %(source)s, %(external_ref)s, %(budget_usd_cap)s)
    ON CONFLICT (source, external_ref) WHERE external_ref IS NOT NULL
        DO UPDATE SET updated_at = now()
    RETURNING lead_id, status, version, (xmax = 0) AS created
"""

_INSERT_INGEST_EVENT = """
    INSERT INTO lead_events (lead_id, event_type, payload)
    VALUES (%(lead_id)s, 'lead:ingested', %(payload)s)
"""


@dataclass(frozen=True)
class LeadIngestCommand:
    """A raw inbound lead, already validated but not yet resolved to identities."""

    source: str
    email: str
    external_ref: str | None = None
    company_domain: str | None = None
    company_name: str | None = None
    full_name: str | None = None
    title: str | None = None
    budget_usd_cap: Decimal | None = None


@dataclass(frozen=True)
class IngestResult:
    lead_id: UUID
    status: LeadStatus
    version: int
    created: bool
    """False when ``(source, external_ref)`` matched a lead that already existed."""
    company_id: UUID
    person_id: UUID
    job_id: UUID
    job_created: bool
    """False when the lead's first job was already enqueued by an earlier delivery."""


def ingest_lead(
    conn: psycopg.Connection,
    *,
    resolver: IdentityResolver,
    queue: PostgresJobQueue,
    command: LeadIngestCommand,
) -> IngestResult:
    """Resolve, insert, and schedule one lead. Does **not** commit.

    The caller commits, which is what makes the whole thing atomic and what
    lets a failure at any point — a bad domain, a constraint violation, a
    dropped connection — leave the database exactly as it was.
    """
    with traced(
        _TRACER,
        "lead.ingest",
        attributes={"arie.source": command.source, "arie.external_ref": command.external_ref},
    ) as ingest_span:
        with traced(_TRACER, "identity.resolve") as identity_span:
            company, person = resolver.resolve_lead_in(
                conn,
                person_email=command.email,
                company_domain=command.company_domain,
                company_name=command.company_name,
                person_full_name=command.full_name,
                person_title=command.title,
            )
            identity_span.set_attribute("arie.company_id", str(company.company_id))
            identity_span.set_attribute("arie.person_id", str(person.person_id))
            identity_span.set_attribute("arie.company_created", company.created)
            identity_span.set_attribute("arie.person_created", person.created)

        # Always written explicitly rather than left to the column default.
        # `leads.budget_usd_cap` defaults to a value that predates the policy
        # config, and a cap below the priciest provider's price silently makes
        # that provider unbuyable — see PolicyConfig.lead_budget_usd_cap, which
        # documents that exact bug being found once already. One source of
        # truth, and it is the config.
        budget_cap = (
            command.budget_usd_cap
            if command.budget_usd_cap is not None
            else Decimal(str(POLICY.lead_budget_usd_cap))
        )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _INSERT_LEAD,
                {
                    "person_id": person.person_id,
                    "company_id": company.company_id,
                    "source": command.source,
                    "external_ref": command.external_ref,
                    "budget_usd_cap": budget_cap,
                },
            )
            lead_row = cur.fetchone()
            assert lead_row is not None

        lead_id: UUID = lead_row["lead_id"]
        created: bool = lead_row["created"]
        status = LeadStatus(lead_row["status"])

        ingest_span.set_attribute("arie.lead_id", str(lead_id))
        ingest_span.set_attribute("arie.lead_created", created)

        if created:
            payload: dict[str, Any] = {
                "source": command.source,
                "external_ref": command.external_ref,
                "company_id": str(company.company_id),
                "person_id": str(person.person_id),
                "budget_usd_cap": str(budget_cap),
            }
            with conn.cursor() as cur:
                cur.execute(_INSERT_INGEST_EVENT, {"lead_id": lead_id, "payload": Jsonb(payload)})

        # Keyed on the lead and the job type, so a re-delivered webhook can
        # never create a second unit of work for the same lead. Note this uses
        # the job type that advances a *new* lead, not one derived from the
        # lead's current status: on a duplicate the lead may already be
        # mid-pipeline, and the right answer there is "the job you asked for
        # already exists", not "start the pipeline again from the top".
        job_type = job_type_for(LeadStatus.NEW)
        assert job_type is not None  # NEW always has outgoing work; see transitions.py

        with traced(_TRACER, "job.enqueue", attributes={"arie.job_type": job_type}) as enqueue_span:
            enqueued = queue.enqueue_in(
                conn,
                lead_id=lead_id,
                job_type=job_type,
                idempotency_key=f"lead:{lead_id}:{job_type}",
                # Captured inside this span, so the worker that claims the job
                # continues *this* trace rather than starting an orphan one.
                trace_context=inject_trace_context(),
            )
            enqueue_span.set_attribute("arie.job_id", str(enqueued.job_id))
            enqueue_span.set_attribute("arie.job_created", enqueued.created)

        return IngestResult(
            lead_id=lead_id,
            status=status,
            version=lead_row["version"],
            created=created,
            company_id=company.company_id,
            person_id=person.person_id,
            job_id=enqueued.job_id,
            job_created=enqueued.created,
        )
