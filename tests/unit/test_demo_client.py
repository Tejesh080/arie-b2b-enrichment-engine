"""`scripts.demo.client` — bounded HTTP client and polling.

Uses `httpx.MockTransport`, injected through `ArieClient.transport` (added
for exactly this), to fake ARIE's API deterministically — no real network, no
live stack. Polling timeouts here run in milliseconds, not the demo's real
30-90s budgets, so this stays fast.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from scripts.demo.client import ArieClient, DemoApiError, DemoTimeoutError


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ArieClient:
    return ArieClient(transport=httpx.MockTransport(handler), request_timeout_s=2.0)


def _json_response(status_code: int, body: Any) -> httpx.Response:
    return httpx.Response(status_code, json=body)


# --------------------------------------------------------------- basic reads --


def test_health_returns_the_parsed_body() -> None:
    client = _client(
        lambda request: _json_response(
            200, {"status": "ok", "database": True, "schema_ready": True}
        )
    )
    assert client.health() == {"status": "ok", "database": True, "schema_ready": True}


def test_is_healthy_is_true_only_for_status_ok() -> None:
    client = _client(lambda request: _json_response(200, {"status": "degraded"}))
    assert client.is_healthy() is False


def test_is_healthy_is_false_on_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert _client(handler).is_healthy() is False


# ------------------------------------------------------------------ errors --


def test_non_2xx_response_raises_demo_api_error() -> None:
    client = _client(lambda request: httpx.Response(404, text="no lead abc"))
    with pytest.raises(DemoApiError, match="404"):
        client.get_receipt("abc")


def test_malformed_json_raises_demo_api_error() -> None:
    client = _client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(DemoApiError, match="valid JSON"):
        client.get_receipt("abc")


def test_non_object_json_raises_demo_api_error() -> None:
    client = _client(lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(DemoApiError, match="non-object"):
        client.get_receipt("abc")


def test_transport_error_raises_demo_api_error_not_a_bare_httpx_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(DemoApiError):
        _client(handler).get_receipt("abc")


# -------------------------------------------------------------------- writes --


def test_post_lead_sends_the_payload_and_returns_the_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _json_response(201, {"lead_id": "x", "created": True, "job_created": True})

    result = _client(handler).post_lead({"source": "demo", "email": "a@b.com"})

    assert captured["body"] == {"source": "demo", "email": "a@b.com"}
    assert result["lead_id"] == "x"


def test_submit_review_decision_omits_notes_when_not_given() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _json_response(200, {"final_decision": "auto_route"})

    _client(handler).submit_review_decision(
        "r1", action="approve", reviewer="arie-demo", expected_lead_version=1
    )

    assert "notes" not in captured["body"]


def test_submit_review_decision_includes_notes_when_given() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _json_response(200, {"final_decision": "manual_review"})

    _client(handler).submit_review_decision(
        "r1", action="edit", reviewer="arie-demo", expected_lead_version=1, notes="override reason"
    )

    assert captured["body"]["notes"] == "override reason"


# --------------------------------------------------------------- bounded waits --


def test_wait_for_health_returns_once_status_is_ok() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _json_response(200, {"status": "ok" if calls["n"] >= 2 else "degraded"})

    _client(handler).wait_for_health(timeout_s=2.0, poll_interval_s=0.01)
    assert calls["n"] >= 2


def test_wait_for_health_times_out_with_a_clear_message() -> None:
    client = _client(lambda request: _json_response(200, {"status": "degraded"}))
    with pytest.raises(DemoTimeoutError, match="did not become healthy"):
        client.wait_for_health(timeout_s=0.05, poll_interval_s=0.01)


def test_wait_for_health_treats_connection_errors_as_not_yet_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DemoTimeoutError):
        _client(handler).wait_for_health(timeout_s=0.05, poll_interval_s=0.01)


def test_wait_for_decision_returns_once_status_leaves_pending() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        status = "decided" if calls["n"] >= 2 else "pending"
        return _json_response(200, {"lead_id": "x", "status": status, "lead_status": "SCORING"})

    receipt = _client(handler).wait_for_decision("x", timeout_s=2.0, poll_interval_s=0.01)
    assert receipt["status"] == "decided"


def test_wait_for_decision_times_out_with_current_lead_status_in_the_message() -> None:
    client = _client(
        lambda request: _json_response(
            200, {"lead_id": "x", "status": "pending", "lead_status": "SCORING"}
        )
    )
    with pytest.raises(DemoTimeoutError, match="SCORING"):
        client.wait_for_decision("x", timeout_s=0.05, poll_interval_s=0.01)


def test_wait_for_decision_returns_immediately_for_processing_failed() -> None:
    """A dead-lettered lead is a settled outcome, not something worth
    continuing to poll for."""
    client = _client(
        lambda request: _json_response(
            200, {"lead_id": "x", "status": "processing_failed", "lead_status": "DEAD_LETTER"}
        )
    )
    receipt = client.wait_for_decision("x", timeout_s=2.0, poll_interval_s=0.01)
    assert receipt["status"] == "processing_failed"
