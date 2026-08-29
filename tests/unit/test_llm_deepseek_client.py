"""DeepSeek client — every test here is hermetic: `httpx.MockTransport` stands
in for the network, so nothing here needs `DEEPSEEK_API_KEY`, network access,
or real money, and every test is deterministic. Live behaviour against the
real API is exercised only by `bench/llm_signal_eval.py`, which is optional
and never required — see that module's docstring.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from arie.config import LLMConfig
from arie.llm.deepseek import (
    DeepSeekConfigurationError,
    DeepSeekSignalExtractor,
    record_extraction_cost,
)

_VALID_BODY = json.dumps(
    {
        "has_buying_intent": True,
        "trigger_event_category": "funding_event",
        "trigger_event_detail": "raised a round",
        "disqualifying_signal": False,
        "confidence": 0.9,
        "rationale": "mentions a funding round",
    }
)


def _completion(
    content: str, *, prompt_tokens: int = 120, completion_tokens: int = 40
) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _config(**overrides: object) -> LLMConfig:
    defaults: dict[str, object] = {
        "deepseek_api_key": "test-key",
        "deepseek_base_url": "https://fake.test",
        "model": "deepseek-chat",
        "timeout_seconds": 5.0,
        "max_attempts": 3,
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)  # type: ignore[arg-type]


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(handler))


def _queued_responses(*responses: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return remaining.pop(0)

    return handler


class _RequestCapture:
    """Records every request body the mock transport saw, for asserting on
    what was actually sent (temperature, response_format, absence of tools)."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.bodies: list[dict[str, object]] = []
        self._responses = responses

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self._responses.pop(0)


# --------------------------------------------------------------- success --


def test_successful_extraction_on_first_attempt(spans: InMemorySpanExporter) -> None:
    client = _client_with(_queued_responses(httpx.Response(200, json=_completion(_VALID_BODY))))
    extractor = DeepSeekSignalExtractor(config=_config(), client=client)

    outcome = extractor.extract_signal("We just closed our Series B.")

    assert outcome.succeeded is True
    assert outcome.signal is not None
    assert outcome.signal.has_buying_intent is True
    assert outcome.signal.trigger_event_category == "funding_event"
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].request_succeeded is True
    assert outcome.attempts[0].validation_error is None
    assert outcome.total_prompt_tokens == 120
    assert outcome.total_completion_tokens == 40


def test_request_body_uses_json_mode_zero_temperature_and_no_tools(
    spans: InMemorySpanExporter,
) -> None:
    """Structural guarantee, not a convention: there is no `tools`/`functions`
    key in the request at all, so the model has nothing it could invoke even
    if it tried."""
    capture = _RequestCapture([httpx.Response(200, json=_completion(_VALID_BODY))])
    client = httpx.Client(base_url="https://fake.test", transport=httpx.MockTransport(capture))
    extractor = DeepSeekSignalExtractor(config=_config(), client=client)

    extractor.extract_signal("some lead text")

    (body,) = capture.bodies
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.0
    assert "tools" not in body
    assert "functions" not in body
    assert body["model"] == "deepseek-chat"


# --------------------------------------------------------------- retries --


def test_retries_after_a_schema_validation_failure_then_succeeds(
    spans: InMemorySpanExporter,
) -> None:
    invalid = _completion(json.dumps({"has_buying_intent": "not-a-bool"}))
    valid = _completion(_VALID_BODY)
    client = _client_with(
        _queued_responses(httpx.Response(200, json=invalid), httpx.Response(200, json=valid))
    )
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=3), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.succeeded is True
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].request_succeeded is True
    assert outcome.attempts[0].validation_error is not None
    assert outcome.attempts[1].validation_error is None


def test_a_billable_but_invalid_response_is_still_marked_billable(
    spans: InMemorySpanExporter,
) -> None:
    """The distinction record_extraction_cost depends on: DeepSeek returned
    and charged for a completion, even though it didn't match the schema."""
    client = _client_with(
        _queued_responses(httpx.Response(200, json=_completion('{"unexpected": true}')))
    )
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=1), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.succeeded is False
    assert outcome.attempts[0].request_succeeded is True
    assert outcome.attempts[0].prompt_tokens == 120
    assert outcome.attempts[0].completion_tokens == 40


def test_extra_fields_are_rejected_not_silently_accepted(spans: InMemorySpanExporter) -> None:
    payload = json.loads(_VALID_BODY)
    payload["extra_field_the_model_made_up"] = "auto_route_this_lead"
    client = _client_with(
        _queued_responses(httpx.Response(200, json=_completion(json.dumps(payload))))
    )
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=1), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.succeeded is False
    assert outcome.attempts[0].validation_error is not None


