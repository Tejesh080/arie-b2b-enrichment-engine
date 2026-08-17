"""Orchestrates the demo scenarios against a running ARIE stack, through
`ArieClient` only — no Postgres, no Docker internals, no n8n. Every id the
demo acts on (lead_id, review_id, lead_version) comes from a prior API
response, never from a database query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arie.evalgen.schema import EvalLead
from scripts.demo.client import DemoApiClient
from scripts.demo.corpus import DemoCorpus
from scripts.demo.render import RenderedDecision, render_decision

DEMO_SOURCE = "arie-demo"
DEMO_REVIEWER = "arie-demo"


@dataclass(frozen=True)
class IdempotencyResult:
    """Redelivering one exact `(source, external_ref)` twice, within one run."""

    source: str
    external_ref: str
    first: dict[str, Any]
    second: dict[str, Any]

    @property
    def is_idempotent(self) -> bool:
        return (
            self.first["lead_id"] == self.second["lead_id"]
            and self.second["created"] is False
            and self.second["job_created"] is False
        )


def _ingest_payload(lead: EvalLead, *, external_ref: str) -> dict[str, Any]:
    return {
        "source": DEMO_SOURCE,
        "email": lead.person.email,
        "external_ref": external_ref,
        "company_domain": lead.company.canonical_domain,
        "company_name": lead.company.legal_name,
        "full_name": lead.person.full_name,
    }


def _label(lead: EvalLead) -> str:
    return f"{lead.person.full_name} — {lead.company.legal_name}"


def run_scenario_a(
    client: DemoApiClient, corpus: DemoCorpus, *, run_id: str, decision_timeout_s: float
) -> tuple[RenderedDecision, IdempotencyResult]:
    """The autonomous decision, plus the idempotent-redelivery proof riding on
    the exact same ingest — scenario C intentionally reuses this one
    `(source, external_ref)`, per the demo's own repeatability rule (unique
    per run, reused exactly once within it)."""
    lead = corpus.autonomous_lead
    external_ref = f"demo-{run_id}-autonomous"
    payload = _ingest_payload(lead, external_ref=external_ref)

    first = client.post_lead(payload)
    second = client.post_lead(payload)  # identical body — the idempotency proof

    receipt = client.wait_for_decision(first["lead_id"], timeout_s=decision_timeout_s)
    rendered = render_decision(_label(lead), receipt)

    idempotency = IdempotencyResult(
        source=DEMO_SOURCE, external_ref=external_ref, first=first, second=second
    )
    return rendered, idempotency


def run_scenario_b(
    client: DemoApiClient, corpus: DemoCorpus, *, run_id: str, decision_timeout_s: float
) -> tuple[RenderedDecision, RenderedDecision]:
    """The human-escalation path: fetch the receipt before any human acts,
    approve via the public review API using only ids the receipt itself
    exposes (`human_review.review_id`), then fetch again. Returns
    (before, after) — `after` carries the frozen recommendation and the live
    override side by side."""
    lead = corpus.escalating_lead
    payload = _ingest_payload(lead, external_ref=f"demo-{run_id}-escalating")
    ingested = client.post_lead(payload)

    before_receipt = client.wait_for_decision(ingested["lead_id"], timeout_s=decision_timeout_s)
    before = render_decision(_label(lead), before_receipt)

    human_review = before.human_review
    if human_review is None:
        # `select_demo_corpus` guarantees a corpus lead that escalates, but
        # not that autonomy could never later reach the same person under a
        # future policy change — guard rather than assume the API's shape.
        return before, before

    review = client.get_review(human_review.review_id)
    client.submit_review_decision(
        human_review.review_id,
        action="approve",
        reviewer=DEMO_REVIEWER,
        expected_lead_version=review["lead_version"],
    )

    after_receipt = client.get_receipt(ingested["lead_id"])
    after = render_decision(_label(lead), after_receipt)
    return before, after


def run_scenario_d(
    client: DemoApiClient, corpus: DemoCorpus, *, run_id: str, decision_timeout_s: float
) -> tuple[RenderedDecision, RenderedDecision] | None:
    """Two distinct contacts at the same company, processed back to back — by
    the time the second is scored, the evidence the first just bought is
    already durable, so the second should show at least one cache reuse
    regardless of whether this machine has run the demo before.

    Returns `None` (the caller omits the scenario) if the corpus has no
    same-company pair, or if no cache reuse was actually observed this run —
    never a fabricated saving."""
    if corpus.same_company_pair is None:
        return None
    lead1, lead2 = corpus.same_company_pair

    first_ingest = client.post_lead(
        _ingest_payload(lead1, external_ref=f"demo-{run_id}-samecompany-1")
    )
    first_receipt = client.wait_for_decision(first_ingest["lead_id"], timeout_s=decision_timeout_s)
    rendered1 = render_decision(_label(lead1), first_receipt)

    second_ingest = client.post_lead(
        _ingest_payload(lead2, external_ref=f"demo-{run_id}-samecompany-2")
    )
    second_receipt = client.wait_for_decision(
        second_ingest["lead_id"], timeout_s=decision_timeout_s
    )
    rendered2 = render_decision(_label(lead2), second_receipt)

    if rendered2.activity.cache_reused_count == 0:
        return None
    return rendered1, rendered2
