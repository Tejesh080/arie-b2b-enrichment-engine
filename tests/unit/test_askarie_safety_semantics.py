"""Ask ARIE's safety and semantic invariants, offline.

These are the guarantees that must hold no matter which model is behind the
copilot, so every one of them is asserted against ARIE's own deterministic
code rather than against a model's cooperation. Three families:

**Grounding.** A claim citing an evidence id that is not in this lead's own
pool is dropped, whether the id is invented or belongs to another lead in
another organization. `arie.intelligence.explanation._sanitize` is the gate;
no guardrail can do this job, because "is this UUID real and is it yours" is a
database question.

**Vocabulary.** The `intent not in LIST_INTENTS` check in
`arie.copilot_service` is what caught every one of Gemma 3 4B's benchmark
failures — it answered list questions with single-lead intents. Both families
live in one `CopilotIntent` enum, so Pydantic cannot catch this and the guard
is the only thing standing between a confused model and a wrong answer.

**Wording.** "No verified person evidence is available" describes ARIE.
"No person verified the lead" describes a human review workflow that does not
exist in this product.
"""

from __future__ import annotations

import uuid

import pytest

from arie.copilot import (
    LEAD_INTENTS,
    LIST_INTENTS,
    CopilotIntent,
)
from arie.copilot_service import _COMPARE_INSTRUCTIONS
from arie.intelligence.explanation import _INSTRUCTIONS as EXPLANATION_INSTRUCTIONS
from arie.intelligence.explanation import (
    FORBIDDEN_PERSON_EVIDENCE_WORDING,
    NO_VERIFIED_PERSON_EVIDENCE,
    PERSON_EVIDENCE_LABELS,
    EvidenceGroundedClaim,
    LeadExplanation,
    _sanitize,
    deterministic_explanation,
    lacks_verified_person_evidence,
)
from arie.recommendations import (
    FIELD_LABELS,
    ConfidenceBand,
    CustomerPriority,
    LeadRecommendation,
    NextAction,
    ResearchStatus,
)


def _recommendation(*, missing: list[str] | None = None) -> LeadRecommendation:
    return LeadRecommendation(
        lead_id=uuid.uuid4(),
        priority=CustomerPriority.WORTH_PURSUING,
        next_action=NextAction.FIND_DECISION_MAKER,
        machine_decision="route_to_human",
        score=0.58,
        confidence=0.61,
        confidence_band=ConfidenceBand.MEDIUM,
        short_reason="Company size and industry match your profile.",
        key_evidence=["company size", "industry"],
        missing_information=missing if missing is not None else ["contact seniority"],
        research_status=ResearchStatus.PARTIAL,
        explanation_status="not_requested",
        profile_version=3,
        shadow=False,
        execution_mode="simulated",
        evidence_sufficiency="insufficient_evidence",
    )


def _explanation(*claims: EvidenceGroundedClaim) -> LeadExplanation:
    return LeadExplanation(
        summary="A summary.",
        claims=list(claims),
        missing_information=[],
        hypothesis_notes=[],
    )


# ------------------------------------------------------------------ grounding --


def test_fabricated_evidence_id_is_dropped() -> None:
    """The model invented a UUID. The claim goes, not just the citation — an
    unsupported factual claim is worth less than nothing."""
    real = uuid.uuid4()
    fabricated = uuid.uuid4()
    cleaned = _sanitize(
        _explanation(
            EvidenceGroundedClaim(text="Grounded.", evidence_ids=[real], hypothesis=False),
            EvidenceGroundedClaim(text="Invented.", evidence_ids=[fabricated], hypothesis=False),
        ),
        {real},
    )
    assert [c.text for c in cleaned.claims] == ["Grounded."]


def test_cross_org_evidence_id_cannot_appear() -> None:
    """A real id belonging to another organization's lead is treated exactly
    like an invented one: the pool is the authorization boundary, and it is
    built by a tenant-scoped query."""
    ours = uuid.uuid4()
    theirs = uuid.uuid4()
    cleaned = _sanitize(
        _explanation(
            EvidenceGroundedClaim(
                text="Their lead has a CTO.", evidence_ids=[theirs], hypothesis=False
            ),
            EvidenceGroundedClaim(text="Ours matches.", evidence_ids=[ours], hypothesis=False),
        ),
        {ours},
    )
    assert [c.text for c in cleaned.claims] == ["Ours matches."]
    assert all(theirs not in c.evidence_ids for c in cleaned.claims)


def test_a_partly_grounded_claim_keeps_only_its_real_citations() -> None:
    ours = uuid.uuid4()
    theirs = uuid.uuid4()
    cleaned = _sanitize(
        _explanation(
            EvidenceGroundedClaim(text="Mixed.", evidence_ids=[ours, theirs], hypothesis=False)
        ),
        {ours},
    )
    assert cleaned.claims[0].evidence_ids == [ours]


