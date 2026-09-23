"""`arie.llm.structured.strip_code_fence` — unwrapping a markdown code fence.

Why this exists at all, recorded because the number is the argument: on the
frozen Ask ARIE benchmark `google.gemma-3-4b-it` wrapped **every** response in
a ```json fence despite being told not to. The JSON inside was well formed and
the intent was usually right, but first-attempt structured validity was 0%
until the envelope came off — and every one of those failures bought a repair
retry at full model *and* guardrail price. `amazon.nova-micro-v1:0` never
fences, so this is insurance for the next model rather than a crutch for the
current one.

The anchoring tests are the important ones. This unwraps a response that *is*
a fence; it does not hunt for JSON inside prose. A model that wrote a paragraph
and then a fenced example has not answered the question, and quietly extracting
the example would convert an obvious failure into a plausible wrong answer.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from arie.llm.provider import LLMCompletion, LLMUsage
from arie.llm.structured import _validate as validate_completion
from arie.llm.structured import strip_code_fence


class _Shape(BaseModel):
    """A stand-in for `LeadListQueryPlan`, with the same `extra="forbid"` that
    makes an invented field fail rather than pass through silently."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    limit: int | None = None


def _completion(text: str, *, finish_reason: str | None = "stop") -> LLMCompletion:
    return LLMCompletion(
        text=text,
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        model="amazon.nova-micro-v1:0",
        provider="bedrock",
        latency_ms=1.0,
        finish_reason=finish_reason,
    )


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"intent": "top_leads"}\n```',
        '```JSON\n{"intent": "top_leads"}\n```',
        '```\n{"intent": "top_leads"}\n```',
        '  ```json\n{"intent": "top_leads"}\n```  ',
        '```json\r\n{"intent": "top_leads"}\r\n```',
        '```json  \n{"intent": "top_leads"}\n  ```',
        '```json\n{"intent": "top_leads"}```',
    ],
)
def test_fenced_variants_are_unwrapped(raw: str) -> None:
    assert strip_code_fence(raw) == '{"intent": "top_leads"}'


def test_the_exact_shape_gemma_returned_validates() -> None:
    """Verbatim from the benchmark's first failing case."""
    raw = '```json\n{\n  "intent": "top_leads",\n  "limit": 20\n}\n```'
    value, error = validate_completion(_completion(raw), _Shape)
    assert error is None
    assert value is not None and value.intent == "top_leads"


def test_unfenced_json_is_untouched() -> None:
    assert strip_code_fence('{"intent": "top_leads"}') == '{"intent": "top_leads"}'
    value, error = validate_completion(_completion('{"intent": "x"}'), _Shape)
    assert error is None and value is not None


def test_prose_around_a_fence_is_not_mined_for_json() -> None:
    """Anchored at both ends: a model that explained itself and then showed an
    example has not returned a structured answer, and must still fail."""
    raw = 'Here is the plan you asked for:\n```json\n{"intent": "top_leads"}\n```'
    assert strip_code_fence(raw) == raw
    value, error = validate_completion(_completion(raw), _Shape)
    assert value is None
    assert error is not None


def test_trailing_prose_after_a_fence_is_not_mined_either() -> None:
    raw = '```json\n{"intent": "top_leads"}\n```\nLet me know if you need more.'
    assert strip_code_fence(raw) == raw
    value, _ = validate_completion(_completion(raw), _Shape)
    assert value is None


def test_prose_only_response_still_fails() -> None:
    """Pydantic v2 reports a non-JSON body as a `json_invalid` validation
    error rather than raising `ValueError`, so this lands on the
    schema-validation branch. Either way the caller gets `value=None` and
    degrades — which is the behaviour that matters."""
    value, error = validate_completion(_completion("I cannot help with that."), _Shape)
    assert value is None
    assert error is not None and "json_invalid" in error


def test_empty_and_fence_only_responses_fail_as_empty() -> None:
    for raw in ("", "   ", "\n\n"):
        value, error = validate_completion(_completion(raw), _Shape)
        assert value is None
        assert error == "the model returned an empty response"


def test_truncation_is_reported_before_any_unwrapping() -> None:
    """A response cut off at the token limit has an unclosed fence and
    incomplete JSON. Naming the truncation is more useful than a parse error,
    and it must win over fence handling."""
    raw = '```json\n{"intent": "top_lea'
    value, error = validate_completion(_completion(raw, finish_reason="length"), _Shape)
    assert value is None
    assert error is not None and "cut off" in error


def test_schema_violation_inside_a_fence_still_fails_validation() -> None:
    """Unwrapping must not become a second, lenient validation path — the
    Pydantic model remains the only authority on shape."""
    raw = '```json\n{"intent": "top_leads", "organization_id": "other-org"}\n```'
    value, error = validate_completion(_completion(raw), _Shape)
    assert value is None
    assert error is not None and "schema validation failed" in error
