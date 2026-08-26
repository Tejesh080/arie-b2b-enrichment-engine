"""The live-mode autonomy guard (Live V1 Foundation, Phase 1).

**The invariant, stated once:** no lead enriched by a real provider may reach
an authoritative outcome without a human. Not ``AUTO_ROUTED``, not the reject
terminal, not ``MANUAL_REVIEW``. Every live-mode lead terminates at
``AWAITING_HUMAN`` (a real person must look at it) or ``SHADOW_EVALUATED`` (it
was ingested as shadow and was never going to route anyway).

**Why, precisely.** ``arie.confidence`` fits ``tau`` on the *synthetic
calibration split* — leads whose ground truth the generator wrote down, scored
by rules the generator itself obeys. That procedure is sound, and its
guarantee ("at most ``target_autonomous_error_rate`` of autonomous decisions
are wrong") holds over that distribution and no other. Real provider evidence
is a different distribution: different coverage, different error modes,
different correlation between fields, and — until this step — different
vocabulary entirely. Applying a threshold calibrated on the first to decisions
made on the second is not a conservative approximation; it is an unmeasured
claim wearing a calibrated number's clothes.

The live path already carried that caveat in prose (``arie.jobs.handlers``'
``_build_live_handlers`` docstring, ``docs/architecture.md``'s P5 section). A
prose caveat does not stop a lead from being auto-routed. This module does.

**Not configurable, on purpose.** There is no ``LIVE_AUTONOMY_ENABLED``
environment variable and this module reads no config. The gate that lifts this
guard is a *measurement* — real-world validation and recalibration of the
confidence model against live evidence — not a deployment flag someone can set
under time pressure. Lifting it means deleting :data:`LIVE_AUTONOMY_ENABLED`'s
``False`` and the tests that pin it, which is a code review, which is the
point.

**Shadow mode is unchanged and orthogonal.** A shadow lead was already
non-authoritative in both provider modes (``arie.jobs.handlers``'
``_finalize_decision``). This guard adds the same protection to *non-shadow*
live leads, which previously could and did route autonomously.

**Simulated mode is untouched.** :func:`autonomy_allowed_for` returns ``True``
for it, the demo keeps auto-routing, and the benchmark keeps measuring the
policy's real autonomy — which is the only place autonomy is currently
validated.
"""

from __future__ import annotations

from arie.core.types import Decision, LeadStatus

__all__ = [
    "AUTONOMY_SUPPRESSED_ROUTE",
    "FORBIDDEN_LIVE_STATUSES",
    "LIVE_AUTONOMY_ENABLED",
    "LIVE_GUARD_REASON",
    "PERMITTED_LIVE_STATUSES",
    "LiveAutonomyViolationError",
    "autonomy_allowed_for",
    "guarded_route",
    "verify_live_status",
]

LIVE_AUTONOMY_ENABLED = False
"""Whether ``PROVIDER_MODE=live`` may act without a human. Pinned ``False``
until real-world validation/recalibration is done. Not env-configurable — see
the module docstring."""

LIVE_GUARD_REASON = "live_autonomy_not_validated"
"""The single machine-readable reason string. Written into the escalation's
``lead_events`` payload and surfaced on the Decision Receipt so a reviewer sees
*why* a confident recommendation still landed on their desk, rather than
inferring it from an unexplained escalation."""

AUTONOMY_SUPPRESSED_ROUTE = "escalate_human"
"""The ``DECISION_OUTCOMES`` key every guarded live lead is forced onto."""

PERMITTED_LIVE_STATUSES: frozenset[LeadStatus] = frozenset(
    {LeadStatus.AWAITING_HUMAN, LeadStatus.SHADOW_EVALUATED}
)
"""The only two terminals a real-provider lead may reach through the decision
path. ``FAILED``/``DEAD_LETTER`` are not listed because they are reached by the
worker's error path, never by a decision — a distinction ``verify_live_status``
keeps by only ever being called at the decision branch."""

FORBIDDEN_LIVE_STATUSES: frozenset[LeadStatus] = frozenset(
    {
        LeadStatus.AUTO_ROUTED,
        LeadStatus.ROUTED,
        LeadStatus.SYNCED,
        LeadStatus.MANUAL_REVIEW,
    }
)
"""Every authoritative business outcome, enumerated positively rather than as
"not permitted". ``SYNCED`` is here because it is the *reject* terminal
(``arie.statemachine.transitions.DECISION_OUTCOMES``) — the guard is about
autonomous rejection just as much as autonomous routing, and a set defined by
negation would have quietly let a newly-added status through."""


class LiveAutonomyViolationError(AssertionError):
    """Raised if a live-mode lead is about to reach an authoritative outcome.

    An ``AssertionError`` subclass because reaching it means a code path
    bypassed :func:`guarded_route` — a bug in ARIE, not a bad input. It fails
    the job into the ordinary retry/dead-letter path, which is the correct
    outcome: a lead sitting in ``DEAD_LETTER`` with this message is recoverable
    and loud, and an auto-routed lead is neither.
    """


def autonomy_allowed_for(provider_mode: str) -> bool:
    """Whether ``provider_mode`` may take an autonomous business action.

    ``simulated`` may: its threshold is calibrated on, and measured against,
    the very distribution it runs on. ``live`` may not, until
    :data:`LIVE_AUTONOMY_ENABLED` says otherwise. An unrecognised mode is
    treated as live — the safe direction for a value this function does not
    understand.
    """
    if provider_mode == "simulated":
        return True
    return LIVE_AUTONOMY_ENABLED


def guarded_route(decision: Decision, autonomous: bool, *, autonomy_allowed: bool) -> str:
    """Map a policy outcome onto a ``DECISION_OUTCOMES`` key, honouring the guard.

    With ``autonomy_allowed=True`` this is exactly the pre-existing rule
    (``arie.jobs.handlers.decision_route``): a confident non-escalate decision
    routes itself, anything else goes to a human.

    With ``autonomy_allowed=False`` the answer is always
    :data:`AUTONOMY_SUPPRESSED_ROUTE`, *regardless of the recommendation or the
    confidence*. Both halves matter — suppressing only ``auto_route`` would
    leave the system autonomously rejecting real leads on an uncalibrated
    threshold, which is the same unvalidated claim with a cheaper-looking
    failure mode.

    The recommendation itself is never rewritten. It is still frozen into
    ``decision_receipts.decision`` and passed to ``request_review`` as
    ``original_decision``, so the reviewer sees "ARIE would have auto-routed
    this" rather than a decision that silently became an escalation.
    """
    if not autonomy_allowed:
        return AUTONOMY_SUPPRESSED_ROUTE
    if not autonomous or decision is Decision.ESCALATE_HUMAN:
        return AUTONOMY_SUPPRESSED_ROUTE
    return str(decision)


def verify_live_status(status: LeadStatus) -> LeadStatus:
    """Assert a live-mode decision landed on a non-authoritative terminal.

    Belt-and-braces behind :func:`guarded_route`, in the same spirit as the
    database-level unique indexes this codebase uses behind its application
    invariants: the routing logic is correct, and a second, independent check
    at the point of no return costs nothing and turns a future refactor's
    mistake into a loud failure instead of a real lead being routed to a real
    salesperson on an uncalibrated model.
    """
    if status in FORBIDDEN_LIVE_STATUSES:
        raise LiveAutonomyViolationError(
            f"live-mode lead reached authoritative status {status} — "
            f"{LIVE_GUARD_REASON}; permitted: {sorted(PERMITTED_LIVE_STATUSES)}"
        )
    return status
