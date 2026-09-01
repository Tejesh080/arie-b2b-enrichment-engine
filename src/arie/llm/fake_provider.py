"""A deterministic in-process :class:`~arie.llm.provider.LLMProvider`.

**The test suite must not need ``DEEPSEEK_API_KEY``.** That is the requirement
this file exists to satisfy, and it is stronger than "there is a mock
available": ``LLM_PROVIDER=fake`` is a real, selectable production-shaped
provider, so every M7 code path — budget accounting, ledger writes, structured
validation, injection fencing, graceful degradation — runs end to end on a
machine with no credential at all, through the same
``arie.llm.factory.build_llm_provider`` seam production uses.

**Deterministic, not random.** Responses come from an explicit script or from
a caller-supplied function of the messages. Token counts are derived from the
text by a fixed rule (:func:`estimate_tokens`) rather than invented, so a
budget test can assert an exact dollar figure and a ledger test can assert an
exact row. ``fake-llm`` is priced at zero in ``arie.ledger.pricing`` — the one
model for which zero is the *true* price rather than a missing-price fallback.

**It also models failure.** A double that only ever succeeds proves nothing
about M7's central promise that an unavailable model degrades instead of
failing a batch, so a scripted entry may be an exception to raise, and
:class:`FakeLLMProvider` can be constructed to fail every call.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from arie.llm.provider import (
    LLMCompletion,
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMUsage,
)

__all__ = [
    "FAKE_MODEL",
    "PROVIDER_NAME",
    "AlwaysFailingLLMProvider",
    "FakeLLMProvider",
    "RecordedCall",
    "estimate_tokens",
]

PROVIDER_NAME = "fake"

FAKE_MODEL = "fake-llm"
"""Priced at $0.00 in ``arie.ledger.pricing.MODEL_PRICES``. A named entry
rather than an unpriced model on purpose: an unpriced model raises
``UnknownModelError``, which is the correct behaviour for a real model nobody
recorded a price for, and would make every fake-provider ledger test fail for
a reason unrelated to what it is testing."""

_CHARS_PER_TOKEN = 4
"""A fixed, deliberately crude rule. It does not need to match any tokenizer —
nothing bills against it — it needs to be *stable*, so that the same prompt
produces the same token count on every run and in every process. Roughly
matching the usual English ratio just keeps fake numbers from looking absurd
in a test's failure message."""


def estimate_tokens(text: str) -> int:
    """Token count for `text` under the fake provider's fixed rule.

    Never zero for non-empty text: a call that consumed something must not
    ledger as having consumed nothing, or a budget test could pass while the
    accounting it is checking silently counts to zero forever.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class RecordedCall:
    """One call the fake received, for a test to assert against.

    ``rendered`` is every message concatenated, which is what an
    injection-fencing test actually wants to search: whether the untrusted
    sentinel survived, whether a credential appeared anywhere, whether the
    prompt exceeded its character cap.
    """

    messages: tuple[LLMMessage, ...]
    json_schema: dict[str, Any] | None
    max_output_tokens: int | None
    temperature: float

    @property
    def rendered(self) -> str:
        return "\n".join(m.content for m in self.messages)

    @property
    def system_text(self) -> str:
        return "\n".join(m.content for m in self.messages if m.role == "system")

    @property
    def user_text(self) -> str:
        return "\n".join(m.content for m in self.messages if m.role == "user")


@dataclass
class FakeLLMProvider(LLMProvider):
    """A provider whose responses are decided by the test, not by a model.

    Three ways to say what it should return, checked in this order:

    1. ``responses`` — a script consumed one entry per call. An entry is either
       the response text, or an exception *instance* to raise.
    2. ``handler`` — a function of the messages, for a test that needs to
       branch on what it was asked.
    3. Neither — an empty JSON object (``"{}"``), which is a syntactically
       valid response that fails almost every real schema. That is the useful
       default: a caller that forgot to script a response gets a *validation*
       failure it can see, not a plausible-looking answer it might believe.

    Running past the end of ``responses`` raises rather than repeating the last
    entry: a test asserting "exactly two LLM calls" should fail loudly when the
    code under test makes three.
    """

    responses: list[str | Exception] = field(default_factory=list)
    handler: Callable[[Sequence[LLMMessage]], str] | None = None
    model_name: str = FAKE_MODEL
    latency_ms: float = 1.0
    finish_reason: str = "stop"
    """Set to ``"length"`` to simulate a response cut off at the output-token
    limit — the case where the body is syntactically broken JSON for a reason
    that is not the model's fault and that ``arie.llm.structured`` reports as
    truncation rather than as a confusing parse error."""
    calls: list[RecordedCall] = field(default_factory=list)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def generate_text(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        return self._respond(
            messages,
            json_schema=None,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

    def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any],
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        return self._respond(
            messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

    def _respond(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any] | None,
        max_output_tokens: int | None,
        temperature: float,
    ) -> LLMCompletion:
        call = RecordedCall(
            messages=tuple(messages),
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        # Recorded before any raise, so a test asserting on a *failed* call's
        # prompt (an injection attempt that the provider then rejected, say)
        # still has the prompt to assert against.
        self.calls.append(call)

        if self.responses:
            if len(self.calls) > len(self.responses):
                raise AssertionError(
                    f"FakeLLMProvider was called {len(self.calls)} times but only "
                    f"{len(self.responses)} responses were scripted"
                )
            scripted = self.responses[len(self.calls) - 1]
            if isinstance(scripted, Exception):
                raise scripted
            text = scripted
        elif self.handler is not None:
            text = self.handler(messages)
        else:
            text = "{}"

        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        if json_schema is not None:
            prompt_tokens += estimate_tokens(json.dumps(json_schema, sort_keys=True))

        return LLMCompletion(
            text=text,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=estimate_tokens(text),
            ),
            model=self.model_name,
            provider=PROVIDER_NAME,
            latency_ms=self.latency_ms,
            finish_reason=self.finish_reason,
        )


@dataclass
class AlwaysFailingLLMProvider(LLMProvider):
    """A provider whose every call raises. The outage case, as a type.

    Separate from ``FakeLLMProvider(responses=[...])`` because "the model is
    down for the whole batch" is not a script of a known length — the point is
    that the caller keeps trying and keeps failing, and the batch still
    completes deterministically.
    """

    error: LLMProviderError = field(
        default_factory=lambda: LLMProviderError("fake provider is configured to always fail")
    )
    model_name: str = FAKE_MODEL
    calls: list[RecordedCall] = field(default_factory=list)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def generate_text(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.calls.append(
            RecordedCall(
                messages=tuple(messages),
                json_schema=None,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
        )
        raise self.error

    def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any],
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.calls.append(
            RecordedCall(
                messages=tuple(messages),
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
        )
        raise self.error
