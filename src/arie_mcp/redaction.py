"""Defensive redaction for diagnostic free-text (e.g. ``jobs.last_error``).

Applied only to values already read for MCP output — this module never
touches application data in Postgres, only the copy a tool is about to
serialize. Deliberately narrow: four obvious, high-confidence patterns
(email addresses, DB connection URLs, bearer tokens, API-key-shaped
strings), not probabilistic/fuzzy PII detection. An honest, narrow pattern
set that says exactly what it catches beats a fuzzy one that either
over-redacts ordinary diagnostic text or silently over-promises what it
scrubs.

Redact **before** truncating (``limits.truncate_str``), not after — cutting
a string to length first can leave the first half of a secret sitting in
the truncated remainder, past the point where a pattern still matches it
whole.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_DB_URL_RE = re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://\S+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9\-_.=]+", re.IGNORECASE)
_ARIE_API_KEY_RE = re.compile(r"\barie_[A-Za-z0-9_-]{16,}\b")
_GENERIC_API_KEY_RE = re.compile(
    r"\b(?:sk|pk|api[-_]?key|secret|token)[-_][A-Za-z0-9]{8,}\b", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Order matters: a DB URL or bearer token is replaced whole before any
# narrower pattern (like the email regex) could match a fragment inside it.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    _DB_URL_RE,
    _BEARER_RE,
    _ARIE_API_KEY_RE,
    _GENERIC_API_KEY_RE,
    _EMAIL_RE,
)


def redact(text: str | None) -> str | None:
    if text is None:
        return None
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result
