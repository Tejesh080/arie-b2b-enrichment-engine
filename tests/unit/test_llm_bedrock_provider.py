"""`arie.llm.bedrock_provider` — the Converse request we build and the response we trust.

Every test here runs with no credentials, no network and no boto3, through the
same injected-client seam `tests/unit/test_llm_deepseek_client.py` uses for
httpx. What is pinned is the *request body* and the response mapping, because
those are the parts a vendor contract change breaks silently.

The guardrail-scoping tests are the load-bearing ones. "The guardrail assesses
the customer's question and not ARIE's evidence" is a correctness property, not
a cost optimisation: guarding the evidence block would let the guardrail's PII
policy anonymise the contact names an Ask ARIE answer is made of.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.ledger.pricing import estimated_guardrail_units, guardrail_cost_usd
from arie.llm.bedrock_provider import (
    BEDROCK_MODELS,
    DEFAULT_BEDROCK_MODEL,
    BedrockProvider,
    GuardrailUnits,
)
from arie.llm.provider import (
    LLMGuardrailInterventionError,
    LLMMessage,
    LLMResponseError,
    LLMTransportError,
    LLMUsage,
)
from arie.llm.structured import UntrustedBlock, render_untrusted

_SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}}


def _screen_response(
    *, action: str = "NONE", topic: int = 1, content: int = 1, sensitive: int = 1, word: int = 0
) -> dict[str, Any]:
    """An `ApplyGuardrail` reply. Note `usage` is top level here — Converse
    nests the same block under `invocationMetrics`."""
    return {
        "action": action,
        "usage": {
            "topicPolicyUnits": topic,
            "contentPolicyUnits": content,
            "sensitiveInformationPolicyUnits": sensitive,
            "wordPolicyUnits": word,
        },
    }


class StubBedrockClient:
    """Records both calls the provider can make and replays scripted replies."""

    def __init__(
        self,
        response: dict[str, Any] | Exception,
        *,
        screen: dict[str, Any] | Exception | None = None,
    ) -> None:
        self._response = response
        self._screen = screen if screen is not None else _screen_response()
        self.requests: list[dict[str, Any]] = []
        self.screen_requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.screen_requests.append(kwargs)
        if isinstance(self._screen, Exception):
            raise self._screen
        return self._screen

    @property
    def request(self) -> dict[str, Any]:
        assert len(self.requests) == 1, f"expected exactly one call, got {len(self.requests)}"
        return self.requests[0]

    @property
    def screen_request(self) -> dict[str, Any]:
        assert len(self.screen_requests) == 1, (
            f"expected exactly one guardrail call, got {len(self.screen_requests)}"
        )
        return self.screen_requests[0]

    @property
    def screened_text(self) -> str:
        return str(self.screen_request["content"][0]["text"]["text"])


def _config(**overrides: Any) -> IntelligenceConfig:
    base = dataclasses.replace(
        INTELLIGENCE,
        provider="bedrock",
        model=DEFAULT_BEDROCK_MODEL,
        bedrock_region="us-east-1",
        bedrock_guardrail_id="quwmqu430jgj",
        bedrock_guardrail_version="1",
        bedrock_guarded_labels=("question",),
        max_output_tokens=400,
    )
    return dataclasses.replace(base, **overrides)


def _usage_trace(
    *, topic: int = 1, content: int = 1, sensitive: int = 1, word: int = 0
) -> dict[str, Any]:
    return {
        "guardrail": {
            "inputAssessment": {
                "quwmqu430jgj": {
                    "invocationMetrics": {
                        "usage": {
                            "topicPolicyUnits": topic,
                            "contentPolicyUnits": content,
                            "sensitiveInformationPolicyUnits": sensitive,
                            "wordPolicyUnits": word,
                        }
                    }
                }
            }
        }
    }


def _ok_response(
    text: str = '{"intent": "top_leads"}',
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 1100,
    output_tokens: int = 40,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "ResponseMetadata": {"RequestId": "req-abc-123"},
    }
    if trace is not None:
        response["trace"] = trace
    return response


def _provider(
    response: dict[str, Any] | Exception,
    *,
    screen: dict[str, Any] | Exception | None = None,
    **overrides: Any,
) -> tuple[BedrockProvider, StubBedrockClient]:
    client = StubBedrockClient(response, screen=screen)
    return BedrockProvider(config=_config(**overrides), client=client), client


# --------------------------------------------------------------- translation --


def test_system_messages_are_lifted_out_of_the_message_list() -> None:
    """Converse takes `system` as a top-level array, not a message role."""
    provider, client = _provider(_ok_response())
    provider.generate_text(
        [
            LLMMessage(role="system", content="standing rule"),
            LLMMessage(role="system", content="task instructions"),
            LLMMessage(role="user", content="some data"),
        ]
    )

    assert [block["text"] for block in client.request["system"]] == [
        "standing rule",
        "task instructions",
    ]
    assert [m["role"] for m in client.request["messages"]] == ["user"]
    assert client.request["messages"][0]["content"] == [{"text": "some data"}]


def test_structured_call_prepends_the_schema_ahead_of_every_instruction() -> None:
    """Ordering matches DeepSeek's: instructions first, untrusted data last."""
    provider, client = _provider(_ok_response())
    provider.generate_structured(
        [LLMMessage(role="system", content="task"), LLMMessage(role="user", content="data")],
        json_schema=_SCHEMA,
    )

    system_texts = [block["text"] for block in client.request["system"]]
    assert "JSON Schema" in system_texts[0]
    assert '"intent"' in system_texts[0]
    assert system_texts[1] == "task"


