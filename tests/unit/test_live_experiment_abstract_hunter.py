"""scripts/live_experiment_abstract_hunter.py — the pure, no-DB, no-network logic.

Covers the validation-20 additions: loading identities from a Phase-1 dataset
JSON (and excluding LOW-quality rows automatically), parsing a connection
string's (host, port, dbname) identity, and the preflight pass/fail
arithmetic. ``_ensure_database``/``_apply_migrations`` need a real Postgres
and are exercised by actually running ``--preflight`` against the dedicated
local database instead of a unit test double.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.live_experiment_abstract_hunter import (
    SHARED_DB_NAME,
    _db_identity,
    _ingest_command,
    _load_identities_from_file,
    _run_preflight,
)

from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME


def _write_dataset(tmp_path: Path, identities: list[dict[str, object]]) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"identities": identities}), encoding="utf-8")
    return path


def test_load_identities_from_file_maps_fields_and_keeps_high_and_medium(
    tmp_path: Path,
) -> None:
    path = _write_dataset(
        tmp_path,
        [
            {
                "validation_id": "v01",
                "full_name": "Ada Lovelace",
                "email": "ada@example.com",
                "company": "Example Co",
                "company_domain": "example.com",
                "expected_title": "Founder & CEO",
                "ground_truth_quality": "HIGH",
            },
            {
                "validation_id": "v02",
                "full_name": "Bea Bloggs",
                "email": "bea@sample.org",
                "company": "Sample Org",
                "company_domain": "sample.org",
                "expected_title": "Director of Operations",
                "ground_truth_quality": "MEDIUM",
            },
        ],
    )

    identities, excluded = _load_identities_from_file(path)

    assert excluded == 0
    assert len(identities) == 2
    assert identities[0] == {
        "email": "ada@example.com",
        "domain": "example.com",
        "company_name": "Example Co",
        "expected_title": "Founder & CEO",
        "full_name": "Ada Lovelace",
        "validation_id": "v01",
        "ground_truth_quality": "HIGH",
    }
    assert identities[1]["ground_truth_quality"] == "MEDIUM"


def test_load_identities_from_file_excludes_low_quality_automatically(
    tmp_path: Path,
) -> None:
    path = _write_dataset(
        tmp_path,
        [
            {
                "validation_id": "v01",
                "email": "ada@example.com",
                "company": "Example Co",
                "company_domain": "example.com",
                "expected_title": "CEO",
                "ground_truth_quality": "HIGH",
            },
            {
                "validation_id": "v10-flagged",
                "email": "ahsan@marakor.com",
                "company": "Marakor",
                "company_domain": "marakor.com",
                "expected_title": "Founder & CEO",
                "ground_truth_quality": "LOW",
            },
        ],
    )

    identities, excluded = _load_identities_from_file(path)

    assert excluded == 1
    assert len(identities) == 1
    assert identities[0]["email"] == "ada@example.com"
    assert all(row["email"] != "ahsan@marakor.com" for row in identities)


def test_ingest_command_carries_full_name_through() -> None:
    """The validation-20 fix: an identity loaded from --identities-file has a
    full_name, and it must reach LeadIngestCommand — that's the only way
    arie.identity.validation gets anything to compare Hunter's
    matched_identity against."""
    identity = {
        "email": "patrick@stripe.com",
        "domain": "stripe.com",
        "company_name": "Stripe",
        "expected_title": "Co-founder & CEO",
        "full_name": "Patrick Collison",
        "validation_id": "v02",
        "ground_truth_quality": "MEDIUM",
    }

    command = _ingest_command(
        identity, source="live-experiment-abstract-hunter", external_ref="r:0"
    )

    assert command.full_name == "Patrick Collison"
    assert command.email == "patrick@stripe.com"
    assert command.company_domain == "stripe.com"
    assert command.company_name == "Stripe"
    assert command.source == "live-experiment-abstract-hunter"
    assert command.external_ref == "r:0"
    assert command.is_shadow is False


def test_ingest_command_normalizes_a_missing_full_name_to_none() -> None:
    """The original hardcoded five-identity list has no full_name key at all
    — .get(...) returns None, and an empty-string default from a dataset row
    must normalize to None too, not pass a falsy-but-not-None value through."""
    no_key = {
        "email": "jason@37signals.com",
        "domain": "37signals.com",
        "company_name": "37signals",
    }
    empty_string = {**no_key, "full_name": ""}

    for identity in (no_key, empty_string):
        command = _ingest_command(identity, source="s", external_ref="r")
        assert command.full_name is None


def test_db_identity_parses_host_port_dbname() -> None:
    assert _db_identity("postgresql://arie:secret@localhost:5432/arie_live_validation") == (
        "localhost",
        "5432",
        "arie_live_validation",
    )


def test_db_identity_defaults_port_and_lowercases() -> None:
    host, port, dbname = _db_identity("postgresql://arie:secret@LOCALHOST/Arie")
    assert host == "localhost"
    assert port == "5432"
    assert dbname == "arie"


def test_preflight_passes_when_every_check_is_satisfied(capsys) -> None:  # type: ignore[no-untyped-def]
    passed = _run_preflight(
        identities=[{"email": f"p{i}@example.com"} for i in range(15)],
        excluded_count=1,
        expected_count=15,
        db_url="postgresql://arie:arie_local_dev@localhost:5432/arie_live_validation",
        abstract_name=ABSTRACT_PROVIDER_NAME,
        hunter_name=HUNTER_PROVIDER_NAME,
        migrations_applied=[],
    )

    out = capsys.readouterr().out
    assert passed is True
    assert "PREFLIGHT PASSED" in out
    assert "[FAIL]" not in out


def test_preflight_fails_on_identity_count_mismatch(capsys) -> None:  # type: ignore[no-untyped-def]
    passed = _run_preflight(
        identities=[{"email": "solo@example.com"}],
        excluded_count=0,
        expected_count=15,
        db_url="postgresql://arie:arie_local_dev@localhost:5432/arie_live_validation",
        abstract_name=ABSTRACT_PROVIDER_NAME,
        hunter_name=HUNTER_PROVIDER_NAME,
        migrations_applied=[],
    )

    out = capsys.readouterr().out
    assert passed is False
    assert "PREFLIGHT FAILED" in out
    assert "[FAIL] eligible identities loaded" in out


def test_preflight_fails_and_refuses_the_shared_database(capsys) -> None:  # type: ignore[no-untyped-def]
    passed = _run_preflight(
        identities=[{"email": f"p{i}@example.com"} for i in range(15)],
        excluded_count=1,
        expected_count=15,
        db_url=f"postgresql://arie:arie_local_dev@localhost:5432/{SHARED_DB_NAME}",
        abstract_name=ABSTRACT_PROVIDER_NAME,
        hunter_name=HUNTER_PROVIDER_NAME,
        migrations_applied=[],
    )

    out = capsys.readouterr().out
    assert passed is False
    assert "[FAIL] dedicated experiment database" in out


def test_preflight_fails_when_a_non_hunter_or_abstract_provider_sneaks_in(capsys) -> None:  # type: ignore[no-untyped-def]
    passed = _run_preflight(
        identities=[{"email": f"p{i}@example.com"} for i in range(15)],
        excluded_count=1,
        expected_count=15,
        db_url="postgresql://arie:arie_local_dev@localhost:5432/arie_live_validation",
        abstract_name=ABSTRACT_PROVIDER_NAME,
        hunter_name="apollo_person_enrichment",
        migrations_applied=[],
    )

    out = capsys.readouterr().out
    assert passed is False
    assert "[FAIL] providers: Abstract + Hunter only" in out
