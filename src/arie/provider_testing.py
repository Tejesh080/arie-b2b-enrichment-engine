"""Provider connection testing (Productization M4 Part 5). One tester per
`arie.provider_configs.SUPPORTED_PROVIDERS` entry, each making the cheapest
safe request that provider's own vendor API supports — reusing each real
adapter's own httpx-client-injection pattern (`client: httpx.Client | None`)
so tests never need real network access or real credentials, exactly like
`arie.providers.live_abstract`/`live_hunter`/`live_apollo` already do for
their own enrichment calls.

**Best-effort choice of "cheapest endpoint" per vendor — flag for human
verification before this runs against a real credential for the first
time**, since nothing in this environment can make a real vendor call to
confirm:

* **Hunter** — `GET https://api.hunter.io/v2/account?api_key=...`, Hunter's
  own documented account-status endpoint, not the combined-enrichment
  endpoint `arie.providers.live_hunter` uses for real work. Chosen because
  Hunter's public docs describe this endpoint as free (no search credit
  consumed) — not independently verified against a live call from this
  session, so confirm against Hunter's current documentation before relying
  on it in production.
* **Apollo** — the same person-match endpoint `arie.providers.live_apollo`
  uses for real enrichment, called with a deliberately synthetic,
  never-matching identifier (`_APOLLO_TEST_EMAIL`, a `.invalid` address).
  `ApolloPersonConfig`'s own docstring states a match that finds nothing
  costs 0 credits, and a bad key 401s before any match is attempted — so
  this distinguishes "valid key" from "invalid key" while spending nothing
  on a real key. No cheaper dedicated endpoint is documented anywhere in
  this codebase's own Apollo integration.
* **Abstract** — the same, only, Company Enrichment endpoint
  `arie.providers.live_abstract` uses for real work, called with a fixed,
  stable test domain. Abstract's Company Enrichment product has no separate
  account/auth-check endpoint documented anywhere this codebase references
  — this is the cheapest call available, and unlike Hunter/Apollo it is a
  real, billable lookup. If that turns out to be unacceptable, the fix is
  rate-limiting how often one organization may test, not a different
  endpoint (none exists to switch to).

**Sanitized errors, never a raw provider response, never the credential.**
Every tester returns a :class:`ConnectionTestResult` carrying only
`success`/`sanitized_error` — never `exc.args`, never a response body,
never a request URL (which carries `api_key=...` for Abstract specifically)
— matching the "never interpolate `str(exc)` or `exc.request.url`" rule
`arie.providers.live_abstract.fetch` already follows for exactly this
reason. Bounded timeout (`_TEST_TIMEOUT_SECONDS`), one attempt, no retries.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

__all__ = [
    "ConnectionTestResult",
    "UnknownTesterProviderError",
    "test_connection",
]

_TEST_TIMEOUT_SECONDS = 10.0

_APOLLO_TEST_EMAIL = "arie-connection-test@arie-byok-probe.invalid"
"""A `.invalid` address per RFC 2606 — guaranteed never to resolve to a real
person, so this can never accidentally return (and bill for) a real match."""


class UnknownTesterProviderError(ValueError):
    """`provider` has no tester defined — should be unreachable in practice
    since callers validate against `arie.provider_configs.SUPPORTED_PROVIDERS`
    first, kept as a real error rather than an assertion so a provider added
    to that tuple without a matching tester here fails loudly instead of
    silently reporting every test as a mysterious failure."""


@dataclass(frozen=True)
class ConnectionTestResult:
    success: bool
    sanitized_error: str | None
    """`None` iff `success` — otherwise a short, safe-to-store,
    safe-to-display classification (`"authentication_failed:401"`,
    `"transport_error:TimeoutException"`, ...), never raw provider output."""


def _sanitized_transport_result(exc: httpx.HTTPError) -> ConnectionTestResult:
    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
    return ConnectionTestResult(success=False, sanitized_error=f"transport_error:{kind}")


def _sanitized_status_result(status_code: int) -> ConnectionTestResult:
    if status_code == httpx.codes.OK:
        return ConnectionTestResult(success=True, sanitized_error=None)
    if status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return ConnectionTestResult(
            success=False, sanitized_error=f"authentication_failed:{status_code}"
        )
    return ConnectionTestResult(success=False, sanitized_error=f"unexpected_status:{status_code}")


def _test_abstract(
    raw_credential: str, *, client: httpx.Client | None = None
) -> ConnectionTestResult:
    owns_client = client is None
    resolved = client or httpx.Client(timeout=_TEST_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = resolved.get(
            "https://companyenrichment.abstractapi.com/v2/",
            params={"api_key": raw_credential, "domain": "example.com"},
        )
    except httpx.HTTPError as exc:
        return _sanitized_transport_result(exc)
    finally:
        if owns_client:
            resolved.close()
    return _sanitized_status_result(response.status_code)


def _test_hunter(
    raw_credential: str, *, client: httpx.Client | None = None
) -> ConnectionTestResult:
    owns_client = client is None
    resolved = client or httpx.Client(timeout=_TEST_TIMEOUT_SECONDS)
    try:
        response = resolved.get(
            "https://api.hunter.io/v2/account", headers={"X-API-KEY": raw_credential}
        )
    except httpx.HTTPError as exc:
        return _sanitized_transport_result(exc)
    finally:
        if owns_client:
            resolved.close()
    return _sanitized_status_result(response.status_code)


def _test_apollo(
    raw_credential: str, *, client: httpx.Client | None = None
) -> ConnectionTestResult:
    owns_client = client is None
    resolved = client or httpx.Client(timeout=_TEST_TIMEOUT_SECONDS)
    try:
        response = resolved.post(
            "https://api.apollo.io/api/v1/people/match",
            headers={"x-api-key": raw_credential},
            params={
                "email": _APOLLO_TEST_EMAIL,
                "reveal_personal_emails": "false",
                "reveal_phone_number": "false",
            },
        )
    except httpx.HTTPError as exc:
        return _sanitized_transport_result(exc)
    finally:
        if owns_client:
            resolved.close()
    return _sanitized_status_result(response.status_code)


_TESTERS = {
    "abstract_company_enrichment": _test_abstract,
    "hunter_combined_enrichment": _test_hunter,
    "apollo_person_enrichment": _test_apollo,
}


def test_connection(
    provider: str, raw_credential: str, *, client: httpx.Client | None = None
) -> ConnectionTestResult:
    """Run `provider`'s tester against `raw_credential`. `client` is test-only
    injection (an `httpx.MockTransport`-backed client) — production callers
    never pass it, matching every real adapter's own `.build()` convention.
    """
    tester = _TESTERS.get(provider)
    if tester is None:
        raise UnknownTesterProviderError(f"no connection tester for provider {provider!r}")
    return tester(raw_credential, client=client)