def test_inference_config_carries_the_caller_ceiling_not_the_deployment_default() -> None:
    provider, client = _provider(_ok_response())
    provider.generate_structured(
        [LLMMessage(role="user", content="d")], json_schema=_SCHEMA, max_output_tokens=100
    )
    assert client.request["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.0}


def test_model_id_is_the_configured_model() -> None:
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content="d")])
    assert client.request["modelId"] == DEFAULT_BEDROCK_MODEL
    assert DEFAULT_BEDROCK_MODEL in BEDROCK_MODELS


def test_empty_message_list_is_rejected_before_any_call() -> None:
    provider, client = _provider(_ok_response())
    with pytest.raises(LLMResponseError):
        provider.generate_text([])
    assert client.requests == []


# ------------------------------------------------------------ guardrail scope --


def test_fence_pattern_matches_real_render() -> None:
    """The fence format is duplicated from `arie.llm.structured`; this is the
    test that makes the duplication safe. If `render_untrusted` ever changes
    its delimiters, this fails loudly rather than the guardrail silently
    screening nothing."""
    rendered, _ = render_untrusted(
        [UntrustedBlock(label="question", text="who should I call?")], max_chars=1000
    )
    provider, _ = _provider(_ok_response())
    spans, guarded = provider._select_guarded(rendered)

    assert guarded == ("question",)
    assert spans == ("who should I call?",)


def test_only_the_question_is_submitted_to_the_guardrail() -> None:
    """The central safety property: ARIE's tenant-scoped evidence must never
    reach the guardrail, because the PII policy would anonymise the contact
    names the answer depends on."""
    rendered, _ = render_untrusted(
        [
            UntrustedBlock(label="question", text="who should I call first?"),
            UntrustedBlock(label="leads", text="Dana Whitfield dana@example.com +61 400 000 000"),
        ],
        max_chars=5000,
    )
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    screened = client.screened_text
    sent_to_model = "".join(b["text"] for b in client.request["messages"][0]["content"])

    assert screened == "who should I call first?"
    assert "Dana Whitfield" not in screened
    assert "dana@example.com" not in screened
    # ...and the evidence still reaches the model, unscreened and unmasked.
    assert "Dana Whitfield" in sent_to_model
    assert "dana@example.com" in sent_to_model


def test_the_model_receives_the_message_verbatim() -> None:
    """Screening happens in a separate call, so nothing about the prompt the
    model sees is rewritten — fences included, or the standing untrusted-data
    rule would describe a structure the model cannot see."""
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.request["messages"][0]["content"] == [{"text": rendered}]
    assert "<<<UNTRUSTED_DATA name=question>>>" in rendered


