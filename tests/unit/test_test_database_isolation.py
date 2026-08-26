"""The integration suite's own isolation guards.

These are unit tests on purpose: the thing under test is *whether the
integration suite is allowed to connect at all*, so it cannot be an integration
test without assuming the answer. Nothing here touches a database.

The failure being prevented is concrete. The integration fixtures read
``DATABASE_URL``, which on a developer machine with a populated ``.env`` is the
deployed database. The suite created and deleted rows there, and the deployed
worker — polling the same database — claimed the tests' jobs within about a
second and processed them with its own handlers.
"""

from __future__ import annotations

import pytest
from scripts.test_db import (
    MARKER_TABLE,
    IntegrationDatabaseGuardError,
    _identity,
    assert_not_production,
)

from arie.config import DatabaseConfig, IntegrationDatabaseConfig

_PROD = "postgresql://u:p@db.example.com:5432/postgres"


# ------------------------------------------------------------ no fallback --


def test_the_test_config_reads_its_own_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@localhost:5432/arie_test")
    assert IntegrationDatabaseConfig().url.endswith("/arie_test")


def test_an_unset_test_url_never_falls_back_to_the_production_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole isolation story in one assertion. With DATABASE_URL set and
    TEST_DATABASE_URL unset, the test config must be empty — which makes the
    suite skip rather than quietly write to a deployment."""
    monkeypatch.setenv("DATABASE_URL", _PROD)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_DIRECT_URL", raising=False)

    config = IntegrationDatabaseConfig()
    assert config.url == ""
    assert config.direct_url == ""
    assert not config.configured
    assert DatabaseConfig().url == _PROD  # production is configured; still unreachable from here


def test_the_direct_url_falls_back_only_to_the_pooled_test_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _PROD)
    monkeypatch.setenv("DATABASE_DIRECT_URL", _PROD)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@localhost:5432/arie_test")
    monkeypatch.delenv("TEST_DATABASE_DIRECT_URL", raising=False)

    assert IntegrationDatabaseConfig().direct_url.endswith("/arie_test")


# --------------------------------------------------------- explicit intent --


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE"])
def test_only_the_literal_one_opts_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Deliberately not truthy-parsed. A flag that accepts "true", "yes", and
    "on" is a flag someone sets by accident; requiring exactly ``1`` means the
    value was copied from documentation that explains what it does."""
    monkeypatch.setenv("ARIE_ALLOW_INTEGRATION_TEST_DB", value)
    assert IntegrationDatabaseConfig().allow is False


def test_the_opt_in_flag_is_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIE_ALLOW_INTEGRATION_TEST_DB", "1")
    assert IntegrationDatabaseConfig().allow is True


# ------------------------------------------------- refusing production URLs --


def test_a_test_url_matching_production_is_refused() -> None:
    with pytest.raises(IntegrationDatabaseGuardError, match="same database as DATABASE_URL"):
        assert_not_production(_PROD, _PROD)


def test_different_credentials_on_the_same_database_are_still_the_same_database() -> None:
    """Compared on (host, port, dbname), not on the raw string. A read-only
    role, a rotated password, or a trailing query string all reach the same
    rows, and a string comparison would wave every one of them through."""
    same_rows = "postgresql://other_user:other_pw@db.example.com:5432/postgres?sslmode=require"
    with pytest.raises(IntegrationDatabaseGuardError):
        assert_not_production(same_rows, _PROD)


def test_the_default_postgres_port_is_normalised() -> None:
    with pytest.raises(IntegrationDatabaseGuardError):
        assert_not_production(
            "postgresql://u:p@db.example.com/postgres",
            "postgresql://u:p@db.example.com:5432/postgres",
        )


@pytest.mark.parametrize(
    "test_url",
    [
        "postgresql://u:p@localhost:5432/arie_test",  # different host
        "postgresql://u:p@db.example.com:5432/arie_test",  # different database
        "postgresql://u:p@db.example.com:6543/postgres",  # different port
    ],
)
def test_a_genuinely_separate_database_is_allowed(test_url: str) -> None:
    assert_not_production(test_url, _PROD)


def test_no_configured_production_url_is_not_treated_as_a_match() -> None:
    """On CI there is no DATABASE_URL at all. That must not read as "matches"."""
    assert_not_production("postgresql://u:p@localhost:5432/arie", "")


def test_the_local_compose_stack_and_the_test_database_are_different_databases() -> None:
    """The specific local arrangement the docs prescribe: same Postgres server,
    different database. The Compose workers poll `arie`; the suite uses
    `arie_test`, whose `jobs` table they cannot see — so the stack can stay up
    during a test run."""
    compose = "postgresql://arie:arie_local_dev@localhost:5432/arie"
    tests = "postgresql://arie:arie_local_dev@localhost:5432/arie_test"

    assert _identity(compose) != _identity(tests)
    assert_not_production(tests, compose)


# ---------------------------------------------------------------- marker --


def test_the_marker_table_is_not_created_by_any_migration() -> None:
    """It has to be the one object the normal deploy path never creates.

    A marker in ``migrations/`` would be stamped onto every database the
    migration runner touches — production included — which is precisely
    backwards: the marker's whole job is to be absent there.
    """
    from pathlib import Path

    for migration in sorted(Path("migrations").glob("*.sql")):
        assert MARKER_TABLE not in migration.read_text(encoding="utf-8"), migration.name


def test_the_marker_name_is_unmistakable() -> None:
    assert "test" in MARKER_TABLE
    assert MARKER_TABLE.startswith("arie_")
