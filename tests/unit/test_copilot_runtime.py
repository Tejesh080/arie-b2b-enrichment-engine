"""`services/copilot-runtime/app.py` — the container's HTTP contract.

Offline: the Bedrock client is stubbed, so no AWS account, no credentials and
no network are involved.

The database-free assertion is the important one. "This container cannot reach
tenant data" is the claim the whole boundary rests on, and it is only true while
nothing in its import graph pulls in a driver — which a well-meaning import
three modules away could change without anyone noticing.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_RUNTIME_DIR = Path(__file__).resolve().parents[2] / "services" / "copilot-runtime"
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import app as runtime_app  # noqa: E402

SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}}
FENCED = (
    "<<<UNTRUSTED_DATA name=question>>>\nwho should I call?\n"
    "<<<END_UNTRUSTED_DATA name=question>>>"
)


class StubClient:
    """Enough of a `bedrock-runtime` client for the app's two call paths."""

    def __init__(self, *, blocked: bool = False, raises: Exception | None = None) -> None:
        self.blocked = blocked
        self.raises = raises
        self.converse_calls: list[dict[str, Any]] = []
        self.screen_calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.screen_calls.append(kwargs)
        return {
            "action": "GUARDRAIL_INTERVENED" if self.blocked else "NONE",
            "usage": {
                "topicPolicyUnits": 1,
                "contentPolicyUnits": 1,
                "sensitiveInformationPolicyUnits": 1,
                "wordPolicyUnits": 0,
            },
            "ResponseMetadata": {"RequestId": "req-screen"},
        }

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.converse_calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": '{"intent": "top_leads"}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 120, "outputTokens": 11},
            "ResponseMetadata": {"RequestId": "req-converse"},
        }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # `IntelligenceConfig` is frozen, so the module binding is replaced rather
    # than its fields mutated — the same thing a real deployment does by
    # setting the environment before import.
    monkeypatch.setattr(
        runtime_app,
        "INTELLIGENCE",
        dataclasses.replace(
            runtime_app.INTELLIGENCE,
            provider="bedrock",
            model="amazon.nova-micro-v1:0",
            bedrock_guardrail_id="quwmqu430jgj",
            bedrock_guardrail_version="1",
            bedrock_guarded_labels=("question",),
        ),
    )
    return TestClient(runtime_app.app)


def _use(monkeypatch: pytest.MonkeyPatch, stub: StubClient) -> None:
    monkeypatch.setattr(runtime_app, "_bedrock_client", lambda: stub)


def _post(client: TestClient, **overrides: Any) -> Any:
    body = {
        "messages": [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": FENCED},
        ],
        "json_schema": SCHEMA,
        "max_output_tokens": 100,
    }
    body.update(overrides)
    return client.post("/invocations", json=body)


# ------------------------------------------------------------------ contract --


def test_ping_reports_healthy(client: TestClient) -> None:
    """AgentCore's health probe. A container failing this never receives traffic."""
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_a_safe_invocation_returns_text_usage_guardrail_and_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, StubClient())
    body = _post(client).json()

    assert body["outcome"] == "ok"
    assert body["text"] == '{"intent": "top_leads"}'
    assert body["parsed"] == {"intent": "top_leads"}
    assert body["model"] == "amazon.nova-micro-v1:0"
    assert body["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 11,
        "guardrail_text_units": 3,
    }
    assert body["guardrail"]["action"] == "NONE"
    assert body["guardrail"]["applied"] is True
    assert body["guardrail"]["version"] == "1"
    assert body["request_id"] == "req-converse"
    # Money as strings, never JSON floats — `arie.ledger.pricing` is Decimal
    # end to end and these are summed into `model_calls` on the caller.
    assert isinstance(body["cost"]["total_usd"], str)


def test_only_the_question_is_screened_never_the_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubClient()
    _use(monkeypatch, stub)
    evidence = (
        "<<<UNTRUSTED_DATA name=leads>>>\nDana Whitfield dana@example.invalid\n"
        "<<<END_UNTRUSTED_DATA name=leads>>>"
    )
    _post(
        client,
        messages=[
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": FENCED + "\n\n" + evidence},
        ],
    )
    screened = stub.screen_calls[0]["content"][0]["text"]["text"]
    assert screened == "who should I call?"
    assert "Dana Whitfield" not in screened
    # ...and the evidence still reaches the model, unscreened.
    sent = "".join(b["text"] for b in stub.converse_calls[0]["messages"][0]["content"])
    assert "Dana Whitfield" in sent


