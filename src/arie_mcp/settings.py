"""Environment configuration for the MCP server.

Mirrors ``arie.config``'s own shape deliberately: ``default_factory`` on
every field so a test that patches the environment before construction is
honoured, and no field here ever falls back to another module's variable —
``MCP_READONLY_DATABASE_URL`` is its own name, never ``DATABASE_URL`` or
``TEST_DATABASE_URL``, so an MCP server started with no environment
configured simply has no database to talk to rather than silently reaching
a deployment (spec § 13, "Environment separation").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a valid integer") from exc


@dataclass(frozen=True)
class Settings:
    readonly_database_url: str = field(
        default_factory=lambda: os.getenv("MCP_READONLY_DATABASE_URL", "")
    )
    """Connects as ``arie_mcp_readonly``. Never falls back to
    ``DATABASE_URL``/``TEST_DATABASE_URL`` — unset means every DB-backed
    tool reports ``DB_UNAVAILABLE`` cleanly; the server still starts."""

    api_base_url: str = field(
        default_factory=lambda: os.getenv("ARIE_MCP_API_BASE_URL", "http://localhost:8000")
    )
    """Only ``inspect_api_contract`` uses this. Defaults to the local dev
    API, never inferred to be a production URL (spec Decision 5)."""

    audit_log_dir: str = field(
        default_factory=lambda: os.getenv("ARIE_MCP_AUDIT_LOG_PATH", "var/mcp-audit")
    )
    """A directory, not a file — one JSONL file per UTC day is written
    inside it. Host-local only in V0.1: no DB audit-write credential, no
    Docker volume requirement (spec Decision 6)."""

    db_statement_timeout_ms: int = field(
        default_factory=lambda: _env_int("ARIE_MCP_DB_STATEMENT_TIMEOUT_MS", 5000)
    )
    tool_timeout_seconds: float = field(
        default_factory=lambda: float(_env_int("ARIE_MCP_TOOL_TIMEOUT_SECONDS", 10))
    )

    # --- Remote (Streamable HTTP) transport only. Unused by the stdio
    # entrypoint (arie_mcp.server) -- reading an unset value there is
    # harmless, but nothing in that path ever does.
    http_host: str = field(default_factory=lambda: os.getenv("ARIE_MCP_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    """Railway injects ``PORT``; the same variable every other service in
    this repo already reads (see the Dockerfile's own CMD)."""

    public_url: str = field(default_factory=lambda: os.getenv("ARIE_MCP_PUBLIC_URL", ""))
    """This service's own public HTTPS origin (e.g.
    ``https://arie-mcp-production.up.railway.app``), no trailing slash.
    Doubles as the OAuth issuer and the MCP resource URL -- both must be
    the URL a client actually reaches this server at, never inferred from
    ``http_host``/``http_port``, which are the local bind address."""

    owner_password: str = field(default_factory=lambda: os.getenv("MCP_OWNER_PASSWORD", ""))
    """Gates the ``/login`` page the OAuth authorization flow redirects to.
    One operator, one password -- there is no username, and no default:
    an unset value means the remote server refuses to start (see
    ``http_server.py``) rather than serving a diagnostic interface with a
    login step nothing actually protects."""

    @property
    def database_configured(self) -> bool:
        return bool(self.readonly_database_url)

    @property
    def remote_auth_configured(self) -> bool:
        return bool(self.public_url and self.owner_password)


def get_settings() -> Settings:
    """Constructed fresh, not a module-level singleton — same reasoning
    ``tests/integration/conftest.py``'s ``_test_database_config`` gives:
    a caller that patches the environment before this runs must be
    honoured, not shadowed by an import-time snapshot."""
    return Settings()
