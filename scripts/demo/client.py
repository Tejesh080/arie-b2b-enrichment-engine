"""A thin, bounded HTTP client over ARIE's public API — the demo's only way of
talking to a running stack. No Postgres, no Docker internals, no n8n.

Every call has a per-request timeout; every polling loop has a wall-clock
timeout. Nothing here waits forever — a stack that never comes up, or a lead
that never finishes, fails with a clear message instead of hanging.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_REQUEST_TIMEOUT_S = 10.0


class DemoApiError(RuntimeError):
    """A request to ARIE's API failed or returned something the demo can't use."""


class DemoTimeoutError(RuntimeError):
    """A bounded wait (stack health, a lead reaching a decision) ran out."""


class DemoApiClient(Protocol):
    """Exactly what `scripts.demo.scenarios` needs from a client — the
    interface `ArieClient` implements for real and tests fake, so a scenario
    function can be exercised against an in-memory double without touching
    `httpx` at all (see `tests/unit/test_demo_scenarios.py`)."""

    def post_lead(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def wait_for_decision(
        self, lead_id: str, *, timeout_s: float, poll_interval_s: float = ...
    ) -> dict[str, Any]: ...
    def get_receipt(self, lead_id: str) -> dict[str, Any]: ...
    def get_review(self, review_id: str) -> dict[str, Any]: ...
    def submit_review_decision(
        self,
        review_id: str,
        *,
        action: str,
        reviewer: str,
        expected_lead_version: int,
        notes: str | None = ...,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ArieClient:
    base_url: str = DEFAULT_BASE_URL
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    transport: httpx.BaseTransport | None = None
    """Normally `None` (real networking). `tests/unit/test_demo_client.py`
    injects `httpx.MockTransport` to fake ARIE's API deterministically without
    a real socket. (`httpx.ASGITransport` doesn't fit here — it only
    implements the *async* transport interface, and this client is
    deliberately sync; see the module docstring for why. The one integration
    smoke test that needs a genuinely live app runs a real uvicorn server on
    an ephemeral port instead.)"""

    def _get(self, path: str) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.request_timeout_s, transport=self.transport
            ) as client:
                response = client.get(path)
        except httpx.HTTPError as exc:
            raise DemoApiError(f"GET {path} failed: {exc}") from exc
        return self._parse(path, response)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.request_timeout_s, transport=self.transport
            ) as client:
                response = client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise DemoApiError(f"POST {path} failed: {exc}") from exc
        return self._parse(path, response)

    def _parse(self, path: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise DemoApiError(f"{path} returned {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DemoApiError(f"{path} did not return valid JSON: {response.text[:500]}") from exc
        if not isinstance(body, dict):
            raise DemoApiError(f"{path} returned a non-object JSON body: {body!r}")
        return body

    # ------------------------------------------------------------- reads --

    def health(self) -> dict[str, Any]:
        return self._get("/healthz")

    def is_healthy(self) -> bool:
        try:
            return self.health().get("status") == "ok"
        except DemoApiError:
            return False

    def get_receipt(self, lead_id: str) -> dict[str, Any]:
        return self._get(f"/leads/{lead_id}/receipt")

    def get_review(self, review_id: str) -> dict[str, Any]:
        return self._get(f"/reviews/{review_id}")

    # ------------------------------------------------------------ writes --

    def post_lead(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/leads", payload)

    def submit_review_decision(
        self,
        review_id: str,
        *,
        action: str,
        reviewer: str,
        expected_lead_version: int,
        notes: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "action": action,
            "reviewer": reviewer,
            "expected_lead_version": expected_lead_version,
        }
        if notes is not None:
            body["notes"] = notes
        return self._post(f"/reviews/{review_id}/decision", body)

    # -------------------------------------------------------- bounded waits --

    def wait_for_health(self, *, timeout_s: float, poll_interval_s: float = 1.0) -> None:
        """Block until `/healthz` reports `ok`, or raise `DemoTimeoutError`.

        Swallows connection errors while polling — a stack still starting up
        (container not listening yet) is expected, not a failure — but keeps
        counting them against the same wall-clock budget.
        """
        deadline = time.monotonic() + timeout_s
        last_status = "unreachable"
        while time.monotonic() < deadline:
            try:
                body = self.health()
                last_status = str(body.get("status", "unknown"))
                if last_status == "ok":
                    return
            except DemoApiError:
                pass
            time.sleep(poll_interval_s)
        raise DemoTimeoutError(
            f"ARIE did not become healthy within {timeout_s:.0f} seconds "
            f"(last status: {last_status}) at {self.base_url}/healthz."
        )

    def wait_for_decision(
        self, lead_id: str, *, timeout_s: float, poll_interval_s: float = 1.0
    ) -> dict[str, Any]:
        """Block until the lead's receipt leaves `"pending"`, or raise
        `DemoTimeoutError`. Returns the final receipt body either way it settles
        (`"decided"` or `"processing_failed"` — the caller decides what to do
        with a failure; this function's only contract is "stopped waiting")."""
        deadline = time.monotonic() + timeout_s
        receipt: dict[str, Any] = self.get_receipt(lead_id)
        while receipt.get("status") == "pending":
            if time.monotonic() >= deadline:
                raise DemoTimeoutError(
                    f"Lead {lead_id} did not reach a decision within {timeout_s:.0f} "
                    f"seconds. Current status: {receipt.get('lead_status', 'UNKNOWN')}."
                )
            time.sleep(poll_interval_s)
            receipt = self.get_receipt(lead_id)
        return receipt
