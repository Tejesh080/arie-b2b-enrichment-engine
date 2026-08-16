"""The pure lead state graph — no database.

Scope note pinned by test, not just docstring: this graph is a linear
scaffold (NEW -> ... -> DECISION), not a reimplementation of
CalibratedBoundsPolicy's adaptive loop. See arie.statemachine.transitions'
module docstring for why.
"""

from __future__ import annotations

import pytest

from arie.core.types import LeadStatus
from arie.statemachine.transitions import DECISION_OUTCOMES, TERMINAL, job_type_for, next_status


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
    "status",
    [LeadStatus.ROUTED, LeadStatus.SYNCED, LeadStatus.FAILED, LeadStatus.DEAD_LETTER],
)
def test_terminal_statuses_have_no_next_action(status: LeadStatus) -> None:
    assert status in TERMINAL
    assert job_type_for(status) is None
    assert next_status(status) is None


@pytest.mark.parametrize(
    "status",
    [
        LeadStatus.IDENTITY_RESOLVED,
        LeadStatus.AUTO_ROUTED,
        LeadStatus.AWAITING_HUMAN,
        LeadStatus.MANUAL_REVIEW,
    ],
)
def test_statuses_not_yet_wired_do_not_auto_advance(status: LeadStatus) -> None:
    """Not terminal in the business sense, but this module doesn't move them yet:
    IDENTITY_RESOLVED has no job wired (leads carry an already-resolved
    person_id/company_id under the current schema, not raw identity fields --
    see the module docstring); routing/CRM sync and human review aren't built."""
    assert job_type_for(status) is None
    assert next_status(status) is None


def test_every_lead_status_is_classified() -> None:
    """Every status either auto-advances, is terminal, is DECISION (branches on
    outcome), or is an acknowledged not-yet-wired wait -- none silently falls
    through unclassified."""
    not_yet_wired = {
        LeadStatus.IDENTITY_RESOLVED,
        LeadStatus.AUTO_ROUTED,
        LeadStatus.AWAITING_HUMAN,
        LeadStatus.MANUAL_REVIEW,
    }
    auto_advancing = {
        LeadStatus.NEW,
        LeadStatus.SCORING,
        LeadStatus.FETCHING_EVIDENCE,
        LeadStatus.INTEGRATING,
    }

    for status in LeadStatus:
        classified = status in TERMINAL or status in not_yet_wired or status in auto_advancing
        assert classified or status is LeadStatus.DECISION, (
            f"{status} isn't accounted for in the graph's classification"
        )