def test_no_inline_guardrail_config_is_ever_sent() -> None:
    """The decisive property. Converse's inline guardrail assesses the model's
    *response* as well as its input, and this guardrail ANONYMIZEs NAME/EMAIL —
    a verified probe came back as literally "{NAME}, {EMAIL}". Ask ARIE answers
    legitimately contain authorized contact details, so the inline form is
    never used and a regression to it must fail here."""
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert "guardrailConfig" not in client.request


def test_the_question_is_screened_with_the_configured_guardrail() -> None:
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.screen_request["guardrailIdentifier"] == "quwmqu430jgj"
    assert client.screen_request["guardrailVersion"] == "1"
    assert client.screen_request["source"] == "INPUT"
    assert provider.last_detail is not None
    assert provider.last_detail.guardrail_applied is True


def test_no_guarded_span_means_nothing_is_screened() -> None:
    """"Screen nothing" and "screen everything" are both wrong; of the two,
    silently screening ARIE's evidence is the one that corrupts answers. The
    call records that the guardrail did not run rather than guessing."""
    rendered, _ = render_untrusted(
        [UntrustedBlock(label="leads", text="Dana Whitfield dana@example.com")], max_chars=5000
    )
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.screen_requests == []
    assert provider.last_detail is not None
    assert provider.last_detail.guardrail_applied is False
    assert provider.last_detail.guarded_labels == ()


def test_empty_guarded_labels_disables_screening() -> None:
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response(), bedrock_guarded_labels=())
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.screen_requests == []
    assert provider.guardrail_enabled is False


def test_unset_guardrail_id_disables_screening() -> None:
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response(), bedrock_guardrail_id="")
    provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.screen_requests == []
    assert provider.guardrail_enabled is False


def test_a_whitespace_only_question_is_not_submitted() -> None:
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="   ")], max_chars=1000)
    provider, client = _provider(_ok_response())
    provider.generate_text([LLMMessage(role="user", content=rendered)])
    assert client.screen_requests == []


# ----------------------------------------------------------------- responses --


def test_usage_maps_from_converse_token_counts() -> None:
    provider, _ = _provider(_ok_response(input_tokens=1100, output_tokens=40))
    completion = provider.generate_text([LLMMessage(role="user", content="d")])

    assert completion.usage.prompt_tokens == 1100
    assert completion.usage.completion_tokens == 40
    assert completion.model == DEFAULT_BEDROCK_MODEL
    assert completion.provider == "bedrock"


def test_guardrail_units_are_collected_and_priced() -> None:
    provider, _ = _provider(
        _ok_response(trace=_usage_trace(topic=1, content=1, sensitive=1, word=3))
    )
    completion = provider.generate_text([LLMMessage(role="user", content="d")])

    assert completion.usage.guardrail_text_units == 6
    # word units are genuinely free; the other three are not.
    assert completion.usage.guardrail_cost_usd == guardrail_cost_usd(
        topic_units=1, content_units=1, sensitive_information_units=1
    )
    assert provider.last_detail is not None
    assert provider.last_detail.request_id == "req-abc-123"


def test_output_assessments_are_summed_alongside_input() -> None:
    """Input is one assessment; output is a list per guardrail. Both bill."""
    trace = _usage_trace(topic=1, content=1, sensitive=0)
    trace["guardrail"]["outputAssessments"] = {
        "quwmqu430jgj": [
            {"invocationMetrics": {"usage": {"topicPolicyUnits": 2, "contentPolicyUnits": 0}}}
        ]
    }
    provider, _ = _provider(_ok_response(trace=trace))
    completion = provider.generate_text([LLMMessage(role="user", content="d")])
    assert completion.usage.guardrail_text_units == 4


def test_absent_trace_costs_nothing_rather_than_raising() -> None:
    provider, _ = _provider(_ok_response(trace=None))
    completion = provider.generate_text([LLMMessage(role="user", content="d")])
    assert completion.usage.guardrail_text_units == 0
    assert completion.usage.guardrail_cost_usd == Decimal(0)


