from __future__ import annotations

from arie_mcp.redaction import REDACTED, redact


def _redact(text: str) -> str:
    result = redact(text)
    assert result is not None
    return result


def test_redacts_email_address() -> None:
    result = _redact("lookup failed for jordan.ellis@example-corp.test")
    assert "jordan.ellis@example-corp.test" not in result
    assert REDACTED in result


def test_redacts_postgres_connection_url() -> None:
    result = _redact("connection failed: postgresql://user:hunter2@db.internal:5432/prod")
    assert "hunter2" not in result
    assert "db.internal" not in result
    assert REDACTED in result


def test_redacts_bearer_token() -> None:
    result = _redact("upstream returned 401 for Authorization: Bearer sk-live-abc123DEF456")
    assert "sk-live-abc123DEF456" not in result
    assert REDACTED in result


def test_redacts_arie_api_key_shape() -> None:
    result = _redact("rejected key arie_9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c")
    assert "arie_9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c" not in result
    assert REDACTED in result


def test_redacts_generic_api_key_shape() -> None:
    result = _redact("provider rejected api_key-1234567890abcdef")
    assert "1234567890abcdef" not in result
    assert REDACTED in result


def test_passes_through_ordinary_text_unchanged() -> None:
    text = "compute_score expects a NEW lead; lead 04ad9852 is AWAITING_HUMAN"
    assert _redact(text) == text


def test_passes_through_none() -> None:
    assert redact(None) is None


def test_redacts_multiple_occurrences_in_one_string() -> None:
    result = _redact("cc: alice@example.com and bob@example.com both notified")
    assert "alice@example.com" not in result
    assert "bob@example.com" not in result
    assert result.count(REDACTED) == 2
