"""The provider contract, both implementations, and the factory that picks one.

Nothing here touches the network or reads ``DEEPSEEK_API_KEY``. The DeepSeek
provider is driven through an ``httpx.MockTransport`` and the fake needs no
transport at all, which is the property M7 Slice 1 is supposed to guarantee:
the whole intelligence layer is exercisable on a machine that has never had a
model credential.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import httpx
import pytest

from arie.config import IntelligenceConfig
from arie.ledger.pricing import UnknownModelError
from arie.llm.deepseek_provider import DeepSeekProvider
from arie.llm.factory import SUPPORTED_PROVIDERS, build_llm_provider, resolve_model
from arie.llm.fake_provider import (
    FAKE_MODEL,
    AlwaysFailingLLMProvider,
    FakeLLMProvider,
    estimate_tokens,
)
from arie.llm.provider import (
    LLMCompletion,
    LLMMessage,
    LLMProviderError,
    LLMResponseError,
    LLMTransportError,
    LLMUnavailableError,
    LLMUsage,
)

_SECRET = "sk-do-not-leak-this-anywhere"


def _config(**overrides: object) -> IntelligenceConfig:
    """A config built from explicit values, never from the ambient environment.

    ``IntelligenceConfig()`` reads ``os.environ``; a developer with a real key
    in ``.env`` would otherwise get different test behaviour from CI, and the
    leak assertions below would be checking a value nobody set.
    """
    base = IntelligenceConfig(
        provider="deepseek",
        model="deepseek-chat",
        api_key=_SECRET,
        base_url="https://api.deepseek.test",
        timeout_seconds=5.0,
        max_attempts=2,
        max_output_tokens=256,
        max_untrusted_chars=1000,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _client(handler: httpx.MockTransport | None = None) -> httpx.Client:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    return httpx.Client(transport=transport, base_url="https://api.deepseek.test")


def _ok_body(content: str, *, prompt: int = 40, completion: int = 12) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


# --------------------------------------------------------------- deepseek --


def test_deepseek_maps_a_successful_response_onto_llmcompletion() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json=_ok_body('{"ok": true}'))

    with DeepSeekProvider(config=_config(), client=_client(httpx.MockTransport(handle))) as p:
        completion = p.generate_text([LLMMessage(role="user", content="hello")])

    assert captured["path"] == "/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 256
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert "response_format" not in body  # generate_text is not JSON mode

    assert completion.text == '{"ok": true}'
    assert completion.usage == LLMUsage(prompt_tokens=40, completion_tokens=12)
    assert completion.model == "deepseek-chat"
    assert completion.provider == "deepseek"
    assert completion.finish_reason == "stop"
    assert not completion.truncated


def test_generate_structured_sets_json_mode_and_prepends_the_schema() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body("{}"))

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with DeepSeekProvider(config=_config(), client=_client(httpx.MockTransport(handle))) as p:
        p.generate_structured([LLMMessage(role="user", content="data")], json_schema=schema)

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    messages = body["messages"]
    assert isinstance(messages, list)
    # The schema goes first, as a system message, ahead of the user data.
    assert messages[0]["role"] == "system"
    assert '"integer"' in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "data"}
    # DeepSeek rejects json_object mode unless the prompt mentions JSON.
    assert "json" in messages[0]["content"].lower()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="upstream exploded"),
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(401, json={"error": "bad key"}),
    ],
)
def test_a_non_2xx_becomes_a_transport_error(response: httpx.Response) -> None:
    with (
        DeepSeekProvider(
            config=_config(), client=_client(httpx.MockTransport(lambda _: response))
        ) as p,
        pytest.raises(LLMTransportError),
    ):
        p.generate_text([LLMMessage(role="user", content="x")])


def test_a_network_failure_becomes_a_transport_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with DeepSeekProvider(config=_config(), client=_client(httpx.MockTransport(boom))) as p:  # noqa: SIM117
        with pytest.raises(LLMTransportError):
            p.generate_text([LLMMessage(role="user", content="x")])


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"no_choices_at_all": True},
        {"choices": [{"message": {"content": 42}}]},
    ],
)
def test_an_unreadable_completion_becomes_a_response_error(body: dict[str, object]) -> None:
    with (
        DeepSeekProvider(
            config=_config(),
            client=_client(httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
        ) as p,
        pytest.raises(LLMResponseError),
    ):
        p.generate_text([LLMMessage(role="user", content="x")])


def test_a_non_json_body_becomes_a_response_error() -> None:
    with (
        DeepSeekProvider(
            config=_config(),
            client=_client(httpx.MockTransport(lambda _: httpx.Response(200, text="<html>"))),
        ) as p,
        pytest.raises(LLMResponseError),
    ):
        p.generate_text([LLMMessage(role="user", content="x")])


def test_missing_usage_is_zero_not_absent() -> None:
    body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    with DeepSeekProvider(
        config=_config(),
        client=_client(httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
    ) as p:
        completion = p.generate_text([LLMMessage(role="user", content="x")])
    assert completion.usage == LLMUsage(0, 0)


def test_a_truncated_completion_is_flagged() -> None:
    body = {
        "choices": [{"message": {"content": '{"partial":'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 256},
    }
    with DeepSeekProvider(
        config=_config(),
        client=_client(httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
    ) as p:
        assert p.generate_text([LLMMessage(role="user", content="x")]).truncated


def test_no_credential_is_a_controlled_error_naming_the_variables() -> None:
    with pytest.raises(LLMUnavailableError) as exc:
        DeepSeekProvider(config=_config(api_key=""))
    assert "LLM_API_KEY" in str(exc.value)
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_the_api_key_never_appears_in_an_error_message() -> None:
    """The key travels in a header. No failure path may render it.

    Checked across every failure mode rather than one, because a leak added
    later would most plausibly arrive in whichever branch nobody thought about.
    """

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    failures: list[str] = []
    for transport in (
        httpx.MockTransport(boom),
        httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "bad key"})),
        httpx.MockTransport(lambda _: httpx.Response(200, text="<html>")),
        httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []})),
    ):
        with DeepSeekProvider(config=_config(), client=_client(transport)) as p:
            try:
                p.generate_text([LLMMessage(role="user", content="x")])
            except Exception as exc:
                failures.append(f"{exc!r}")
    assert failures, "expected every transport above to fail"
    assert not any(_SECRET in message for message in failures)


def test_an_empty_message_list_is_rejected_before_any_request() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_ok_body("{}"))

    with DeepSeekProvider(config=_config(), client=_client(httpx.MockTransport(handle))) as p:  # noqa: SIM117
        with pytest.raises(LLMResponseError):
            p.generate_text([])
    assert calls == []


def test_close_is_idempotent_and_does_not_close_an_injected_client() -> None:
    client = _client()
    provider = DeepSeekProvider(config=_config(), client=client)
    provider.close()
    provider.close()
    assert not client.is_closed  # the caller owns an injected client


# ------------------------------------------------------------------- fake --


def test_the_fake_returns_scripted_responses_in_order() -> None:
    fake = FakeLLMProvider(responses=["first", "second"])
    assert fake.generate_text([LLMMessage(role="user", content="a")]).text == "first"
    assert fake.generate_text([LLMMessage(role="user", content="b")]).text == "second"
    assert fake.call_count == 2


def test_the_fake_fails_loudly_when_called_more_often_than_scripted() -> None:
    fake = FakeLLMProvider(responses=["only one"])
    fake.generate_text([LLMMessage(role="user", content="a")])
    with pytest.raises(AssertionError, match="only 1 responses"):
        fake.generate_text([LLMMessage(role="user", content="b")])


def test_a_scripted_exception_is_raised() -> None:
    fake = FakeLLMProvider(responses=[LLMTransportError("simulated outage")])
    with pytest.raises(LLMTransportError, match="simulated outage"):
        fake.generate_text([LLMMessage(role="user", content="a")])
    # Recorded even though it raised, so a test can still inspect the prompt.
    assert fake.call_count == 1


def test_the_unscripted_default_is_an_empty_object_not_a_plausible_answer() -> None:
    fake = FakeLLMProvider()
    assert fake.generate_text([LLMMessage(role="user", content="a")]).text == "{}"


def test_the_fake_records_the_prompt_it_was_given() -> None:
    fake = FakeLLMProvider(responses=["ok"])
    fake.generate_structured(
        [LLMMessage(role="system", content="rules"), LLMMessage(role="user", content="data")],
        json_schema={"type": "object"},
    )
    call = fake.calls[0]
    assert call.system_text == "rules"
    assert call.user_text == "data"
    assert "rules" in call.rendered and "data" in call.rendered
    assert call.json_schema == {"type": "object"}


def test_fake_token_counts_are_deterministic_and_never_zero_for_content() -> None:
    fake = FakeLLMProvider(responses=["abcd"])
    completion = fake.generate_text([LLMMessage(role="user", content="12345678")])
    assert completion.usage == LLMUsage(prompt_tokens=2, completion_tokens=1)
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1


def test_the_fake_is_priced_at_zero_so_it_can_be_ledgered() -> None:
    fake = FakeLLMProvider()
    assert fake.model == FAKE_MODEL
    assert fake.estimate_cost(LLMUsage(1_000_000, 1_000_000)) == Decimal(0)


def test_the_always_failing_provider_fails_every_call_and_still_records_it() -> None:
    provider = AlwaysFailingLLMProvider()
    for _ in range(3):
        with pytest.raises(LLMProviderError):
            provider.generate_structured(
                [LLMMessage(role="user", content="x")], json_schema={"type": "object"}
            )
    assert provider.call_count == 3


# ------------------------------------------------------------- estimate --


def test_estimate_cost_uses_the_ledger_price_table() -> None:
    provider = DeepSeekProvider(config=_config(), client=_client())
    # deepseek-chat: $0.27/1M input, $1.10/1M output.
    assert provider.estimate_cost(LLMUsage(1_000_000, 0)) == Decimal("0.27")
    assert provider.estimate_cost(LLMUsage(0, 1_000_000)) == Decimal("1.10")
    provider.close()


def test_estimate_cost_raises_for_an_unpriced_model_rather_than_returning_zero() -> None:
    provider = DeepSeekProvider(config=_config(model="some-unreleased-model"), client=_client())
    with pytest.raises(UnknownModelError):
        provider.estimate_cost(LLMUsage(10, 10))
    provider.close()


# ------------------------------------------------------------- factory --


def test_the_factory_builds_the_fake_without_any_credential() -> None:
    provider = build_llm_provider(config=_config(provider="fake", api_key=""))
    assert isinstance(provider, FakeLLMProvider)
    assert provider.model == FAKE_MODEL


def test_the_factory_builds_deepseek_when_configured() -> None:
    provider = build_llm_provider(config=_config())
    assert isinstance(provider, DeepSeekProvider)
    assert provider.name == "deepseek"
    provider.close()


def test_deepseek_without_a_key_is_unavailable_not_silently_faked() -> None:
    with pytest.raises(LLMUnavailableError):
        build_llm_provider(config=_config(api_key=""))


def test_provider_none_is_unavailable() -> None:
    with pytest.raises(LLMUnavailableError, match="switched off"):
        build_llm_provider(config=_config(provider="none"))


def test_an_unrecognised_provider_refuses_rather_than_guessing() -> None:
    with pytest.raises(LLMUnavailableError) as exc:
        build_llm_provider(config=_config(provider="opeanai"))
    assert "refusing to guess" in str(exc.value)
    for name in SUPPORTED_PROVIDERS:
        assert name in str(exc.value)


def test_an_organizations_preferred_model_is_honoured_when_priced() -> None:
    provider = build_llm_provider(config=_config(), preferred_model="deepseek-reasoner")
    assert provider.model == "deepseek-reasoner"
    provider.close()


def test_an_unpriced_preferred_model_falls_back_to_the_deployment_default() -> None:
    """An organization's stale preference must degrade it, not fail its batch."""
    assert resolve_model(_config(), "a-model-that-was-withdrawn") == "deepseek-chat"


def test_an_unpriced_deployment_default_raises_because_there_is_no_fallback() -> None:
    with pytest.raises(LLMUnavailableError, match="MODEL_PRICES"):
        resolve_model(_config(model="mystery-model"), None)


# ------------------------------------------------------------- config --


def test_fake_is_configured_without_a_key_and_none_never_is() -> None:
    assert _config(provider="fake", api_key="").configured
    assert not _config(provider="none", api_key=_SECRET).configured
    assert not _config(api_key="").configured
    assert _config().configured


def test_llm_completion_is_immutable() -> None:
    completion = LLMCompletion(text="x", usage=LLMUsage(), model="m", provider="p", latency_ms=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        completion.text = "y"  # type: ignore[misc]
