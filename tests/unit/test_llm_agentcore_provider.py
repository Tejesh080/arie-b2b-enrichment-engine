"""`arie.llm.agentcore_provider` — the wire contract with the Decision Copilot Runtime.

No network, no AWS, no Cognito: every path runs through an injected
`httpx.MockTransport`, the same seam `test_llm_deepseek_client.py` uses.

Two properties carry most of the weight here. Messages must cross the wire
**unchanged**, because the Runtime picks which fenced spans to screen by label —
rewriting them on this side would silently move the guardrail boundary. And the
model the Runtime reports must match what this deployment prices, because the
ledger row is written here.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.ledger.pricing import guardrail_cost_usd
from arie.llm.agentcore_provider import AgentCoreProvider
from arie.llm.bedrock_provider import DEFAULT_BEDROCK_MODEL
from arie.llm.provider import (
    LLMGuardrailInterventionError,
    LLMMessage,
    LLMResponseError,
    LLMTransportError,
    LLMUnavailableError,
)
from arie.llm.structured import UntrustedBlock, render_untrusted

RUNTIME_URL = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations"
TOKEN_URL = "https://arie-copilot.auth.us-east-1.amazoncognito.com/oauth2/token"
SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}}


def _config(**overrides: Any) -> IntelligenceConfig:
    base = dataclasses.replace(
        INTELLIGENCE,
        provider="agentcore",
        model=DEFAULT_BEDROCK_MODEL,
        agentcore_runtime_url=RUNTIME_URL,
        agentcore_token_url=TOKEN_URL,
        agentcore_client_id="client-id",
        agentcore_client_secret="client-secret-never-logged",
        agentcore_scope="arie-copilot/invoke",
        max_output_tokens=800,
    )
    return dataclasses.replace(base, **overrides)


def _runtime_ok(**overrides: Any) -> dict[str, Any]:
    body = {
        "outcome": "ok",
        "text": '{"intent": "top_leads"}',
        "parsed": {"intent": "top_leads"},
        "model": DEFAULT_BEDROCK_MODEL,
        "provider": "bedrock",
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 1304,
            "completion_tokens": 21,
            "guardrail_text_units": 3,
        },
        "guardrail": {
            "id": "quwmqu430jgj",
            "version": "1",
            "action": "NONE",
            "applied": True,
            "guarded_labels": ["question"],
            "units": {
                "topic": 1,
                "content": 1,
                "sensitive_information": 1,
                "contextual_grounding": 0,
                "word": 0,
                "total": 3,
            },
        },
        "cost": {"model_usd": "0.00006", "guardrail_usd": "0.00040", "total_usd": "0.00046"},
        "request_id": "req-1",
        "latency_ms": 840.0,
        "bedrock_latency_ms": 559.0,
    }
    body.update(overrides)
    return body


class Recorder:
    def __init__(self) -> None:
        self.runtime_requests: list[dict[str, Any]] = []
        self.token_requests: list[httpx.Request] = []


def _provider(
    runtime_response: httpx.Response | list[httpx.Response],
    *,
    token_response: httpx.Response | None = None,
    **overrides: Any,
) -> tuple[AgentCoreProvider, Recorder]:
    rec = Recorder()
    responses = (
        list(runtime_response) if isinstance(runtime_response, list) else [runtime_response]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            rec.token_requests.append(request)
            return token_response or httpx.Response(
                200, json={"access_token": "tok-abc", "expires_in": 3600}
            )
        rec.runtime_requests.append(json.loads(request.content))
        rec.last_headers = dict(request.headers)  # type: ignore[attr-defined]
        return responses.pop(0) if len(responses) > 1 else responses[0]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AgentCoreProvider(config=_config(**overrides), client=client), rec


# ------------------------------------------------------------- configuration --


def test_missing_configuration_raises_unavailable_not_a_runtime_error() -> None:
    """Raised at construction, so "the feature is off" stays distinguishable
    from "the feature tried and failed"."""
    with pytest.raises(LLMUnavailableError) as excinfo:
        AgentCoreProvider(config=_config(agentcore_runtime_url="", agentcore_client_id=""))
    assert "AGENTCORE_RUNTIME_URL" in str(excinfo.value)
    assert "AGENTCORE_CLIENT_ID" in str(excinfo.value)


def test_provider_identity() -> None:
    provider, _ = _provider(httpx.Response(200, json=_runtime_ok()))
    assert provider.name == "agentcore"
    assert provider.model == DEFAULT_BEDROCK_MODEL


# --------------------------------------------------------------------- auth --


def test_a_token_is_minted_once_and_reused() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    for _ in range(3):
        provider.generate_structured([LLMMessage(role="user", content="d")], json_schema=SCHEMA)
    assert len(rec.token_requests) == 1, "the token should be cached across calls"
    assert len(rec.runtime_requests) == 3


def test_the_token_request_uses_client_credentials_and_the_configured_scope() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    provider.generate_text([LLMMessage(role="user", content="d")])
    body = rec.token_requests[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert "arie-copilot%2Finvoke" in body or "arie-copilot/invoke" in body
    # The secret travels in the Authorization header, never the body.
    assert "client-secret-never-logged" not in body


def test_the_runtime_call_carries_the_bearer_token() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    provider.generate_text([LLMMessage(role="user", content="d")])
    assert rec.last_headers["authorization"] == "Bearer tok-abc"  # type: ignore[attr-defined]


def test_a_rejected_token_degrades_rather_than_raising_something_exotic() -> None:
    provider, _ = _provider(httpx.Response(403, json={"message": "forbidden"}))
    with pytest.raises(LLMTransportError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content="d")])
    assert "token" in str(excinfo.value).lower()


def test_a_token_endpoint_failure_never_echoes_the_secret() -> None:
    provider, _ = _provider(
        httpx.Response(200, json=_runtime_ok()),
        token_response=httpx.Response(500, text="upstream exploded"),
    )
    with pytest.raises(LLMTransportError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content="d")])
    assert "client-secret-never-logged" not in str(excinfo.value)


# ---------------------------------------------------------------- the wire --


def test_messages_cross_the_wire_unchanged() -> None:
    """The Runtime selects guarded spans by fence label. Rewriting messages on
    this side would silently move the guardrail boundary."""
    rendered, _ = render_untrusted(
        [
            UntrustedBlock(label="question", text="who should I call?"),
            UntrustedBlock(label="leads", text="Dana Whitfield dana@example.invalid"),
        ],
        max_chars=5000,
    )
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    provider.generate_structured(
        [LLMMessage(role="system", content="rule"), LLMMessage(role="user", content=rendered)],
        json_schema=SCHEMA,
    )
    sent = rec.runtime_requests[0]
    assert sent["messages"] == [
        {"role": "system", "content": "rule"},
        {"role": "user", "content": rendered},
    ]
    assert "<<<UNTRUSTED_DATA name=question>>>" in sent["messages"][1]["content"]


def test_schema_and_ceilings_are_forwarded() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    provider.generate_structured(
        [LLMMessage(role="user", content="d")], json_schema=SCHEMA, max_output_tokens=800
    )
    sent = rec.runtime_requests[0]
    assert sent["json_schema"] == SCHEMA
    assert sent["max_output_tokens"] == 800
    assert sent["temperature"] == 0.0


def test_generate_text_sends_no_schema() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    provider.generate_text([LLMMessage(role="user", content="d")])
    assert "json_schema" not in rec.runtime_requests[0]


def test_empty_message_list_is_rejected_before_any_call() -> None:
    provider, rec = _provider(httpx.Response(200, json=_runtime_ok()))
    with pytest.raises(LLMResponseError):
        provider.generate_text([])
    assert rec.runtime_requests == []


# ------------------------------------------------------------------ results --


def test_a_successful_call_maps_to_a_completion() -> None:
    provider, _ = _provider(httpx.Response(200, json=_runtime_ok()))
    completion = provider.generate_structured(
        [LLMMessage(role="user", content="d")], json_schema=SCHEMA
    )
    assert completion.text == '{"intent": "top_leads"}'
    assert completion.provider == "agentcore"
    assert completion.model == DEFAULT_BEDROCK_MODEL
    assert completion.usage.prompt_tokens == 1304
    assert completion.usage.completion_tokens == 21
    assert completion.usage.guardrail_text_units == 3


def test_guardrail_cost_is_repriced_locally_not_taken_from_the_runtime() -> None:
    """The Runtime reports units; the money comes from this deployment's own
    price table, so a Runtime cannot write a figure into ARIE's ledger that
    ARIE's own pricing does not agree with."""
    body = _runtime_ok()
    body["cost"]["guardrail_usd"] = "999.99"  # a Runtime claiming nonsense
    provider, _ = _provider(httpx.Response(200, json=body))
    completion = provider.generate_structured(
        [LLMMessage(role="user", content="d")], json_schema=SCHEMA
    )
    assert completion.usage.guardrail_cost_usd == guardrail_cost_usd(
        topic_units=1, content_units=1, sensitive_information_units=1
    )
    assert completion.usage.guardrail_cost_usd < Decimal("1")


