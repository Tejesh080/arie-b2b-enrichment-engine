"""Provider connection testing (Productization M4 Part 5) — every tester
exercised entirely against `httpx.MockTransport`, per the module's own
"do not rely on real vendor APIs during normal test suite" contract. No
network access, no real credentials, anywhere in this file.
"""

from __future__ import annotations

from typing import NoReturn

import httpx
import pytest

from arie.provider_testing import ConnectionTestResult, UnknownTesterProviderError
from arie.provider_testing import test_connection as run_connection_test


def _client_returning(status_code: int, *, json: dict[str, object] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_raising(exc: httpx.HTTPError) -> httpx.Client:
    def handler(request: httpx.Request) -> NoReturn:
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_a_200_response_is_success(provider: str) -> None:
    result = run_connection_test(provider, "fake-credential", client=_client_returning(200))
    assert result == ConnectionTestResult(success=True, sanitized_error=None)


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_a_401_is_reported_as_authentication_failed(provider: str) -> None:
    result = run_connection_test(provider, "fake-credential", client=_client_returning(401))
    assert result.success is False
    assert result.sanitized_error == "authentication_failed:401"


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_a_403_is_reported_as_forbidden_not_authentication_failed(provider: str) -> None:
    """Verified against Hunter's docs (Part 12B): 403 on the account-status
    endpoint means rate-limited, not an invalid key — conflating it with
    401 would misreport a throttled-but-valid credential as bad."""
    result = run_connection_test(provider, "fake-credential", client=_client_returning(403))
    assert result.success is False
    assert result.sanitized_error == "forbidden:403"


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_an_unexpected_status_is_reported_sanitized(provider: str) -> None:
    result = run_connection_test(provider, "fake-credential", client=_client_returning(500))
    assert result.success is False
    assert result.sanitized_error == "unexpected_status:500"


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_a_timeout_is_reported_sanitized_without_the_credential(provider: str) -> None:
    result = run_connection_test(
        provider, "super-secret-value", client=_client_raising(httpx.ConnectTimeout("boom"))
    )
    assert result.success is False
    # ConnectTimeout is an httpx.TimeoutException subclass — classified by
    # that shared base, not its own subclass name (see
    # _sanitized_transport_result).
    assert result.sanitized_error == "transport_error:timeout"
    assert "super-secret-value" not in (result.sanitized_error or "")


@pytest.mark.parametrize(
    "provider",
    ["abstract_company_enrichment", "hunter_combined_enrichment", "apollo_person_enrichment"],
)
def test_a_transport_error_never_echoes_the_credential_or_url(provider: str) -> None:
    """The transport-error message is a bare exception-class label — never
    `str(exc)` (which for an `httpx` error can embed the request URL, and
    Abstract sends the credential as a URL query parameter)."""
    exc = httpx.ConnectError("connect failed to https://x?api_key=super-secret-value")
    result = run_connection_test(provider, "super-secret-value", client=_client_raising(exc))
    assert result.sanitized_error == "transport_error:ConnectError"
    assert "super-secret-value" not in (result.sanitized_error or "")


def test_unknown_provider_raises() -> None:
    with pytest.raises(UnknownTesterProviderError):
        run_connection_test("some_other_provider", "x")


def test_apollo_test_request_uses_a_dot_invalid_email() -> None:
    """`.invalid` (RFC 2606) can never resolve to a real person — this is
    what makes the Apollo test call safe to run against a real key without
    risking a real, billable match."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["email"] = dict(request.url.params)["email"]
        return httpx.Response(200, json={"person": None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run_connection_test("apollo_person_enrichment", "x", client=client)

    assert captured["email"].endswith(".invalid")
