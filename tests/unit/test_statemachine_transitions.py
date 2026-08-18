"""The pure lead state graph — no database.

Scope note pinned by test, not just docstring: this graph is a linear
scaffold (NEW -> ... -> DECISION), not a reimplementation of
CalibratedBoundsPolicy's adaptive loop. See arie.statemachine.transitions'
module docstring for why.
"""

from __future__ import annotations

import pytest

from arie.core.types import LeadStatus
from arie.statemachine.transitions import (
    AWAITING_REVIEW,
    DECISION_OUTCOMES,
    FAILURE,
    FINALIZED,
    HUMAN_REVIEW_OUTCOMES,
    QUALIFIED,
    REJECTED,
    TERMINAL,
    job_type_for,
    next_status,
)


def test_new_leads_advance_toward_scoring() -> None:
    assert job_type_for(LeadStatus.NEW) == "compute_score"
    assert next_status(LeadStatus.NEW) == LeadStatus.SCORING


def test_full_auto_advancing_chain_reaches_decision() -> None:
    status = LeadStatus.NEW
    seen = [status]
    for _ in range(10):
        job = job_type_for(status)
        if job is None:
            break
        status = next_status(status)  # type: ignore[assignment]
        seen.append(status)

    assert status == LeadStatus.DECISION
    assert seen == [
        LeadStatus.NEW,
        LeadStatus.SCORING,
        LeadStatus.FETCHING_EVIDENCE,
        LeadStatus.INTEGRATING,
        LeadStatus.DECISION,
    ]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("auto_route", LeadStatus.AUTO_ROUTED),
        ("escalate_human", LeadStatus.AWAITING_HUMAN),
        ("reject", LeadStatus.SYNCED),
    ],
)
def test_decision_branches_on_outcome(outcome: str, expected: LeadStatus) -> None:
    assert next_status(LeadStatus.DECISION, outcome=outcome) == expected


def test_decision_outcomes_table_matches_what_next_status_accepts() -> None:
    for outcome in DECISION_OUTCOMES:
        next_status(LeadStatus.DECISION, outcome=outcome)  # must not raise


@pytest.mark.parametrize("bad_outcome", [None, "", "maybe", "AUTO_ROUTE"])
def test_decision_without_a_recognised_outcome_raises(bad_outcome: str | None) -> None:
    with pytest.raises(ValueError):
        next_status(LeadStatus.DECISION, outcome=bad_outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("auto_route", LeadStatus.AUTO_ROUTED),
        ("reject", LeadStatus.SYNCED),
        ("manual_review", LeadStatus.MANUAL_REVIEW),
    ],
)
def test_human_review_branches_on_outcome(outcome: str, expected: LeadStatus) -> None:
    assert next_status(LeadStatus.AWAITING_HUMAN, outcome=outcome) == expected


def test_human_review_outcomes_table_matches_what_next_status_accepts() -> None:
    for outcome in HUMAN_REVIEW_OUTCOMES:
        next_status(LeadStatus.AWAITING_HUMAN, outcome=outcome)  # must not raise


@pytest.mark.parametrize("bad_outcome", [None, "", "maybe", "escalate_human", "APPROVE"])
def test_awaiting_human_without_a_recognised_outcome_raises(bad_outcome: str | None) -> None:
    """`escalate_human` is deliberately in the bad-outcome list: it's a valid
    DECISION_OUTCOMES key but a lead already in AWAITING_HUMAN can't escalate
    to itself -- the two outcome tables are intentionally not interchangeable."""
    with pytest.raises(ValueError):
        next_status(LeadStatus.AWAITING_HUMAN, outcome=bad_outcome)


@pytest.mark.parametrize(
    "status",
    [
        LeadStatus.ROUTED,
        LeadStatus.SYNCED,
        LeadStatus.FAILED,
        LeadStatus.DEAD_LETTER,
        LeadStatus.SHADOW_EVALUATED,
    ],
)
def test_terminal_statuses_have_no_next_action(status: LeadStatus) -> None:
    assert status in TERMINAL
    assert job_type_for(status) is None
    assert next_status(status) is None


