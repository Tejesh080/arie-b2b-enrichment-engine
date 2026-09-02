"""Targeting changes ARIE suggests, and a person decides on.

The rule this module exists to enforce is in its docstring rather than in a
comment somewhere: **ARIE never rewrites a customer's targeting on its own.**
Historical results produce a *proposal*. A proposal sits in a table with a
status. Nothing about a lead's score changes until somebody with authority
reads it and accepts, and accepting goes through
``arie.icp_profiles.create_profile`` like every other profile change — a new
immutable version, the previous one retired, receipts already written untouched.

**The changes are derived deterministically from the statistics, not written by
a model.** :func:`derive_changes` reads
``arie.intelligence.outcomes.OutcomeAnalysis`` and proposes concrete edits to a
:class:`~arie.intelligence.schemas.BusinessProfileDraft`: mark this size band
preferred, mark that industry acceptable, raise this dimension's importance one
step. A model's contribution is the prose around them, and a proposal is
complete and applicable without one.

**A proposal names the profile version it was reasoning about.** Accepting one
computed against version 3 while version 5 is live is a real situation — a
customer can leave a proposal open for a week — and it is detected rather than
silently applied to whatever happens to be current.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from arie.icp_profiles import ICPProfileRecord, get_active_profile, get_profile_by_version
from arie.intelligence.outcomes import (
    MODERATE_MIN_SAMPLE,
    GroupStat,
    OutcomeAnalysis,
    OutcomeInterpretation,
    SignalStrength,
)
from arie.intelligence.schemas import (
    BandPreference,
    BusinessProfileDraft,
    EmployeeBand,
    PreferenceLevel,
    ScoringDimension,
    TargetingObjective,
)
from arie.intelligence.targeting import confirm_targeting_draft, stored_draft

__all__ = [
    "ACTIONABLE_SIGNALS",
    "ChangeKind",
    "ProposalRecord",
    "ProposalSource",
    "ProposalStatus",
    "ProposedChange",
    "RevisionProposal",
    "StaleProposalError",
    "accept_proposal",
    "apply_changes",
    "build_revision_proposal",
    "create_proposal",
    "derive_changes",
    "get_proposal",
    "list_proposals",
    "reject_proposal",
]


class ProposalSource(StrEnum):
    HISTORICAL_OUTCOMES = "historical_outcomes"
    USER_FEEDBACK = "user_feedback"
    """Not produced by anything yet — thumbs-up/down arrives in a later slice.
    Declared now because the column's CHECK constraint has to know about it,
    and adding a value to a constraint later is a migration this avoids."""


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


ACTIONABLE_SIGNALS: frozenset[SignalStrength] = frozenset(
    {SignalStrength.MODERATE, SignalStrength.STRONG}
)
"""A ``WEAK`` group is shown to the customer as an observation and never turned
into a proposed change. The whole risk of this feature is somebody acting on
noise, and the cheapest defence is refusing to put noise in front of them as a
suggestion — the numbers are still there to read."""

_LEVEL_ORDER: tuple[PreferenceLevel, ...] = (
    PreferenceLevel.NONE,
    PreferenceLevel.LOW,
    PreferenceLevel.MEDIUM,
    PreferenceLevel.HIGH,
    PreferenceLevel.CRITICAL,
)


def _shift(level: PreferenceLevel, steps: int) -> PreferenceLevel:
    """Move a preference one step, clamped. Never wraps, never skips."""
    index = _LEVEL_ORDER.index(level)
    return _LEVEL_ORDER[max(0, min(len(_LEVEL_ORDER) - 1, index + steps))]


class ChangeKind(StrEnum):
    EMPLOYEE_BAND = "employee_band"
    INDUSTRY = "industry"
    DIMENSION_IMPORTANCE = "dimension_importance"


@dataclass(frozen=True)
class ProposedChange:
    """One concrete edit to a targeting profile, and why.

    Concrete on purpose. "Increase preference for mid-sized companies" is not
    something a customer can evaluate; "mark 51-200 people as ideal, up from
    worth-contacting" is. `from_value`/`to_value` are both stated so the console
    can show the before and after without recomputing anything.
    """

    kind: ChangeKind
    dimension: str
    target: str
    """The band key, canonical industry, or dimension the change applies to."""
    target_label: str
    from_value: str
    to_value: str
    rationale: str
    """One associational sentence from the statistics — never causal. See
    ``arie.intelligence.outcomes``."""


def derive_changes(
    analysis: OutcomeAnalysis, current: BusinessProfileDraft
) -> list[ProposedChange]:
    """Turn statistics into concrete profile edits. Pure; no model, no cost.

    Only groups whose signal is in :data:`ACTIONABLE_SIGNALS` produce a change,
    and only where the change would actually differ from what the profile
    already says — a proposal telling a customer to keep doing what they are
    doing is noise, and a list of them would train people to dismiss the
    feature unread.

    Every change moves by one step. Nothing here jumps a preference from
    ``low`` to ``critical`` on the strength of one spreadsheet, and a negative
    signal demotes rather than excludes: turning a group off entirely on 40
    examples is a bigger claim than this data can carry.
    """
    changes: list[ProposedChange] = []
    dimensions_seen: set[str] = set()

    for group in analysis.groups:
        if group.signal not in ACTIONABLE_SIGNALS:
            continue
        improving = group.describes_an_improvement

        if group.dimension == str(ScoringDimension.EMPLOYEE_COUNT):
            band = EmployeeBand(group.group_key)
            currently = current.employee_band_preferences.get(band, BandPreference.ACCEPTABLE)
            wanted = BandPreference.PREFERRED if improving else BandPreference.ACCEPTABLE
            if improving and currently is BandPreference.PREFERRED:
                continue
            if not improving and currently is not BandPreference.PREFERRED:
                # Already not favoured. Demoting to `avoid` would be a bigger
                # claim than a single dataset supports.
                continue
            changes.append(
                ProposedChange(
                    kind=ChangeKind.EMPLOYEE_BAND,
                    dimension=group.dimension,
                    target=str(band),
                    target_label=group.group_label,
                    from_value=str(currently),
                    to_value=str(wanted),
                    rationale=group.sentence(),
                )
            )
        elif group.dimension == str(ScoringDimension.INDUSTRY):
            industry = group.group_key
            if improving:
                if industry in current.preferred_industries:
                    continue
                target_state = "preferred"
                from_state = (
                    "acceptable" if industry in current.acceptable_industries else "not listed"
                )
            else:
                if industry not in current.preferred_industries:
                    continue
                target_state = "acceptable"
                from_state = "preferred"
            changes.append(
                ProposedChange(
                    kind=ChangeKind.INDUSTRY,
                    dimension=group.dimension,
                    target=industry,
                    target_label=group.group_label,
                    from_value=from_state,
                    to_value=target_state,
                    rationale=group.sentence(),
                )
            )

        # One importance nudge per dimension, from its strongest group only.
        if group.dimension not in dimensions_seen and group.sample_size >= MODERATE_MIN_SAMPLE:
            dimensions_seen.add(group.dimension)
            dimension = ScoringDimension(group.dimension)
            currently_level = current.relative_preferences.get(dimension, PreferenceLevel.MEDIUM)
            wanted_level = _shift(currently_level, 1)
            if wanted_level is not currently_level and improving:
                changes.append(
                    ProposedChange(
                        kind=ChangeKind.DIMENSION_IMPORTANCE,
                        dimension=group.dimension,
                        target=group.dimension,
                        target_label=group.group_label,
                        from_value=str(currently_level),
                        to_value=str(wanted_level),
                        rationale=(
                            f"This dataset separates outcomes by {group.dimension.replace('_', ' ')} "
                            f"more than by anything else ARIE could measure."
                        ),
                    )
                )

    return changes


def apply_changes(
    draft: BusinessProfileDraft, changes: list[ProposedChange]
) -> BusinessProfileDraft:
    """Apply proposed changes to a draft. Pure, total, and order-independent.

    Returns a new draft; the input is untouched. This is the only thing
    acceptance does to the profile itself — everything after it is Slice 2's
    existing path, so an accepted proposal and a hand-edited draft produce the
    same kind of profile version by the same code.
    """
    bands: dict[EmployeeBand, BandPreference] = dict(draft.employee_band_preferences)
    # `list[str]`, not the draft's `list[CanonicalIndustry]`: a change's target
    # came out of a customer's spreadsheet, and the only honest way to promote
    # it back into the typed schema is to revalidate the whole draft at the end
    # — which is what happens below, and which rejects an industry that is not
    # canonical rather than casting it into a field that promised it was.
    preferred: list[str] = list(draft.preferred_industries)
    acceptable: list[str] = list(draft.acceptable_industries)
    levels = dict(draft.relative_preferences)

    for change in changes:
        if change.kind is ChangeKind.EMPLOYEE_BAND:
            bands[EmployeeBand(change.target)] = BandPreference(change.to_value)
        elif change.kind is ChangeKind.INDUSTRY:
            if change.to_value == "preferred":
                if change.target not in preferred:
                    preferred.append(change.target)
                acceptable = [i for i in acceptable if i != change.target]
            else:
                preferred = [i for i in preferred if i != change.target]
                if change.target not in acceptable:
                    acceptable.append(change.target)
        else:
            levels[ScoringDimension(change.dimension)] = PreferenceLevel(change.to_value)

    return BusinessProfileDraft.model_validate(
        {
            **draft.model_dump(mode="json"),
            "employee_band_preferences": {str(k): str(v) for k, v in bands.items()},
            "preferred_industries": preferred,
            "acceptable_industries": acceptable,
            "relative_preferences": {str(k): str(v) for k, v in levels.items()},
        }
    )


@dataclass(frozen=True)
class RevisionProposal:
    """A proposal, before it has been persisted."""

    source: ProposalSource
    summary: str
    changes: list[ProposedChange]
    evidence_strength: SignalStrength
    sample_size: int
    observations: list[str]
    caveats: list[str]
    statistics: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "observations": list(self.observations),
            "caveats": list(self.caveats),
            "changes": [
                {
                    "kind": str(c.kind),
                    "dimension": c.dimension,
                    "target": c.target,
                    "target_label": c.target_label,
                    "from_value": c.from_value,
                    "to_value": c.to_value,
                    "rationale": c.rationale,
                }
                for c in self.changes
            ],
        }


def _statistics_json(analysis: OutcomeAnalysis) -> dict[str, Any]:
    """The aggregates worth keeping, and only those.

    Not the dataset. A customer reading a proposal three days later should see
    the numbers it was based on; nobody needs their list of who they won and
    lost living in a table that has no delete path.
    """
    return {
        "labelled_rows": analysis.labelled_rows,
        "positive_count": analysis.positive_count,
        "negative_count": analysis.negative_count,
        "baseline_rate": round(analysis.baseline_rate, 4),
        "warnings": list(analysis.warnings),
        "groups": [
            {
                "dimension": g.dimension,
                "group_key": g.group_key,
                "group_label": g.group_label,
                "sample_size": g.sample_size,
                "positive_count": g.positive_count,
                "negative_count": g.negative_count,
                "positive_rate": round(g.positive_rate, 4),
                "baseline_rate": round(g.baseline_rate, 4),
                "rate_difference": round(g.rate_difference, 4),
                "signal": str(g.signal),
            }
            for g in analysis.groups
        ],
    }


def _strongest(
    changes: list[ProposedChange], analysis: OutcomeAnalysis
) -> tuple[SignalStrength, int]:
    actionable = [g for g in analysis.groups if g.signal in ACTIONABLE_SIGNALS]
    if not actionable or not changes:
        return SignalStrength.INSUFFICIENT_DATA, analysis.labelled_rows
    best: GroupStat = max(
        actionable, key=lambda g: (g.signal is SignalStrength.STRONG, g.sample_size)
    )
    return best.signal, best.sample_size


def build_revision_proposal(
    analysis: OutcomeAnalysis,
    current: BusinessProfileDraft,
    *,
    interpretation: OutcomeInterpretation | None = None,
) -> RevisionProposal | None:
    """Assemble a proposal, or ``None`` when the data does not support one.

    ``None`` is the common and correct answer for a small or flat dataset, and
    it is not a failure: "your results do not yet say anything about who to
    target" is a useful, honest thing for a product to report.

    `interpretation` is a model's prose about the same aggregates. Optional in
    every sense — the proposal's summary falls back to a sentence assembled
    from the statistics, which is less fluent and exactly as true.
    """
    changes = derive_changes(analysis, current)
    if not changes:
        return None

    strength, sample = _strongest(changes, analysis)
    if interpretation is not None:
        summary = interpretation.summary
        observations = list(interpretation.observations)
        caveats = list(interpretation.caveats)
    else:
        summary = (
            f"Across {analysis.labelled_rows} past results, some groups had a "
            f"noticeably different positive-outcome rate from your overall "
            f"{analysis.baseline_rate:.0%}. ARIE has suggested "
            f"{len(changes)} targeting change{'s' if len(changes) != 1 else ''} below."
        )
        observations = [c.rationale for c in changes]
        caveats = []

    caveats = caveats or [
        "These are patterns in your own past data, not proof that one kind of "
        "company buys because of what makes it that kind of company.",
    ]
    return RevisionProposal(
        source=ProposalSource.HISTORICAL_OUTCOMES,
        summary=summary,
        changes=changes,
        evidence_strength=strength,
        sample_size=sample,
        observations=observations,
        caveats=caveats,
        statistics=_statistics_json(analysis),
    )


# ---------------------------------------------------------------- storage --


@dataclass(frozen=True)
class ProposalRecord:
    """One row of `profile_revision_proposals`."""

    proposal_id: UUID
    organization_id: UUID
    profile_id: UUID
    profile_version: int
    source: str
    status: str
    summary: str
    proposal: dict[str, Any]
    supporting_statistics: dict[str, Any]
    evidence_strength: str
    sample_size: int
    created_by_user_id: UUID | None
    created_at: datetime
    resolved_by_user_id: UUID | None
    resolved_at: datetime | None
    resulting_profile_id: UUID | None

    @property
    def is_open(self) -> bool:
        return self.status == ProposalStatus.PROPOSED


class StaleProposalError(RuntimeError):
    """The proposal was computed against a profile version that is no longer active.

    Raised rather than applied. A customer who confirmed a new targeting
    profile yesterday should not have a week-old suggestion silently folded
    into it — the changes were derived from a starting point that no longer
    exists, and "mark 51-200 preferred, up from acceptable" may now be wrong in
    both halves.
    """


_COLUMNS = """
    proposal_id, organization_id, profile_id, profile_version, source, status, summary,
    proposal, supporting_statistics, evidence_strength, sample_size,
    created_by_user_id, created_at, resolved_by_user_id, resolved_at, resulting_profile_id
