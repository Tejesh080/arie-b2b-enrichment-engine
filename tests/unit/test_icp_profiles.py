"""Unit tests for `arie.icp_profiles`'s pure logic — config validation and
materialization into `arie.scoring.rules.ScoringConfig`. The create/list/get
persistence functions need a real `organization_icp_profiles` table and are
covered against a live database in
`tests/integration/test_icp_profiles_integration.py` instead.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from arie.icp_profiles import (
    REFERENCE_CONFIG,
    InvalidICPConfigError,
    materialize_scoring_config,
    validate_config,
)
from arie.scoring.rules import ScoringConfig

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "0019_organization_icp_profiles.sql"
)


def test_reference_config_is_valid() -> None:
    validate_config(REFERENCE_CONFIG)  # must not raise


def test_reference_config_materializes_to_the_scorer_defaults() -> None:
    """`ScoringConfig()`'s own field defaults already reproduce
    `arie.scoring.rules`'s hardcoded constants (see `test_scoring_config.py`).
    This proves the *stored, JSON-shaped* form of the reference ICP produces
    the identical materialized config — the property that keeps a bootstrapped
    organization's leads scoring exactly as they did before Productization M3.
    """
    materialized = materialize_scoring_config(REFERENCE_CONFIG, profile_id=uuid4(), version=1)
    reference = ScoringConfig()

    assert materialized.qualify_threshold == reference.qualify_threshold
    assert materialized.reject_threshold == reference.reject_threshold
    assert materialized.size_bands == reference.size_bands
    assert materialized.industry_points == reference.industry_points
    assert materialized.seniority_points == reference.seniority_points
    assert materialized.function_points == reference.function_points
    assert materialized.intent_max_points == reference.intent_max_points
    assert materialized.trigger_points == reference.trigger_points
    assert materialized.disqualifier_enabled == reference.disqualifier_enabled


def test_migration_bootstrap_json_matches_reference_config() -> None:
    """Pins `migrations/0019...sql`'s literal bootstrap JSON to
    `REFERENCE_CONFIG` so the two copies (SQL, only re-runnable/idempotent as
    a migration, and Python, importable for tests) cannot silently drift —
    the same "mirror, don't hand-duplicate" discipline
    `scripts/sync_supabase_migrations.py` enforces for the migration mirror
    itself.
    """
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"'(\{.*?\})'::jsonb", sql, flags=re.DOTALL)
    assert match is not None, "could not find the bootstrap config JSON literal in the migration"
    bootstrap_config = json.loads(match.group(1))
    assert bootstrap_config == REFERENCE_CONFIG


def test_reject_threshold_must_be_strictly_less_than_qualify_threshold() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["qualify_threshold"] = 50.0
    config["reject_threshold"] = 50.0
    with pytest.raises(InvalidICPConfigError, match="strictly less"):
        validate_config(config)


def test_weights_must_sum_to_one_hundred() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["trigger_event_weight"] = 999.0
    with pytest.raises(InvalidICPConfigError, match="sum to 100"):
        validate_config(config)


def test_negative_weight_is_rejected() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["buying_intent_weight"] = -5.0
    with pytest.raises(InvalidICPConfigError, match="buying_intent_weight"):
        validate_config(config)


def test_malformed_employee_band_is_rejected() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["employee_count_bands"] = [{"min_employees": 10, "max_employees": 5, "points": 1.0}]
    with pytest.raises(InvalidICPConfigError, match="invalid min/max range"):
        validate_config(config)


def test_empty_employee_bands_is_rejected() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["employee_count_bands"] = []
    with pytest.raises(InvalidICPConfigError, match="non-empty"):
        validate_config(config)


def test_non_numeric_point_value_is_rejected() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["industry_points"]["software"] = "a lot"
    with pytest.raises(InvalidICPConfigError, match="industry_points"):
        validate_config(config)


def test_disqualifier_enabled_must_be_boolean() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["disqualifier_enabled"] = "yes"
    with pytest.raises(InvalidICPConfigError, match="disqualifier_enabled"):
        validate_config(config)


def test_target_geographies_must_be_a_list_of_strings() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["target_geographies"] = ["US", 42]
    with pytest.raises(InvalidICPConfigError, match="target_geographies"):
        validate_config(config)


def test_multiple_violations_are_all_reported() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["qualify_threshold"] = 200.0
    config["disqualifier_enabled"] = "nope"
    with pytest.raises(InvalidICPConfigError) as exc_info:
        validate_config(config)
    message = str(exc_info.value)
    assert "qualify_threshold" in message
    assert "disqualifier_enabled" in message


def test_a_custom_but_valid_config_is_accepted() -> None:
    config = copy.deepcopy(REFERENCE_CONFIG)
    config["qualify_threshold"] = 70.0
    config["reject_threshold"] = 40.0
    config["industry_points"] = {"construction": 15.0}
    validate_config(config)  # must not raise
    materialized = materialize_scoring_config(config, profile_id=uuid4(), version=2)
    assert materialized.qualify_threshold == 70.0
    assert materialized.industry_points == {"construction": 15.0}
    assert materialized.profile_version == 2