def test_a_guardrail_block_raises_its_own_type_with_billed_usage() -> None:
    provider, _ = _provider(
        httpx.Response(
            200,
            json={
                "outcome": "guardrail_intervened",
                "error": "blocked",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "guardrail_text_units": 3},
                "guardrail": {
                    "action": "GUARDRAIL_INTERVENED",
                    "units": {
                        "topic": 1,
                        "content": 1,
                        "sensitive_information": 1,
                        "contextual_grounding": 0,
                        "word": 0,
                        "total": 3,
                    },
                },
                "request_id": "req-blocked",
            },
        )
    )
    with pytest.raises(LLMGuardrailInterventionError) as excinfo:
        provider.generate_structured([LLMMessage(role="user", content="d")], json_schema=SCHEMA)
    usage = excinfo.value.usage
    assert usage is not None
    assert usage.guardrail_text_units == 3
    assert usage.guardrail_cost_usd > Decimal(0)
    assert usage.prompt_tokens == 0


def test_a_model_mismatch_fails_rather_than_mispricing_the_ledger() -> None:
    provider, _ = _provider(
        httpx.Response(200, json=_runtime_ok(model="amazon.nova-lite-v1:0"))
    )
    with pytest.raises(LLMResponseError) as excinfo:
        provider.generate_structured([LLMMessage(role="user", content="d")], json_schema=SCHEMA)
    assert "nova-lite" in str(excinfo.value)
    assert "priced for" in str(excinfo.value)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_transport_errors(status: int) -> None:
    provider, _ = _provider(httpx.Response(status, text="boom"))
    with pytest.raises(LLMTransportError):
        provider.generate_text([LLMMessage(role="user", content="d")])


def test_a_non_json_body_is_a_response_error() -> None:
    provider, _ = _provider(httpx.Response(200, text="not json"))
    with pytest.raises(LLMResponseError):
        provider.generate_text([LLMMessage(role="user", content="d")])


def test_an_unknown_outcome_is_a_response_error() -> None:
    provider, _ = _provider(httpx.Response(200, json={"outcome": "weird", "error": "?"}))
    with pytest.raises(LLMResponseError):
        provider.generate_text([LLMMessage(role="user", content="d")])


def test_estimate_cost_includes_the_guardrail_component() -> None:
    provider, _ = _provider(httpx.Response(200, json=_runtime_ok()))
    completion = provider.generate_structured(
        [LLMMessage(role="user", content="d")], json_schema=SCHEMA
    )
    assert provider.estimate_cost(completion.usage) > completion.usage.guardrail_cost_usd