"""

_INSERT = f"""
    INSERT INTO profile_revision_proposals (
        organization_id, profile_id, profile_version, source, summary,
        proposal, supporting_statistics, evidence_strength, sample_size, created_by_user_id
    ) VALUES (
        %(organization_id)s, %(profile_id)s, %(profile_version)s, %(source)s, %(summary)s,
        %(proposal)s, %(supporting_statistics)s, %(evidence_strength)s, %(sample_size)s,
        %(created_by_user_id)s
    )
    RETURNING {_COLUMNS}
"""

_SELECT_ONE = f"""
    SELECT {_COLUMNS} FROM profile_revision_proposals
    WHERE proposal_id = %(proposal_id)s AND organization_id = %(organization_id)s
"""

_SELECT_FOR_ORG = f"""
    SELECT {_COLUMNS} FROM profile_revision_proposals
    WHERE organization_id = %(organization_id)s
    ORDER BY created_at DESC
    LIMIT %(limit)s
"""

_RESOLVE = f"""
    UPDATE profile_revision_proposals
    SET status = %(status)s,
        resolved_by_user_id = %(resolved_by_user_id)s,
        resolved_at = now(),
        resulting_profile_id = %(resulting_profile_id)s
    WHERE proposal_id = %(proposal_id)s
      AND organization_id = %(organization_id)s
      AND status = 'proposed'
    RETURNING {_COLUMNS}