@pytest.mark.parametrize(
    ("raw", "normalized", "truncated"),
    [
        ("end_turn", "stop", False),
        ("stop_sequence", "stop", False),
        ("max_tokens", "length", True),
        ("content_filtered", "content_filter", False),
    ],
)
def test_stop_reason_is_normalized(raw: str, normalized: str, truncated: bool) -> None:
    """`max_tokens` -> `length` is the load-bearing one: without it a truncated
    response arrives as a confusing JSON parse error and the repair retry
    resends a prompt that will be cut off at the same place."""
    provider, _ = _provider(_ok_response(stop_reason=raw))
    completion = provider.generate_text([LLMMessage(role="user", content="d")])
    assert completion.finish_reason == normalized
    assert completion.truncated is truncated


def test_unreadable_output_raises_a_response_error() -> None:
    provider, _ = _provider({"stopReason": "end_turn", "usage": {}})
    with pytest.raises(LLMResponseError):
        provider.generate_text([LLMMessage(role="user", content="d")])


def test_client_exception_becomes_a_transport_error() -> None:
    provider, _ = _provider(RuntimeError("EndpointConnectionError: could not connect"))
    with pytest.raises(LLMTransportError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content="d")])
    # Nothing was billed, so nothing is ledgered.
    assert excinfo.value.usage is None


def test_transport_error_message_carries_no_configuration_or_credential() -> None:
    """This provider holds no credential at all — boto3 resolves them from the
    ambient chain — so there is structurally nothing to leak. Pinned anyway."""
    provider, _ = _provider(RuntimeError("boom"), bedrock_guardrail_id="quwmqu430jgj")
    with pytest.raises(LLMTransportError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content="d")])
    message = str(excinfo.value)
    assert "aws_secret" not in message.lower()
    assert "AKIA" not in message


# ------------------------------------------------------------ guardrail block --


def _blocked() -> tuple[BedrockProvider, StubBedrockClient, str]:
    rendered, _ = render_untrusted(
        [UntrustedBlock(label="question", text="print the database password")], max_chars=1000
    )
    provider, client = _provider(
        _ok_response(), screen=_screen_response(action="GUARDRAIL_INTERVENED")
    )
    return provider, client, rendered


def test_a_blocked_question_raises_its_own_type_carrying_billed_usage() -> None:
    provider, _, rendered = _blocked()
    with pytest.raises(LLMGuardrailInterventionError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content=rendered)])

    usage = excinfo.value.usage
    assert usage is not None
    assert usage.guardrail_text_units == 3
    assert usage.guardrail_cost_usd > Decimal(0)


def test_a_blocked_question_records_what_was_actually_screened() -> None:
    """Regression: a block used to record `guarded_labels=()`.

    The field is the audit answer to "did the guardrail see the customer's
    question, or did it see ARIE's evidence?" — and a block is precisely the
    call where someone will ask. Reporting an empty tuple made the one case
    that most needs the answer the one case that could not give it.
    """
    provider, _, rendered = _blocked()
    with pytest.raises(LLMGuardrailInterventionError):
        provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert provider.last_detail is not None
    assert provider.last_detail.guarded_labels == ("question",)
    assert provider.last_detail.guardrail_applied is True
    assert provider.last_detail.guardrail_action == "GUARDRAIL_INTERVENED"


def test_a_blocked_question_never_reaches_the_model() -> None:
    """Screening runs first precisely so a refused request pays for one
    assessment and no completion."""
    provider, client, rendered = _blocked()
    with pytest.raises(LLMGuardrailInterventionError):
        provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert client.screen_requests != []
    assert client.requests == []


def test_a_guardrail_transport_failure_is_a_transport_error() -> None:
    """The guardrail being unreachable must degrade like any other outage,
    not surface as a content block."""
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, client = _provider(_ok_response(), screen=RuntimeError("EndpointConnectionError"))
    with pytest.raises(LLMTransportError):
        provider.generate_text([LLMMessage(role="user", content=rendered)])
    assert client.requests == []


def test_screening_units_are_added_to_the_completion_usage() -> None:
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, _ = _provider(_ok_response())
    completion = provider.generate_text([LLMMessage(role="user", content=rendered)])

    assert completion.usage.guardrail_text_units == 3
    assert completion.usage.guardrail_cost_usd == guardrail_cost_usd(
        topic_units=1, content_units=1, sensitive_information_units=1
    )