def test_a_hypothesis_survives_without_citations_but_stays_marked() -> None:
    """Hypotheses are the one thing that may stand uncited — they are offered
    as possibilities, and the UI must render them as such."""
    cleaned = _sanitize(
        _explanation(
            EvidenceGroundedClaim(
                text="They may be expanding.", evidence_ids=[uuid.uuid4()], hypothesis=True
            )
        ),
        set(),
    )
    assert len(cleaned.claims) == 1
    assert cleaned.claims[0].hypothesis is True
    assert cleaned.claims[0].evidence_ids == []


def test_an_empty_pool_drops_every_factual_claim() -> None:
    cleaned = _sanitize(
        _explanation(
            EvidenceGroundedClaim(text="A.", evidence_ids=[uuid.uuid4()], hypothesis=False),
            EvidenceGroundedClaim(text="B.", evidence_ids=[], hypothesis=False),
        ),
        set(),
    )
    assert cleaned.claims == []


# ----------------------------------------------------------------- vocabulary --


def test_list_and_lead_intents_are_disjoint() -> None:
    assert not (set(LIST_INTENTS) & set(LEAD_INTENTS))


@pytest.mark.parametrize(
    "confused",
    [CopilotIntent.LEAD_RESEARCHABILITY, CopilotIntent.LEAD_EXPLANATION],
)
def test_a_single_lead_intent_is_rejected_for_a_list_question(
    confused: CopilotIntent,
) -> None:
    """Gemma 3 4B returned exactly these for list questions on the benchmark.
    `CopilotIntent` is one enum spanning both families, so schema validation
    accepts them and only this membership check does not."""
    assert confused not in LIST_INTENTS


@pytest.mark.parametrize("confused", [CopilotIntent.NEEDS_RESEARCH, CopilotIntent.TOP_LEADS])
def test_a_list_intent_is_rejected_for_a_single_lead_question(
    confused: CopilotIntent,
) -> None:
    assert confused not in LEAD_INTENTS


# -------------------------------------------------------------------- wording --


def test_person_evidence_absence_uses_the_approved_wording() -> None:
    explanation = deterministic_explanation(_recommendation(missing=["contact seniority"]))
    assert NO_VERIFIED_PERSON_EVIDENCE in explanation.missing_information


def test_person_evidence_absence_never_says_a_person_failed_to_verify() -> None:
    """The distinction that matters: ARIE has no human review step, so
    describing one would send a customer looking for a reviewer."""
    explanation = deterministic_explanation(_recommendation(missing=["contact seniority"]))
    blob = " ".join(
        [
            explanation.summary,
            *explanation.missing_information,
            *(c.text for c in explanation.claims),
        ]
    ).lower()
    assert FORBIDDEN_PERSON_EVIDENCE_WORDING not in blob
    assert "verified the lead" not in blob


def test_the_sentence_is_absent_when_person_evidence_did_resolve() -> None:
    """It must not become boilerplate on every lead — a lead whose contact
    fields resolved has verified person evidence and should not be told
    otherwise."""
    explanation = deterministic_explanation(_recommendation(missing=["industry"]))
    assert NO_VERIFIED_PERSON_EVIDENCE not in explanation.missing_information


@pytest.mark.parametrize("field", ["title_seniority", "title_function"])
def test_both_person_fields_trigger_the_wording(field: str) -> None:
    label = FIELD_LABELS[field]
    assert label in PERSON_EVIDENCE_LABELS
    assert lacks_verified_person_evidence([label]) is True


def test_non_person_gaps_do_not_trigger_the_wording() -> None:
    assert lacks_verified_person_evidence(["industry", "company size"]) is False
    assert lacks_verified_person_evidence([]) is False


@pytest.mark.parametrize(
    "instructions", [EXPLANATION_INSTRUCTIONS, _COMPARE_INSTRUCTIONS], ids=["explain", "compare"]
)
def test_every_customer_facing_prompt_carries_the_wording_rule(instructions: str) -> None:
    """The deterministic path is covered above; this covers the generated
    path, which is the one that could invent the wrong framing."""
    assert NO_VERIFIED_PERSON_EVIDENCE in instructions
    assert "no human reviews leads" in instructions


def test_the_forbidden_wording_appears_nowhere_in_the_prompts() -> None:
    for instructions in (EXPLANATION_INSTRUCTIONS, _COMPARE_INSTRUCTIONS):
        body = instructions.lower()
        forbidden_at = body.find(FORBIDDEN_PERSON_EVIDENCE_WORDING)
        # It may appear only inside the rule that prohibits it -- and that rule
        # says "Never describe it as...", so the phrase itself must not occur.
        assert forbidden_at == -1