"""
"""`AND status = 'proposed'` is the concurrency control. Two admins clicking
Accept on the same proposal is an ordinary race, and the second one updating
zero rows — and being told the proposal is already resolved — is better than
both creating a profile version from the same suggestion."""


def _to_record(row: dict[str, Any]) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        organization_id=row["organization_id"],
        profile_id=row["profile_id"],
        profile_version=row["profile_version"],
        source=row["source"],
        status=row["status"],
        summary=row["summary"],
        proposal=dict(row["proposal"]),
        supporting_statistics=dict(row["supporting_statistics"]),
        evidence_strength=row["evidence_strength"],
        sample_size=row["sample_size"],
        created_by_user_id=row["created_by_user_id"],
        created_at=row["created_at"],
        resolved_by_user_id=row["resolved_by_user_id"],
        resolved_at=row["resolved_at"],
        resulting_profile_id=row["resulting_profile_id"],
    )


def create_proposal(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    profile: ICPProfileRecord,
    proposal: RevisionProposal,
) -> ProposalRecord:
    """Persist a proposal against the profile version it was computed from. Commits.

    Writes nothing but this row. The profile is read to record which version
    the reasoning applies to; it is not touched.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _INSERT,
            {
                "organization_id": organization_id,
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "source": str(proposal.source),
                "summary": proposal.summary,
                "proposal": Jsonb(proposal.to_json()),
                "supporting_statistics": Jsonb(proposal.statistics),
                "evidence_strength": str(proposal.evidence_strength),
                "sample_size": proposal.sample_size,
                "created_by_user_id": created_by_user_id,
            },
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return _to_record(row)


