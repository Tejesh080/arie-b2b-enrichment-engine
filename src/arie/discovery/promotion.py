"""Promoting a screened discovery candidate into the canonical lead pipeline
— Discovery Pivot Phase 7. From here on, nothing in this package scores,
researches, or decides anything; `arie.api.ingest.ingest_lead` is the exact
same entry point CSV upload and `POST /leads` already use.

**Why a synthetic contact email.** ARIE's identity model — every M1-M7 slice
of it — is keyed on (company, person), not company alone: `leads.person_id`
is `NOT NULL`, and the scorer's person-level fields (contact seniority,
function) are exactly what later tells a customer whether ARIE already knows
who to contact. A discovery candidate starts as a company with no known
person. Rather than build a second, company-only pipeline (`arie.discovery`'s
own standing rule is to reuse this one, not fork it), promotion mints one
placeholder identity per candidate — `discovery+<candidate_id>@<domain>` —
so the existing pipeline can run unmodified. This is not a claim that such a
person exists: `PROVIDER_MODE=simulated` already labels every fact it
produces as modelled, and this pivot's `arie.discovery.opportunity` never
promotes the placeholder's synthesized display name to a customer-facing
"buyer" — see `BuyerSignal.name_known`, always `False` here. What the
placeholder *does* let through honestly is company-level evidence (industry,
employee count, buying intent), which a real firmographics provider would
answer from the domain alone regardless of who the contact turns out to be.

Idempotent by construction: `external_ref` is `run:<run_id>:candidate:
<candidate_id>`, so retrying a discovery run (or promoting the same domain
twice within one run — dedupe already prevents that, but belt and braces)
never creates a second lead.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import psycopg

from arie.api.ingest import IngestResult, LeadIngestCommand, ingest_lead
from arie.discovery.models import DiscoveryCandidate
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue

__all__ = ["promote_candidate", "synthetic_contact_email"]


def synthetic_contact_email(candidate_id: UUID, domain: str) -> str:
    token = hashlib.sha256(str(candidate_id).encode("utf-8")).hexdigest()[:12]
    return f"discovery+{token}@{domain}"


@dataclass(frozen=True)
class PromotionResult:
    candidate: DiscoveryCandidate
    ingest: IngestResult


def promote_candidate(
    conn: psycopg.Connection,
    *,
    resolver: IdentityResolver,
    queue: PostgresJobQueue,
    organization_id: UUID,
    run_id: UUID,
    candidate: DiscoveryCandidate,
) -> PromotionResult:
    """Ingest one candidate as a lead. Does not commit — same contract as
    `ingest_lead` itself, so a caller promoting several candidates in one
    discovery run can batch them into one transaction, or not, as it likes.
    """
    command = LeadIngestCommand(
        source="discovery",
        email=synthetic_contact_email(candidate.candidate_id, candidate.domain),
        organization_id=organization_id,
        external_ref=f"run:{run_id}:candidate:{candidate.candidate_id}",
        company_domain=candidate.domain,
        company_name=candidate.company_name,
        budget_usd_cap=None,
        is_shadow=False,
    )
    result = ingest_lead(conn, resolver=resolver, queue=queue, command=command)
    return PromotionResult(candidate=candidate, ingest=result)