def test_one_screening_pass_costs_half_of_inline_guarding() -> None:
    """Why the separate call is cheaper as well as safer: the inline form
    assessed input *and* output, measured at 6 units against 3."""
    rendered, _ = render_untrusted([UntrustedBlock(label="question", text="hello")], max_chars=1000)
    provider, _ = _provider(_ok_response())
    completion = provider.generate_text([LLMMessage(role="user", content=rendered)])

    inline_equivalent = guardrail_cost_usd(
        topic_units=2, content_units=2, sensitive_information_units=2
    )
    assert completion.usage.guardrail_cost_usd * 2 == inline_equivalent


def test_blocked_message_text_is_not_returned_as_a_completion() -> None:
    """A caller that saw Bedrock's blocked-message string as a completion could
    persist it as an answer."""
    provider, _ = _provider(
        _ok_response(
            text="Sorry, the model cannot answer this question.", stop_reason="guardrail_intervened"
        )
    )
    with pytest.raises(LLMGuardrailInterventionError) as excinfo:
        provider.generate_text([LLMMessage(role="user", content="d")])
    assert "Sorry, the model cannot answer" not in str(excinfo.value)


# --------------------------------------------------------------------- costs --


def test_estimate_cost_includes_guardrail_spend() -> None:
    provider, _ = _provider(_ok_response())
    usage = LLMUsage(
        prompt_tokens=1100,
        completion_tokens=40,
        guardrail_text_units=3,
        guardrail_cost_usd=Decimal("0.00040"),
    )
    assert provider.estimate_cost(usage) > Decimal("0.00040")


def test_guardrail_estimate_is_zero_when_the_guardrail_is_off() -> None:
    provider, _ = _provider(_ok_response(), bedrock_guardrail_id="")
    assert provider.estimate_guardrail_cost_usd(guarded_chars=5000) == Decimal(0)


def test_guardrail_estimate_rounds_units_up_and_prices_two_passes() -> None:
    provider, _ = _provider(_ok_response())
    # 1200 chars is two text units, not one.
    assert estimated_guardrail_units(1200) == 2
    expected = guardrail_cost_usd(topic_units=2, content_units=2, sensitive_information_units=2) * 2
    assert provider.estimate_guardrail_cost_usd(guarded_chars=1200) == expected


def test_guardrail_units_total_and_addition() -> None:
    a = GuardrailUnits(topic=1, content=2)
    b = GuardrailUnits(sensitive_information=3, word=4)
    assert (a + b).total == 10
    assert GuardrailUnits().total == 0


# ------------------------------------------------------------ model selection --


def test_nova_micro_is_the_default_and_gemma_stays_selectable() -> None:
    """Nova is the default on measured semantic accuracy (95% vs 65% on the
    frozen Ask ARIE set). Gemma stays listed and priced so that benchmark
    remains reproducible rather than something to take on faith."""
    from arie.ledger.pricing import MODEL_PRICES

    assert DEFAULT_BEDROCK_MODEL == "amazon.nova-micro-v1:0"
    assert "google.gemma-3-4b-it" in BEDROCK_MODELS
    assert all(model in MODEL_PRICES for model in BEDROCK_MODELS)


def test_nova_input_is_cheaper_and_output_dearer_than_gemma() -> None:
    """Pins the price *shape* the model choice depends on. ARIE's copilot
    calls are ~1,400 input tokens against ~20 output, so an input-cheap model
    wins even when its output rate is higher — the reason this decision was
    not made on headline token price."""
    from arie.ledger.pricing import MODEL_PRICES

    nova = MODEL_PRICES["amazon.nova-micro-v1:0"]
    gemma = MODEL_PRICES["google.gemma-3-4b-it"]
    assert nova.usd_per_1m_input_tokens < gemma.usd_per_1m_input_tokens
    assert nova.usd_per_1m_output_tokens > gemma.usd_per_1m_output_tokens
    assert nova.cost_usd(prompt_tokens=1449, completion_tokens=20) < Decimal("0.0001")