def get_proposal(
    conn: psycopg.Connection, *, organization_id: UUID, proposal_id: UUID
) -> ProposalRecord | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ONE, {"proposal_id": proposal_id, "organization_id": organization_id})
        row = cur.fetchone()
    return _to_record(row) if row is not None else None


def list_proposals(
    conn: psycopg.Connection, *, organization_id: UUID, limit: int = 20
) -> list[ProposalRecord]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_FOR_ORG, {"organization_id": organization_id, "limit": limit})
        rows = cur.fetchall()
    return [_to_record(row) for row in rows]


def reject_proposal(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    proposal_id: UUID,
    user_id: UUID,
) -> ProposalRecord | None:
    """Dismiss a proposal. Touches nothing but this row, by construction —
    there is no profile write on this path at all."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _RESOLVE,
            {
                "proposal_id": proposal_id,
                "organization_id": organization_id,
                "status": str(ProposalStatus.REJECTED),
                "resolved_by_user_id": user_id,
                "resulting_profile_id": None,
            },
        )
        row = cur.fetchone()
    conn.commit()
    return _to_record(row) if row is not None else None


def accept_proposal(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    proposal_id: UUID,
    user_id: UUID,
    name: str,
    now: datetime,
) -> tuple[ProposalRecord, ICPProfileRecord]:
    """Apply a proposal as a new immutable profile version.

    The path is deliberately the same one a customer's own edit takes: the
    stored draft the profile was built from, plus the proposed changes, through
    ``arie.intelligence.targeting.confirm_targeting_draft`` — which recomputes
    the scoring configuration deterministically and calls
    ``create_profile``. Nothing here writes to `organization_icp_profiles`
    directly, so there is no second way for a profile version to come into
    existence.

    Raises :class:`StaleProposalError` when the proposal's version is no longer
    the active one, or when the profile it was computed from carries no stored
    draft (a hand-written configuration, or the bootstrap reference profile —
    there is nothing to apply changes *to*).
    """
    record = get_proposal(conn, organization_id=organization_id, proposal_id=proposal_id)
    if record is None or not record.is_open:
        raise StaleProposalError("this suggestion has already been dealt with")

    active = get_active_profile(conn, organization_id=organization_id)
    if active is None or active.version != record.profile_version:
        raise StaleProposalError(
            "your targeting has changed since ARIE made this suggestion, so it no "
            "longer applies. Upload your results again for a fresh one."
        )

    source_profile = get_profile_by_version(
        conn, organization_id=organization_id, version=record.profile_version
    )
    assert source_profile is not None  # `active` above is this row
    draft = stored_draft(source_profile.config)
    if draft is None:
        raise StaleProposalError(
            "this targeting profile was set up directly rather than described in "
            "words, so ARIE cannot adjust it automatically. Edit it on the "
            "targeting screen instead."
        )

    changes = [
        ProposedChange(
            kind=ChangeKind(entry["kind"]),
            dimension=entry["dimension"],
            target=entry["target"],
            target_label=entry["target_label"],
            from_value=entry["from_value"],
            to_value=entry["to_value"],
            rationale=entry["rationale"],
        )
        for entry in record.proposal.get("changes", [])
    ]
    updated = apply_changes(draft, changes)

    objective = TargetingObjective(
        source_profile.config.get("generation", {}).get(
            "objective", str(TargetingObjective.BEST_PROSPECTS)
        )
    )
    created = confirm_targeting_draft(
        conn,
        organization_id=organization_id,
        created_by_user_id=user_id,
        name=name,
        profile=updated,
        objective=objective,
        now=now,
    )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _RESOLVE,
            {
                "proposal_id": proposal_id,
                "organization_id": organization_id,
                "status": str(ProposalStatus.ACCEPTED),
                "resolved_by_user_id": user_id,
                "resulting_profile_id": created.profile_id,
            },
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:  # pragma: no cover - another admin resolved it mid-flight
        raise StaleProposalError("this suggestion has already been dealt with")
    return _to_record(row), created
