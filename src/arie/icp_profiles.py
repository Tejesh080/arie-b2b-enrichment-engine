"""Organization ICP/scoring profiles — immutable, versioned configuration
(Productization M3). Owns every piece of `organization_icp_profiles` (see
`migrations/0019_organization_icp_profiles.sql`): config validation, creating
a new version (which atomically retires whichever version was previously
active), listing, point lookups, and materializing a stored config into the
`arie.scoring.rules.ScoringConfig` the scorer actually reads.

**The scorer is not rewritten.** `materialize_scoring_config` only builds the
dynamically-scoped override `arie.scoring.rules.use_scoring_config` activates
around a lead's processing (`arie.jobs.handlers`) — see that function's
module docstring for the full mechanism.

**Config shape, and why there is no separate top-level "weights" object.** A
field's ceiling (how many points it can contribute at most) is *derived*,
never separately declared, for every field with a category point-map or a
banded range: `max(industry_points.values())`, `max(seniority_points.
values())`, `max(function_points.values())`, and the highest `points` among
`employee_count_bands`. Giving those four fields both a point-map *and* an
independent "weight" would let the two disagree (a submitted `industry`
weight of 15 alongside an `industry_points` map whose highest value is 20) —
a real inconsistency this design makes structurally impossible instead of
merely validating against. Only `buying_intent` (continuous, scaled 0..1 by
`arie.scoring.rules.field_points`) and `recent_trigger_event` (binary) have
no natural point-map, so those two alone are declared directly as
`buying_intent_weight`/`trigger_event_weight`. `validate_config` still
enforces that all six ceilings sum to 100, preserving the reference config's
0-100 scale so `qualify_threshold`/`reject_threshold` keep the same meaning
regardless of how an organization distributes weight across fields.

`target_geographies` is accepted and stored but never scored — geography is
not one of `arie.scoring.rules.SCORED_FIELDS` because no evidence field
supplies it today (see `arie.icp.REFERENCE_ICP_V1`, which treats geography
the same way: declared, advisory, unobservable). Storing it is honest
forward-compatibility, not a silent no-op the caller can't see: the frontend
must label it as advisory-only, not as a filter that affects scoring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from arie.scoring.rules import RULES_VERSION, ScoringConfig

__all__ = [
    "REFERENCE_CONFIG",
    "WEIGHT_SUM_TARGET",
    "ICPProfileRecord",
    "InvalidICPConfigError",
    "create_profile",
    "get_active_profile",
    "get_profile_by_version",
    "list_profiles",
    "materialize_scoring_config",
    "resolve_scoring_config",
    "validate_config",
]

WEIGHT_SUM_TARGET = 100.0
"""The reference config's six field ceilings sum to exactly this
(20 + 15 + 20 + 15 + 20 + 10). Every organization profile must too, or
`qualify_threshold`/`reject_threshold` stop meaning what they say on a
0-100 scale."""

_WEIGHT_SUM_TOLERANCE = 1e-6

REFERENCE_CONFIG: dict[str, Any] = {
    "qualify_threshold": 65.0,
    "reject_threshold": 55.0,
    "employee_count_bands": [
        {"min_employees": 1, "max_employees": 10, "points": 2.0},
        {"min_employees": 11, "max_employees": 50, "points": 10.0},
        {"min_employees": 51, "max_employees": 200, "points": 20.0},
        {"min_employees": 201, "max_employees": 1000, "points": 18.0},
        {"min_employees": 1001, "max_employees": 1_000_000_000, "points": 8.0},
    ],
    "industry_points": {
        "software": 15.0,
        "fintech": 15.0,
        "healthtech": 13.0,
        "ecommerce": 12.0,
        "logistics": 8.0,
        "manufacturing": 7.0,
        "education": 5.0,
        "nonprofit": 2.0,
    },
    "seniority_points": {
        "c_level": 20.0,
        "vp": 18.0,
        "director": 14.0,
        "manager": 8.0,
        "ic": 2.0,
    },
    "function_points": {
        "data": 15.0,
        "engineering": 14.0,
        "operations": 9.0,
        "marketing": 5.0,
        "sales": 5.0,
        "finance": 4.0,
        "other": 2.0,
    },
    "buying_intent_weight": 20.0,
    "trigger_event_weight": 10.0,
    "target_geographies": [],
    "disqualifier_enabled": True,
}
"""The exact transcription of `arie.scoring.rules`'s hardcoded reference
constants into this module's config shape. `migrations/0019_organization_icp_
profiles.sql` bootstraps every pre-existing organization with this literal
JSON — `tests/unit/test_icp_profiles.py` pins the two together (parses the
migration file's JSON blob and asserts equality) so they cannot silently
drift. Not used by any runtime code path — organizations always read their
own stored row, never this constant — its only jobs are that test and
documentation."""


class InvalidICPConfigError(ValueError):
    """`config` failed structural validation. The message joins every
    violation found (not just the first) so a caller building an edit form
    can surface them all at once."""


@dataclass(frozen=True)
class ICPProfileRecord:
    profile_id: UUID
    organization_id: UUID
    version: int
    name: str
    config: dict[str, Any]
    scorer_version: str
    status: str
    created_by_user_id: UUID | None
    created_at: datetime
    activated_at: datetime
    retired_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def _row_to_record(row: Mapping[str, Any]) -> ICPProfileRecord:
    return ICPProfileRecord(
        profile_id=row["profile_id"],
        organization_id=row["organization_id"],
        version=row["version"],
        name=row["name"],
        config=dict(row["config"]),
        scorer_version=row["scorer_version"],
        status=row["status"],
        created_by_user_id=row["created_by_user_id"],
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        retired_at=row["retired_at"],
    )


def _field_ceiling_from_bands(bands: Sequence[Mapping[str, Any]]) -> float:
    return max((band.get("points", 0.0) for band in bands), default=0.0)


def validate_config(config: Mapping[str, Any]) -> None:
    """Raise :class:`InvalidICPConfigError` if `config` is not a well-formed,
    safe scoring configuration. Never evaluates or imports anything from
    `config` — every check below is a plain type/range/consistency check
    against known keys, which is what keeps this "structured validated
    configuration" rather than a rule language a customer could use to run
    arbitrary logic.
    """
    errors: list[str] = []

    qualify = config.get("qualify_threshold")
    reject = config.get("reject_threshold")
    if (
        not isinstance(qualify, int | float)
        or isinstance(qualify, bool)
        or not (0.0 <= qualify <= 100.0)
    ):
        errors.append("qualify_threshold must be a number between 0 and 100")
        qualify = None
    if (
        not isinstance(reject, int | float)
        or isinstance(reject, bool)
        or not (0.0 <= reject <= 100.0)
    ):
        errors.append("reject_threshold must be a number between 0 and 100")
        reject = None
    if qualify is not None and reject is not None and reject >= qualify:
        errors.append("reject_threshold must be strictly less than qualify_threshold")

    bands = config.get("employee_count_bands")
    if not isinstance(bands, list) or not bands:
        errors.append("employee_count_bands must be a non-empty list")
        bands = []
    else:
        for band in bands:
            if not isinstance(band, dict):
                errors.append(f"employee_count_bands entry {band!r} must be an object")
                continue
            raw = (band.get("min_employees"), band.get("max_employees"), band.get("points"))
            if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in raw):
                errors.append(
                    f"employee_count_bands entry {band!r} must have numeric "
                    "min_employees/max_employees/points"
                )
                continue
            lo, hi, pts = (float(v) for v in raw)  # type: ignore[arg-type]
            if lo < 0 or hi < lo:
                errors.append(f"employee_count_bands entry {band!r} has an invalid min/max range")
            if pts < 0:
                errors.append(f"employee_count_bands entry {band!r} has a negative points value")

    point_maps: dict[str, dict[str, float]] = {}
    for map_name in ("industry_points", "seniority_points", "function_points"):
        mapping = config.get(map_name)
        if not isinstance(mapping, dict):
            errors.append(f"{map_name} must be an object mapping category name -> points")
            continue
        clean: dict[str, float] = {}
        for key, value in mapping.items():
            if not isinstance(key, str) or not key:
                errors.append(f"{map_name} has a non-string or empty key: {key!r}")
                continue
            if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
                errors.append(f"{map_name}[{key!r}] must be a non-negative number")
                continue
            clean[key] = float(value)
        point_maps[map_name] = clean

    weight_fields: dict[str, float] = {}
    for name in ("buying_intent_weight", "trigger_event_weight"):
        value = config.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            errors.append(f"{name} must be a non-negative number")
        else:
            weight_fields[name] = float(value)

    if not errors:
        ceiling_total = (
            _field_ceiling_from_bands(bands)
            + max(point_maps["industry_points"].values(), default=0.0)
            + max(point_maps["seniority_points"].values(), default=0.0)
            + max(point_maps["function_points"].values(), default=0.0)
            + weight_fields["buying_intent_weight"]
            + weight_fields["trigger_event_weight"]
        )
        if abs(ceiling_total - WEIGHT_SUM_TARGET) > _WEIGHT_SUM_TOLERANCE:
            errors.append(
                "field ceilings (employee_count band max + industry/seniority/function map "
                f"max + buying_intent_weight + trigger_event_weight) must sum to "
                f"{WEIGHT_SUM_TARGET}; got {ceiling_total}"
            )

    target_geographies = config.get("target_geographies", [])
    if not isinstance(target_geographies, list) or not all(
        isinstance(g, str) for g in target_geographies
    ):
        errors.append("target_geographies must be a list of strings")

    disqualifier_enabled = config.get("disqualifier_enabled", True)
    if not isinstance(disqualifier_enabled, bool):
        errors.append("disqualifier_enabled must be a boolean")

    if errors:
        raise InvalidICPConfigError("; ".join(errors))


def materialize_scoring_config(
    config: Mapping[str, Any], *, profile_id: UUID, version: int
) -> ScoringConfig:
    """Build the `arie.scoring.rules.ScoringConfig` a validated, stored
    `config` describes. Assumes `config` already passed :func:`validate_config`
    — rows in `organization_icp_profiles` are immutable once written, so a
    stored config cannot have become invalid since; this function does not
    re-validate.
    """
    bands = tuple(
        (int(band["min_employees"]), int(band["max_employees"]), float(band["points"]))
        for band in config["employee_count_bands"]
    )
    return ScoringConfig(
        qualify_threshold=float(config["qualify_threshold"]),
        reject_threshold=float(config["reject_threshold"]),
        size_bands=bands,
        industry_points={k: float(v) for k, v in config["industry_points"].items()},
        seniority_points={k: float(v) for k, v in config["seniority_points"].items()},
        function_points={k: float(v) for k, v in config["function_points"].items()},
        intent_max_points=float(config["buying_intent_weight"]),
        trigger_points=float(config["trigger_event_weight"]),
        disqualifier_enabled=bool(config.get("disqualifier_enabled", True)),
        profile_id=profile_id,
        profile_version=version,
    )


_LOCK_ORGANIZATION = "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text))"
"""Serializes concurrent `create_profile` calls for the *same* organization
for the duration of the caller's transaction — without this, two concurrent
admins each reading "no active profile yet" and each inserting version 1
would both succeed as far as the retire-then-insert statements below are
concerned, and the partial unique index would only catch the case where both
land as `status='active'` simultaneously, not two racing version-number
computations. A hash collision between two *different* organizations' locks
only over-serializes (briefly blocks an unrelated organization's own
unrelated create), never under-serializes — correctness for the single-active
-row invariant itself still rests on `idx_organization_icp_profiles_one_active`
regardless of this lock."""

_SELECT_MAX_VERSION = """
    SELECT COALESCE(MAX(version), 0) AS max_version
    FROM organization_icp_profiles
    WHERE organization_id = %(organization_id)s
"""

_RETIRE_ACTIVE = """
    UPDATE organization_icp_profiles
    SET status = 'retired', retired_at = now()
    WHERE organization_id = %(organization_id)s AND status = 'active'
"""

_INSERT_PROFILE = """
    INSERT INTO organization_icp_profiles (
        organization_id, version, name, config, scorer_version, status,
        created_by_user_id, activated_at
    ) VALUES (
        %(organization_id)s, %(version)s, %(name)s, %(config)s, %(scorer_version)s, 'active',
        %(created_by_user_id)s, now()
    )
    RETURNING profile_id, organization_id, version, name, config, scorer_version, status,
              created_by_user_id, created_at, activated_at, retired_at
"""

_SELECT_ACTIVE = """
    SELECT profile_id, organization_id, version, name, config, scorer_version, status,
           created_by_user_id, created_at, activated_at, retired_at
    FROM organization_icp_profiles
    WHERE organization_id = %(organization_id)s AND status = 'active'
"""

_SELECT_BY_VERSION = """
    SELECT profile_id, organization_id, version, name, config, scorer_version, status,
           created_by_user_id, created_at, activated_at, retired_at
    FROM organization_icp_profiles
    WHERE organization_id = %(organization_id)s AND version = %(version)s
"""

_SELECT_ALL_FOR_ORG = """
    SELECT profile_id, organization_id, version, name, config, scorer_version, status,
           created_by_user_id, created_at, activated_at, retired_at
    FROM organization_icp_profiles
    WHERE organization_id = %(organization_id)s
    ORDER BY version DESC
"""


def create_profile(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    name: str,
    config: Mapping[str, Any],
    scorer_version: str = RULES_VERSION,
) -> ICPProfileRecord:
    """Validate `config`, create it as the next version for `organization_id`,
    and atomically retire whichever version was previously active. Commits.

    The new row is always `status='active'` — there is no draft state in V1;
    an admin submitting a new version means it immediately governs new
    decisions, exactly as Part 1 of the M3 brief specifies ("changing
    configuration affects NEW decisions only" — the previous version's rows,
    and every receipt already written against them, are untouched).
    """
    validate_config(config)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LOCK_ORGANIZATION, {"organization_id": organization_id})
        cur.execute(_SELECT_MAX_VERSION, {"organization_id": organization_id})
        max_version_row = cur.fetchone()
        assert max_version_row is not None  # COALESCE(..., 0) always returns exactly one row
        next_version = max_version_row["max_version"] + 1
        cur.execute(_RETIRE_ACTIVE, {"organization_id": organization_id})
        cur.execute(
            _INSERT_PROFILE,
            {
                "organization_id": organization_id,
                "version": next_version,
                "name": name,
                "config": Jsonb(dict(config)),
                "scorer_version": scorer_version,
                "created_by_user_id": created_by_user_id,
            },
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return _row_to_record(row)


def get_active_profile(
    conn: psycopg.Connection, *, organization_id: UUID
) -> ICPProfileRecord | None:
    """The organization's current active profile, or `None` if it has never
    created one — a lead for such an organization scores against the
    reference `ScoringConfig()` default, matching pre-M3 behaviour exactly.
    In practice every organization existing before this migration was
    bootstrapped a version-1 "Reference ICP" profile
    (`migrations/0019_organization_icp_profiles.sql`), so `None` is expected
    only for an organization created by some future path that does not also
    bootstrap one.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ACTIVE, {"organization_id": organization_id})
        row = cur.fetchone()
    return _row_to_record(row) if row is not None else None


def get_profile_by_version(
    conn: psycopg.Connection, *, organization_id: UUID, version: int
) -> ICPProfileRecord | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_BY_VERSION, {"organization_id": organization_id, "version": version})
        row = cur.fetchone()
    return _row_to_record(row) if row is not None else None


def list_profiles(conn: psycopg.Connection, *, organization_id: UUID) -> list[ICPProfileRecord]:
    """Every version ever created for this organization, newest first —
    permanent audit history; nothing here is ever deleted."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ALL_FOR_ORG, {"organization_id": organization_id})
        rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def resolve_scoring_config(conn: psycopg.Connection, *, organization_id: UUID) -> ScoringConfig:
    """The `ScoringConfig` a lead in `organization_id` should be scored
    against right now — the sole integration point `arie.jobs.handlers` needs
    to make a lead's processing organization-aware.

    Reads the organization's active profile if one exists and materializes
    it; falls back to the reference `ScoringConfig()` — byte-identical to
    every pre-Productization-M3 decision — if it does not (see
    `get_active_profile`'s own docstring for when that's expected). Reads
    with `conn` so this observes whatever `organization_icp_profiles` state
    is visible in the caller's own transaction; a concurrent activation can
    only land strictly before or strictly after this read, never torn
    mid-read, because `create_profile` retires the old row and inserts the
    new one in one transaction of its own.
    """
    profile = get_active_profile(conn, organization_id=organization_id)
    if profile is None:
        return ScoringConfig()
    return materialize_scoring_config(
        profile.config, profile_id=profile.profile_id, version=profile.version
    )
