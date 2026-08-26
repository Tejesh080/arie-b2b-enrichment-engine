"""The live-mode autonomy guard (Live V1 Foundation, Phase 1).

The invariant proved here, exhaustively rather than by example: **no
combination of recommendation and confidence lets a real-provider lead reach an
authoritative outcome.** The integration counterpart, which drives the same
guard through a real database and the real state machine, is
``tests/integration/test_live_provider_integration.py``.
"""

from __future__ import annotations

import itertools

import pytest

from arie.core.types import Decision, LeadStatus
from arie.jobs.handlers import decision_route
from arie.live.safety import (
    AUTONOMY_SUPPRESSED_ROUTE,
    FORBIDDEN_LIVE_STATUSES,
    LIVE_AUTONOMY_ENABLED,
    LIVE_GUARD_REASON,
    PERMITTED_LIVE_STATUSES,
    LiveAutonomyViolationError,
    autonomy_allowed_for,
    guarded_route,
    verify_live_status,
)
from arie.statemachine.transitions import DECISION_OUTCOMES, FINALIZED, QUALIFIED, REJECTED

_ALL_DECISIONS = tuple(Decision)
_ALL_AUTONOMY = (True, False)


# ------------------------------------------------------------ the pin itself --


def test_live_autonomy_is_disabled() -> None:
    """The single fact this whole phase rests on. If someone flips this
    constant without doing the recalibration work, this test is the thing that
    stops the change from being quiet."""
    assert LIVE_AUTONOMY_ENABLED is False


def test_live_mode_is_not_permitted_to_act_autonomously() -> None:
    assert autonomy_allowed_for("live") is False


def test_simulated_mode_is_unchanged() -> None:
    """Simulated autonomy is validated against an oracle on held-out data. The
    guard must not touch it — the demo and the benchmark both depend on it."""
    assert autonomy_allowed_for("simulated") is True


def test_an_unrecognised_provider_mode_defaults_to_guarded() -> None:
    """A mode string this function does not understand is treated as live. The
    safe direction for an unknown: refusing to automate costs an escalation,
    automating on an unrecognised configuration costs a wrong routing."""
    for mode in ("", "LIVE", "production", "hybrid", "sim"):
        assert autonomy_allowed_for(mode) is False


# ------------------------------------------------------------- routing rules --


@pytest.mark.parametrize(
    ("decision", "autonomous"), list(itertools.product(_ALL_DECISIONS, _ALL_AUTONOMY))
)
def test_guarded_mode_always_escalates_whatever_the_recommendation(
    decision: Decision, autonomous: bool
) -> None:
    """Exhaustive over the full product. Not just `auto_route`: suppressing
    only routing would leave the system autonomously *rejecting* real leads on
    the same uncalibrated threshold."""
    assert guarded_route(decision, autonomous, autonomy_allowed=False) == "escalate_human"


@pytest.mark.parametrize(
    ("decision", "autonomous", "expected"),
    [
        (Decision.AUTO_ROUTE, True, "auto_route"),
        (Decision.AUTO_ROUTE, False, "escalate_human"),
        (Decision.REJECT, True, "reject"),
        (Decision.REJECT, False, "escalate_human"),
        (Decision.ESCALATE_HUMAN, True, "escalate_human"),
        (Decision.ESCALATE_HUMAN, False, "escalate_human"),
    ],
)
def test_unguarded_mode_keeps_the_original_rule_exactly(
    decision: Decision, autonomous: bool, expected: str
) -> None:
    assert guarded_route(decision, autonomous, autonomy_allowed=True) == expected


@pytest.mark.parametrize(
    ("decision", "autonomous"), list(itertools.product(_ALL_DECISIONS, _ALL_AUTONOMY))
)
def test_decision_route_is_the_unguarded_rule_with_no_second_implementation(
    decision: Decision, autonomous: bool
) -> None:
    """`arie.jobs.handlers.decision_route` (the simulated path, the demo, the
    benchmark reporting) and `guarded_route` must be the same function, or the
    two paths can drift on the half they share."""
    assert decision_route(decision, autonomous) == guarded_route(
        decision, autonomous, autonomy_allowed=True
    )


def test_every_route_the_guard_produces_is_a_real_decision_outcome() -> None:
    for decision, autonomous, allowed in itertools.product(
        _ALL_DECISIONS, _ALL_AUTONOMY, _ALL_AUTONOMY
    ):
        assert guarded_route(decision, autonomous, autonomy_allowed=allowed) in DECISION_OUTCOMES


def test_the_suppressed_route_lands_on_awaiting_human() -> None:
    assert DECISION_OUTCOMES[AUTONOMY_SUPPRESSED_ROUTE] is LeadStatus.AWAITING_HUMAN


# ----------------------------------------------------------- status backstop --


@pytest.mark.parametrize("status", sorted(FORBIDDEN_LIVE_STATUSES))
def test_an_authoritative_status_is_rejected_by_the_backstop(status: LeadStatus) -> None:
    with pytest.raises(LiveAutonomyViolationError, match=LIVE_GUARD_REASON):
        verify_live_status(status)


@pytest.mark.parametrize("status", sorted(PERMITTED_LIVE_STATUSES))
def test_the_two_permitted_terminals_pass_the_backstop(status: LeadStatus) -> None:
    assert verify_live_status(status) is status


def test_the_forbidden_set_covers_every_business_finalized_status() -> None:
    """Defined against the state machine's own business-semantic groups rather
    than restated by hand, so a status added to `QUALIFIED` or `REJECTED`
    tomorrow cannot slip past the guard by simply not being listed here."""
    assert FINALIZED <= FORBIDDEN_LIVE_STATUSES
    assert QUALIFIED <= FORBIDDEN_LIVE_STATUSES
    assert REJECTED <= FORBIDDEN_LIVE_STATUSES


def test_permitted_and_forbidden_do_not_overlap() -> None:
    assert not (PERMITTED_LIVE_STATUSES & FORBIDDEN_LIVE_STATUSES)


def test_no_permitted_terminal_is_a_business_outcome() -> None:
    """AWAITING_HUMAN is "paused on a person", SHADOW_EVALUATED is "never a
    business decision at all". Neither may read as a finalized outcome to
    `v_pipeline_metrics`, the n8n outcome-sync gate, or a CRM."""
    assert not (PERMITTED_LIVE_STATUSES & FINALIZED)


# ------------------------------------------------------ end-to-end, in the pure layer --


@pytest.mark.parametrize(
    ("decision", "autonomous"), list(itertools.product(_ALL_DECISIONS, _ALL_AUTONOMY))
)
def test_no_live_recommendation_can_reach_an_authoritative_status(
    decision: Decision, autonomous: bool
) -> None:
    """The invariant end to end through the pure layer: route the decision
    under the live guard, resolve it through the real state graph, and assert
    the result is one a human still controls."""
    route = guarded_route(decision, autonomous, autonomy_allowed=autonomy_allowed_for("live"))
    status = DECISION_OUTCOMES[route]

    assert status in PERMITTED_LIVE_STATUSES
    assert verify_live_status(status) is status