def test_shadow_evaluated_is_not_a_business_semantic_group() -> None:
    """Post-M1 P5: a shadow evaluation is not a business decision. It must be
    mechanically terminal (nothing auto-advances it) without being counted as
    qualified, rejected, awaiting review, or a failure -- see arie.jobs.
    handlers' shadow-branch docstring for why (v_pipeline_metrics/
    v_escalation_rate must never be inflated by shadow-run activity, and
    outcome-sync.json's FINALIZED gate must never treat one as synced)."""
    assert LeadStatus.SHADOW_EVALUATED not in QUALIFIED
    assert LeadStatus.SHADOW_EVALUATED not in REJECTED
    assert LeadStatus.SHADOW_EVALUATED not in AWAITING_REVIEW
    assert LeadStatus.SHADOW_EVALUATED not in FAILURE
    assert LeadStatus.SHADOW_EVALUATED not in FINALIZED


@pytest.mark.parametrize(
    "status",
    [
        LeadStatus.IDENTITY_RESOLVED,
        LeadStatus.AUTO_ROUTED,
        LeadStatus.MANUAL_REVIEW,
    ],
)
def test_statuses_not_yet_wired_do_not_auto_advance(status: LeadStatus) -> None:
    """Not terminal in the business sense, but this module doesn't move them yet:
    IDENTITY_RESOLVED has no job wired (leads carry an already-resolved
    person_id/company_id under the current schema, not raw identity fields --
    see the module docstring); routing/CRM sync (AUTO_ROUTED) isn't built, and
    MANUAL_REVIEW is a resting state with no further auto-advancement defined.
    AWAITING_HUMAN moved out of this group in M1 Step 11 -- it now branches on
    an outcome, the same way DECISION always has (see the tests above)."""
    assert job_type_for(status) is None
    assert next_status(status) is None


def test_every_lead_status_is_classified() -> None:
    """Every status either auto-advances, is terminal, branches on an outcome
    (DECISION, AWAITING_HUMAN), or is an acknowledged not-yet-wired wait --
    none silently falls through unclassified."""
    not_yet_wired = {
        LeadStatus.IDENTITY_RESOLVED,
        LeadStatus.AUTO_ROUTED,
        LeadStatus.MANUAL_REVIEW,
    }
    auto_advancing = {
        LeadStatus.NEW,
        LeadStatus.SCORING,
        LeadStatus.FETCHING_EVIDENCE,
        LeadStatus.INTEGRATING,
    }
    branches_on_outcome = {LeadStatus.DECISION, LeadStatus.AWAITING_HUMAN}

    for status in LeadStatus:
        classified = (
            status in TERMINAL
            or status in not_yet_wired
            or status in auto_advancing
            or status in branches_on_outcome
        )
        assert classified, f"{status} isn't accounted for in the graph's classification"


# --- business-semantic status groups ---------------------------------------
#
# A different axis from TERMINAL above -- see the module's own comment.
# AUTO_ROUTED and MANUAL_REVIEW are QUALIFIED (business-finalized) despite
# not being in TERMINAL (mechanically, nothing auto-advances them yet).


def test_qualified_excludes_the_reject_terminal() -> None:
    """The audit-fixed ambiguity: SYNCED is DECISION_OUTCOMES/HUMAN_REVIEW_
    OUTCOMES's own *reject* target, not a qualified lead -- counting it as
    qualified rewards rejecting more leads, the exact inversion 0005 already
    fixed once for the automatic path."""
    assert LeadStatus.SYNCED not in QUALIFIED
    assert {LeadStatus.AUTO_ROUTED, LeadStatus.ROUTED, LeadStatus.MANUAL_REVIEW} == QUALIFIED


def test_rejected_is_exactly_synced() -> None:
    assert {LeadStatus.SYNCED} == REJECTED


def test_finalized_is_qualified_or_rejected() -> None:
    assert FINALIZED == QUALIFIED | REJECTED


def test_semantic_groups_are_pairwise_disjoint() -> None:
    groups = [QUALIFIED, REJECTED, AWAITING_REVIEW, FAILURE]
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            assert a.isdisjoint(b), f"{a} and {b} overlap"


def test_every_decision_and_review_outcome_lands_in_a_semantic_group() -> None:
    """Every status DECISION_OUTCOMES/HUMAN_REVIEW_OUTCOMES can produce, plus
    the failure terminals, must be classified somewhere in the vocabulary --
    the same "nothing falls through unclassified" guarantee
    test_every_lead_status_is_classified pins for the mechanical axis."""
    reachable_outcomes = set(DECISION_OUTCOMES.values()) | set(HUMAN_REVIEW_OUTCOMES.values())
    for status in reachable_outcomes | FAILURE:
        classified = (
            status in QUALIFIED
            or status in REJECTED
            or status in AWAITING_REVIEW
            or status in FAILURE
        )
        assert classified, f"{status} isn't in any business-semantic group"
