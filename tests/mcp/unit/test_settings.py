"""Proves the environment-separation property spec § 13 depends on:
``Settings`` never falls back to ``DATABASE_URL``/``TEST_DATABASE_URL``,
even when both are set and ``MCP_READONLY_DATABASE_URL`` is not.
"""

from __future__ import annotations

import pytest

from arie_mcp.settings import Settings


@pytest.fixture(autouse=True)
def _clear_relevant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MCP_READONLY_DATABASE_URL",
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "ARIE_MCP_API_BASE_URL",
        "ARIE_MCP_AUDIT_LOG_PATH",
        "ARIE_MCP_DB_STATEMENT_TIMEOUT_MS",
        "ARIE_MCP_TOOL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_unset_readonly_url_is_not_configured() -> None:
    settings = Settings()
    assert settings.readonly_database_url == ""
    assert settings.database_configured is False


def test_never_falls_back_to_database_url_or_test_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod-should-never-be-read/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test-should-never-be-read/db")

    settings = Settings()

    assert settings.readonly_database_url == ""
    assert settings.database_configured is False


def test_readonly_url_is_read_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_READONLY_DATABASE_URL", "postgresql://arie_mcp_readonly@host/db")

    settings = Settings()

    assert settings.readonly_database_url == "postgresql://arie_mcp_readonly@host/db"
    assert settings.database_configured is True


def test_api_base_url_defaults_to_localhost() -> None:
    settings = Settings()
    assert settings.api_base_url == "http://localhost:8000"


def test_defaults_are_sane() -> None:
    settings = Settings()
    assert settings.db_statement_timeout_ms == 5000
    assert settings.tool_timeout_seconds == 10.0
    assert settings.audit_log_dir == "var/mcp-audit"
