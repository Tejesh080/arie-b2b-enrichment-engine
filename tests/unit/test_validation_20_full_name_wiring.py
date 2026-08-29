"""Root-causes a defect the validation-20 real run exposed (2026-08-30).

patrick@stripe.com's lead in that run recorded Hunter's ``title_seniority:
ic`` as usable evidence — Hunter had honestly resolved the mailbox to Patrick
Bosmans, an IT Administrator, not the intended Patrick Collison (CEO). That is
exactly the case ``arie.identity.validation`` was built to catch (see
``tests/unit/test_identity_validation.py``'s
``test_stripe_same_company_different_person_is_a_mismatch``, itself written
after the *previous* real run hit this same identity) — but the validator
only has a name to compare against when the requested lead carries one, and
``scripts/live_experiment_abstract_hunter.py``'s ``LeadIngestCommand`` calls
never pass ``full_name``/``title``, even though ``identities.json`` (and the
identity dicts loaded from it) carry ``full_name`` for exactly this purpose.

This is a confirming test, not a fix: it shows the *existing, unmodified*
``arie.identity.validation`` + ``arie.jobs.handlers`` code already handles
this correctly once given a name to compare — the gap is purely in what the
experiment script threads through to ingestion, and stays unfixed here
pending a decision on scope (see the validation-20 report).
"""

from __future__ import annotations

import uuid
from typing import cast

from arie.core.types import ProviderResult, ProviderStatus
from arie.identity.validation import MISMATCH
from arie.jobs.handlers import _LeadIdentity, _validate_person_match
from arie.providers.base import EnrichmentProvider

# The real Hunter response recorded in
# data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.json for
# patrick@stripe.com, reproduced verbatim.
_HUNTER_RESULT = ProviderResult(
    fields={"title_function": "engineering", "title_seniority": "ic"},
    confidence=0.75,
    cost_usd=0.0049,
    latency_ms=450.0,
    status=ProviderStatus.SUCCESS,
    raw={
        "matched_identity": {
            "full_name": "Patrick Bosmans",
            "title": "IT Administrator",
            "email": "patrick@stripe.com",
            "employer_name": "Stripe",
            "employer_domain": "stripe.com",
        }
    },
)


class _FakePersonProvider:
    entity_type = "person"


def _identity(*, full_name: str | None) -> _LeadIdentity:
    return _LeadIdentity(
        company_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        canonical_email="patrick@stripe.com",
        canonical_domain="stripe.com",
        is_shadow=False,
        full_name=full_name,
    )


def test_without_a_requested_full_name_the_wrong_person_evidence_is_not_caught() -> None:
    """Reproduces exactly what happened in the validation-20 run:
    ``LeadIngestCommand`` never received ``full_name``, so ``_LeadIdentity``
    resolved with ``full_name=None`` and Hunter's Patrick-Bosmans-not-Collison
    result sailed through with its (wrong) title_seniority/title_function
    still marked usable."""
    identity = _identity(full_name=None)

    result, validation = _validate_person_match(
        identity, cast(EnrichmentProvider, _FakePersonProvider()), _HUNTER_RESULT
    )

    assert validation is not None
    assert validation.verdict != MISMATCH
    assert result.fields == {"title_function": "engineering", "title_seniority": "ic"}


def test_with_the_requested_full_name_present_the_same_response_is_correctly_rejected() -> None:
    """What would have happened had the experiment script passed
    ``full_name="Patrick Collison"`` through to ``LeadIngestCommand``, as the
    validation-20 dataset already carries for every identity. No change to
    ``arie.identity.validation`` or ``arie.jobs.handlers`` was needed — only
    the missing input."""
    identity = _identity(full_name="Patrick Collison")

    result, validation = _validate_person_match(
        identity, cast(EnrichmentProvider, _FakePersonProvider()), _HUNTER_RESULT
    )

    assert validation is not None
    assert validation.verdict == MISMATCH
    assert result.fields == {}