def test_no_inline_guardrail_config_is_sent_to_converse(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline guarding would assess the model's output too, and the PII policy
    would anonymise the contact details Ask ARIE answers are made of."""
    stub = StubClient()
    _use(monkeypatch, stub)
    _post(client)
    assert "guardrailConfig" not in stub.converse_calls[0]


def test_a_blocked_question_returns_200_with_billable_guardrail_usage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200, not 4xx: AWS charged for the assessment that blocked it, and the
    caller has to be able to ledger that. A bare 4xx discards the spend."""
    stub = StubClient(blocked=True)
    _use(monkeypatch, stub)
    response = _post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "guardrail_intervened"
    assert body["guardrail"]["action"] == "GUARDRAIL_INTERVENED"
    assert body["usage"]["guardrail_text_units"] == 3
    assert body["cost"]["model_usd"] == "0"
    assert body["cost"]["guardrail_usd"] != "0"
    # Blocked before the model ran, so no completion was paid for.
    assert stub.converse_calls == []


def test_a_blocked_call_reports_the_labels_it_screened(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: blocked calls reported `guarded_labels: []`, so the response
    could not show whether the guardrail saw the question or the evidence —
    on exactly the calls where that matters most."""
    _use(monkeypatch, StubClient(blocked=True))
    body = _post(client).json()
    assert body["outcome"] == "guardrail_intervened"
    assert body["guardrail"]["guarded_labels"] == ["question"]
    assert body["guardrail"]["applied"] is True


def test_a_blocked_call_reports_its_own_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the screen used to raise before recording detail, so a block
    reported the *previous* call's request id — a real-looking id that traces
    to an unrelated request."""
    _use(monkeypatch, StubClient())
    first = _post(client).json()
    _use(monkeypatch, StubClient(blocked=True))
    blocked = _post(client).json()

    assert first["request_id"] == "req-converse"
    assert blocked["request_id"] == "req-screen"
    assert blocked["request_id"] != first["request_id"]


def test_a_transport_failure_answers_502_so_the_caller_degrades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, StubClient(raises=RuntimeError("EndpointConnectionError")))
    response = _post(client)
    assert response.status_code == 502
    assert response.json()["outcome"] == "transport_error"


def test_the_blocked_message_text_is_never_returned_as_a_completion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, StubClient(blocked=True))
    body = _post(client).json()
    assert "text" not in body


@pytest.mark.parametrize(
    "bad",
    [
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x"}], "max_output_tokens": 0},
        {"messages": [{"role": "user", "content": "x"}], "unexpected": 1},
    ],
)
def test_malformed_payloads_are_rejected(client: TestClient, bad: dict[str, Any]) -> None:
    """`extra="forbid"` and the role literal are load-bearing: no `assistant`
    role means a caller cannot build a transcript, which is the agentic shape
    ARIE's LLM layer exists to exclude."""
    assert client.post("/invocations", json=bad).status_code == 422


# -------------------------------------------------------------- the boundary --


def test_runtime_imports_no_database_driver() -> None:
    """The claim the whole architecture rests on: this container cannot reach
    tenant data, because nothing in its import graph can open a connection.

    Checked in a **subprocess** that imports only the runtime. Inspecting this
    process's `sys.modules` would be meaningless — the rest of the suite has
    already imported psycopg, so the assertion would fail for a reason that
    says nothing about what the container ships.
    """
    probe = (
        "import sys, json; "
        f"sys.path.insert(0, {str(_RUNTIME_DIR)!r}); "
        "import app; "
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    forbidden = {"psycopg", "psycopg_pool", "sqlalchemy", "asyncpg", "pandas", "sklearn"}
    leaked = forbidden & loaded
    assert not leaked, f"the runtime's import graph reaches {sorted(leaked)}"


def test_runtime_exposes_only_ping_and_invocations() -> None:
    """A wider surface is a wider thing to secure. AgentCore needs exactly two."""
    paths = {r.path for r in runtime_app.app.routes if hasattr(r, "path")}
    assert {"/ping", "/invocations"} <= paths
    assert not {p for p in paths if p.startswith("/") and "openapi" not in p and "docs" not in p} - {
        "/ping",
        "/invocations",
        "/redoc",
    }