def test_all_attempts_exhausted_returns_no_signal_but_does_not_raise(
    spans: InMemorySpanExporter,
) -> None:
    bad = _completion("not json at all")
    client = _client_with(_queued_responses(*[httpx.Response(200, json=bad) for _ in range(3)]))
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=3), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.succeeded is False
    assert outcome.signal is None
    assert len(outcome.attempts) == 3
    assert all(a.request_succeeded for a in outcome.attempts)


# ------------------------------------------------------------ failures --


def test_http_error_status_is_not_billed(spans: InMemorySpanExporter) -> None:
    client = _client_with(
        _queued_responses(httpx.Response(500, json={"error": "internal server error"}))
    )
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=1), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.succeeded is False
    assert outcome.attempts[0].request_succeeded is False
    assert outcome.attempts[0].prompt_tokens == 0
    assert outcome.attempts[0].completion_tokens == 0
    assert "request failed" in (outcome.attempts[0].validation_error or "")


def test_network_error_is_not_billed(spans: InMemorySpanExporter) -> None:
    def raising_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_with(raising_handler)
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=1), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.attempts[0].request_succeeded is False
    assert outcome.total_prompt_tokens == 0


def test_non_json_response_body_is_not_billed(spans: InMemorySpanExporter) -> None:
    client = _client_with(_queued_responses(httpx.Response(200, content=b"<html>not json</html>")))
    extractor = DeepSeekSignalExtractor(config=_config(max_attempts=1), client=client)

    outcome = extractor.extract_signal("text")

    assert outcome.attempts[0].request_succeeded is False


def test_missing_api_key_and_no_injected_client_fails_at_construction() -> None:
    """Failing at construction, not at call time, is what lets a caller (e.g.
    bench/llm_signal_eval.py) distinguish "the feature is off" from "the
    feature tried and failed"."""
    with pytest.raises(DeepSeekConfigurationError, match="DEEPSEEK_API_KEY"):
        DeepSeekSignalExtractor(config=_config(deepseek_api_key=""))


# ------------------------------------------------------------- tracing --


def test_successful_call_produces_a_span_with_attributes(spans: InMemorySpanExporter) -> None:
    client = _client_with(_queued_responses(httpx.Response(200, json=_completion(_VALID_BODY))))
    DeepSeekSignalExtractor(config=_config(), client=client).extract_signal("text")

    (span,) = [s for s in spans.get_finished_spans() if s.name == "llm.extract_signal"]
    attributes = dict(span.attributes or {})
    assert attributes["arie.llm.model"] == "deepseek-chat"
    assert attributes["arie.llm.attempts"] == 1
    assert attributes["arie.llm.succeeded"] is True
    assert attributes["arie.llm.total_prompt_tokens"] == 120
    assert span.status.status_code is not StatusCode.ERROR


def test_exhausted_retries_marks_the_span_error(spans: InMemorySpanExporter) -> None:
    """A failure that doesn't raise is still a failure — the same reasoning
    arie.jobs.worker applies to a job that exhausts its retries."""
    client = _client_with(_queued_responses(*[httpx.Response(500) for _ in range(3)]))
    DeepSeekSignalExtractor(config=_config(max_attempts=3), client=client).extract_signal("text")

    (span,) = [s for s in spans.get_finished_spans() if s.name == "llm.extract_signal"]
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes or {})["arie.llm.succeeded"] is False


# ------------------------------------------------------------- ledger --


class _FakeLedger:
    """A minimal stand-in matching `PostgresCostLedger.record_model_call`'s
    signature, so the "only bill request_succeeded attempts" logic in
    `record_extraction_cost` gets unit-level coverage with no database. The
    real ledger's own behaviour (idempotency, `v_lead_cost` rollup) is tested
    against a live database in `tests/integration/test_llm_ledger_integration.py`.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_model_call(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return f"call-{len(self.calls)}"


def test_record_extraction_cost_only_bills_request_succeeded_attempts() -> None:
    from arie.llm.deepseek import ExtractionAttempt, ExtractionOutcome

    outcome = ExtractionOutcome(
        model="deepseek-chat",
        signal=None,
        attempts=(
            ExtractionAttempt(
                request_succeeded=False,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=5.0,
                validation_error="request failed: timeout",
            ),
            ExtractionAttempt(
                request_succeeded=True,
                prompt_tokens=100,
                completion_tokens=30,
                latency_ms=400.0,
                validation_error="schema mismatch",
            ),
        ),
    )
    ledger = _FakeLedger()

    writes = record_extraction_cost(
        ledger,  # type: ignore[arg-type]
        outcome,
        organization_id=uuid4(),
        idempotency_key_base="lead:abc:extract",
    )

    assert len(writes) == 1, "the network-failed attempt must not be billed"
    (call,) = ledger.calls
    assert call["prompt_tokens"] == 100
    assert call["completion_tokens"] == 30
    assert call["idempotency_key"] == "lead:abc:extract:attempt1"
