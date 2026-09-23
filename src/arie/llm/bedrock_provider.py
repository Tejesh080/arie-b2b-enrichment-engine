"""Amazon Bedrock behind :class:`~arie.llm.provider.LLMProvider`.

One ``Converse`` call per :meth:`generate_text`/:meth:`generate_structured`, with
the same no-retries-here rule ``arie.llm.deepseek_provider`` states: retry policy
belongs to ``arie.llm.structured``, which is the layer that knows what a retry
costs.

**Why a provider and not an integration.** Everything Ask ARIE needs from a
model — tenant-scoped evidence, citation checks, budget authorisation, ledger
rows, untrusted-data fencing — already happens above
:class:`~arie.llm.provider.LLMProvider`. Implementing three methods puts Bedrock
underneath all of it. A bespoke "Bedrock copilot" path would have had to
re-earn each of those guarantees, and would have bypassed
``arie.llm.service.LLMService``, which is the only thing enforcing
budget-before-call and ledger-after-call.

**The guardrail screens the customer's question, in a separate call, before
the model runs.** ``ApplyGuardrail`` on the question alone, then ``Converse``
with **no** ``guardrailConfig`` — deliberately not Bedrock's inline guardrail.

The inline form was tried first and rejected on measured behaviour. Converse's
``guardrailConfig`` assesses the model's *response* as well as its input, and
no ``guardContent`` tagging changes that: tagging narrows which input spans are
submitted, not whether the output is. With this guardrail's PII policy — which
``ANONYMIZE``s ``NAME``, ``EMAIL``, ``PHONE`` and ``ADDRESS`` — a verified
probe asking "who is the contact?" came back as literally ``{NAME}, {EMAIL}``
with ``stopReason: guardrail_intervened``.

That is the precise failure this design exists to avoid. ARIE's evidence block
carries the contact names and email addresses of the calling organization's own
leads, which it is already authorized to read; masking them does not produce a
safer answer, it produces a wrong one. The customer's question is the only span
in an Ask ARIE prompt an attacker authors, so it is the only span that needs
assessing.

Screening separately also costs *less*: one assessment pass rather than two,
which measured as 3 text units instead of 6 — $0.00040 against $0.00080 per
request. The price is one extra round trip before the model call.

**It fails closed on scope, not on availability.** If no guarded span is found,
nothing is screened and nothing is silently guarded in its place; callers
needing a guarantee the screen ran assert on
:attr:`BedrockCompletionDetail.guardrail_applied`, recorded on every call. A
blocked question raises :class:`LLMGuardrailInterventionError` *before* any
model call, so a refused request never pays for a completion.

**This module never holds a credential.** boto3 resolves them from the ambient
chain (instance role, environment, ``AWS_PROFILE``), so unlike
``arie.llm.deepseek_provider`` there is no key in a formattable position for an
error message to leak — the property is structural here rather than pinned by a
test, though ``tests/unit/test_llm_bedrock_provider.py`` pins it anyway.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.ledger.pricing import (
    estimated_guardrail_units,
    guardrail_cost_usd,
)
from arie.llm.provider import (
    LLMCompletion,
    LLMGuardrailInterventionError,
    LLMMessage,
    LLMProvider,
    LLMResponseError,
    LLMTransportError,
    LLMUnavailableError,
    LLMUsage,
)
from arie.observability.tracing import get_tracer, set_attributes, traced

__all__ = [
    "BEDROCK_MODELS",
    "DEFAULT_BEDROCK_MODEL",
    "PROVIDER_NAME",
    "BedrockProvider",
    "GuardrailUnits",
]

_TRACER = get_tracer("arie.llm.bedrock_provider")

PROVIDER_NAME = "bedrock"

DEFAULT_BEDROCK_MODEL = "amazon.nova-micro-v1:0"
"""Chosen by measurement, not by list price.

Benchmarked against ``google.gemma-3-4b-it`` using the real instructions, the
real schemas and ``arie.llm.structured.generate_structured``, at temperature 0,
over two runs with identical results: **95% semantic correctness on a frozen
20-case Ask ARIE benchmark, against Gemma's 65%**, with a tighter latency tail
(1.3s max against 3.0s). That figure is scoped to those 20 cases and to intent
classification; it is not a general accuracy claim. Gemma is the cheaper model
per token and still loses, because an intent ARIE has to reject is an intent
nobody paid for usefully — per *usable* answer Nova is about 30% cheaper.

Gemma's failures were not malformed JSON. It confused the two closed
vocabularies, answering a list question with ``lead_researchability`` and a
single-lead question with ``needs_research``. Schema validation cannot catch
that — both are members of ``CopilotIntent`` — so the thing that caught every
one was ``arie.copilot_service``'s existing ``intent not in LIST_INTENTS``
check. That guard is load-bearing and has a regression test for exactly this
reason."""

BEDROCK_MODELS = frozenset({"amazon.nova-micro-v1:0", "google.gemma-3-4b-it"})
"""Models this build knows how to reach *and* price on Bedrock.

Used by ``arie.llm.factory`` to reject a model that belongs to a different
vendor. Without it, an organization whose ``preferred_llm_model`` is
``deepseek-chat`` would, under the copilot override, get a ``BedrockProvider``
politely asking Bedrock for a DeepSeek model — a confusing runtime 400 in place
of a clear configuration error.

Gemma stays listed and priced though it is not the default, so the benchmark
that rejected it stays reproducible. A comparison you cannot re-run is a
comparison you have to take on faith the next time someone asks why this model
and not that one."""

_JSON_INSTRUCTION = """Respond with a single JSON object matching this JSON Schema exactly. \
Output the JSON object and nothing else — no prose, no explanation, no markdown code fences.

{schema}"""
"""Converse has no ``response_format`` equivalent, so the schema in a system
message is the *only* pressure toward a correctly shaped object — weaker than
DeepSeek's JSON mode, which is why ``arie.llm.structured``'s validate-then-
repair loop matters more here.

The "no markdown code fences" sentence is not decoration. Gemma 3 4B ignored it
on **every** call of the benchmark, returning correct JSON inside a ```` ```json ````
fence; that alone took it from 95% first-attempt validity to 0%. Nova Micro
never fences. ``arie.llm.structured`` now strips a fence before validating, so
neither the instruction nor any one model's habit is load-bearing."""

_FENCE_SPAN = re.compile(
    r"(<<<UNTRUSTED_DATA name=(?P<label>[a-z0-9_]{1,48})>>>\n)"
    r"(?P<body>.*?)"
    r"(\n<<<END_UNTRUSTED_DATA name=(?P=label)>>>)",
    re.DOTALL,
)
"""Matches one fenced block as ``arie.llm.structured.render_untrusted`` writes it.

Duplicating that module's fence format here is deliberate, and is the same
trade ``arie.llm.deepseek_provider`` documents for DeepSeek's request body: the
alternative is importing a private constant across a layer boundary.
``tests/unit/test_llm_bedrock_provider.py::test_fence_pattern_matches_real_render``
asserts this pattern against actual ``render_untrusted`` output, so a change to
the fence format fails loudly here instead of silently disabling the guardrail."""


@dataclass(frozen=True)
class GuardrailUnits:
    """Billable guardrail text units, per policy, as Bedrock reported them."""

    topic: int = 0
    content: int = 0
    sensitive_information: int = 0
    contextual_grounding: int = 0
    word: int = 0

    @property
    def total(self) -> int:
        return (
            self.topic
            + self.content
            + self.sensitive_information
            + self.contextual_grounding
            + self.word
        )

    def cost_usd(self) -> Decimal:
        return guardrail_cost_usd(
            topic_units=self.topic,
            content_units=self.content,
            sensitive_information_units=self.sensitive_information,
            contextual_grounding_units=self.contextual_grounding,
            word_units=self.word,
        )

    def __add__(self, other: GuardrailUnits) -> GuardrailUnits:
        return GuardrailUnits(
            topic=self.topic + other.topic,
            content=self.content + other.content,
            sensitive_information=self.sensitive_information + other.sensitive_information,
            contextual_grounding=self.contextual_grounding + other.contextual_grounding,
            word=self.word + other.word,
        )


@dataclass(frozen=True)
class BedrockCompletionDetail:
    """Per-call Bedrock facts that :class:`LLMCompletion` has no field for.

    Recorded on the provider (:attr:`BedrockProvider.last_detail`) rather than
    widened into ``LLMCompletion``, which is a vendor-neutral type shared with
    DeepSeek and the fake. Read by the live smoke test and by tracing; nothing
    in the decision path consumes it.
    """

    request_id: str | None
    stop_reason: str | None
    guardrail_action: str
    guardrail_applied: bool
    units: GuardrailUnits
    guarded_labels: tuple[str, ...]


_STOP_REASONS = {
    # Bedrock's vocabulary -> the vocabulary `LLMCompletion.truncated` and
    # `arie.llm.structured._validate` already understand. Only the truncation
    # case is load-bearing: without this mapping a response cut off at
    # `maxTokens` would arrive as a JSON parse error, and the repair retry
    # would resend a prompt that is going to be cut off at exactly the same
    # place, for exactly the same money.
    "max_tokens": "length",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "content_filtered": "content_filter",
    "guardrail_intervened": "guardrail_intervened",
}


def _normalize_stop_reason(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return _STOP_REASONS.get(raw, raw)


def _iter_invocation_usage(node: object) -> Iterator[dict[str, Any]]:
    """Every ``invocationMetrics.usage`` block anywhere in a guardrail trace.

    Recursive and tolerant because the trace nests differently for input
    (one assessment) and output (a list per guardrail), and because an
    assessment that fired no policy may omit the block entirely. Anchoring on
    the ``invocationMetrics`` parent rather than on the presence of
    ``topicPolicyUnits`` keeps a future top-level summary block from being
    counted twice.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "invocationMetrics" and isinstance(value, dict):
                usage = value.get("usage")
                if isinstance(usage, dict):
                    yield usage
            else:
                yield from _iter_invocation_usage(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_invocation_usage(item)


def _units_from_usage(usage: dict[str, Any]) -> GuardrailUnits:
    """One Bedrock ``usage`` block as units. ``ApplyGuardrail`` returns this at
    the top level; Converse nests it under ``invocationMetrics``."""
    return GuardrailUnits(
        topic=int(usage.get("topicPolicyUnits", 0) or 0),
        content=int(usage.get("contentPolicyUnits", 0) or 0),
        sensitive_information=int(usage.get("sensitiveInformationPolicyUnits", 0) or 0),
        contextual_grounding=int(usage.get("contextualGroundingPolicyUnits", 0) or 0),
        word=int(usage.get("wordPolicyUnits", 0) or 0),
    )


def _collect_units(trace: object) -> GuardrailUnits:
    units = GuardrailUnits()
    for usage in _iter_invocation_usage(trace):
        units += _units_from_usage(usage)
    return units


class BedrockProvider(LLMProvider):
    """A ``bedrock-runtime`` client bound to one model and one guardrail.

    Inject ``client`` in tests with any object exposing ``converse(**kwargs)``
    — the same seam ``DeepSeekProvider`` offers via ``httpx`` — so every path
    here is exercisable with no credentials, no network and no boto3 import.
    Passing a client also bypasses the boto3 import check, which is what makes
    the unit tests runnable on a machine that has never installed the AWS SDK.
    """

    def __init__(
        self,
        *,
        config: IntelligenceConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self._config = config or INTELLIGENCE
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - exercised by the import guard test
                raise LLMUnavailableError(
                    "boto3 is not installed, so the Bedrock provider cannot be constructed — "
                    'install the service extra (pip install -e ".[service]"). Pass an explicit '
                    "`client` (e.g. in tests) to bypass this check."
                ) from exc
            client = boto3.client("bedrock-runtime", region_name=self._config.bedrock_region)
        self._client = client
        self._closed = False
        self.last_detail: BedrockCompletionDetail | None = None
        """Bedrock-specific facts from the most recent call. Overwritten per
        call and not thread-safe — a diagnostic for the live smoke test, never
        an input to a decision."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def guardrail_enabled(self) -> bool:
        return bool(self._config.bedrock_guardrail_id and self._config.bedrock_guarded_labels)

    def close(self) -> None:
        self._closed = True

    def estimate_guardrail_cost_usd(self, *, guarded_chars: int) -> Decimal:
        """Worst-case guardrail cost for `guarded_chars` of content.

        Pessimistic in two ways, both deliberate. It prices *all* untrusted
        content even though only the labelled spans are submitted, because the
        estimator upstream does not know the labels. And it prices the input
        assessment twice, standing in for an output assessment that may or may
        not be enabled later. An estimate that came in under the true cost
        would let an organization pass a budget check it should have failed.
        """
        if not self.guardrail_enabled:
            return Decimal(0)
        units = estimated_guardrail_units(guarded_chars)
        per_pass = guardrail_cost_usd(
            topic_units=units,
            content_units=units,
            sensitive_information_units=units,
        )
        return per_pass * 2

    def generate_text(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        return self._call(
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
        return self._call(
            messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )

    def _select_guarded(self, text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Extract the untrusted spans that should be screened.

        Returns ``(span_texts, labels)``. The message itself is sent to the
        model unchanged — fences and all — because screening happens in a
        separate ``ApplyGuardrail`` call and the model must still see the
        fenced structure the standing untrusted-data rule describes.
        """
        selected = set(self._config.bedrock_guarded_labels)
        spans: list[str] = []
        labels: list[str] = []
        for match in _FENCE_SPAN.finditer(text):
            label = match.group("label")
            if label not in selected:
                continue
            body = match.group("body")
            if not body.strip():
                continue
            spans.append(body)
            labels.append(label)
        return tuple(spans), tuple(labels)

    def _screen(
        self, spans: tuple[str, ...], labels: tuple[str, ...]
    ) -> tuple[GuardrailUnits, str]:
        """Run the guardrail over the customer-authored spans.

        Raises :class:`LLMGuardrailInterventionError` — carrying the units the
        assessment consumed — when the guardrail blocks. Raising *here*, before
        the model call, is the point: a blocked question costs one assessment
        and no completion.

        ``labels`` is carried purely so a blocked call can record *what was
        screened*. It is the audit answer to "did this guardrail see the
        customer's question, or did it see ARIE's evidence?", and a block is
        exactly the case where someone will ask.
        """
        joined = "\n\n".join(spans)
        try:
            response: dict[str, Any] = self._client.apply_guardrail(
                guardrailIdentifier=self._config.bedrock_guardrail_id,
                guardrailVersion=self._config.bedrock_guardrail_version,
                source="INPUT",
                content=[{"text": {"text": joined}}],
            )
        except Exception as exc:
            raise LLMTransportError(
                f"Bedrock apply_guardrail failed: {type(exc).__name__}: {exc}"
            ) from exc

        raw = response.get("usage")
        units = _units_from_usage(raw) if isinstance(raw, dict) else GuardrailUnits()
        action = str(response.get("action") or "NONE")
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
        if action == "GUARDRAIL_INTERVENED":
            # Recorded *before* raising. Without this the blocked call leaves
            # `last_detail` holding the previous call's values, so a caller
            # reporting the request id of a block would cite an unrelated
            # request — which is worse than reporting none, because it looks
            # like a real id and cannot be traced.
            self.last_detail = BedrockCompletionDetail(
                request_id=request_id if isinstance(request_id, str) else None,
                stop_reason=None,
                guardrail_action="GUARDRAIL_INTERVENED",
                guardrail_applied=True,
                units=units,
                guarded_labels=labels,
            )
            raise LLMGuardrailInterventionError(
                "the question was blocked by the Bedrock content guardrail",
                usage=LLMUsage(
                    guardrail_text_units=units.total,
                    guardrail_cost_usd=units.cost_usd(),
                ),
            )
        return units, action

    def _build_request(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any] | None,
        max_output_tokens: int | None,
        temperature: float,
    ) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
        system: list[dict[str, str]] = []
        user_blocks: list[dict[str, Any]] = []
        spans: tuple[str, ...] = ()
        guarded: tuple[str, ...] = ()

        if json_schema is not None:
            # Prepended for the reason `arie.llm.deepseek_provider` gives:
            # every instruction stays ahead of the untrusted data the caller
            # already fenced, so an injection attempt never gets the last word.
            system.append(
                {
                    "text": _JSON_INSTRUCTION.format(
                        schema=json.dumps(json_schema, indent=2, sort_keys=True)
                    )
                }
            )

        for message in messages:
            if message.role == "system":
                system.append({"text": message.content})
                continue
            message_spans, labels = self._select_guarded(message.content)
            # Sent verbatim. The guardrail sees the extracted spans through a
            # separate ApplyGuardrail call; the model sees the whole fenced
            # message, unmodified and unmasked.
            user_blocks.append({"text": message.content})
            spans = spans + message_spans
            guarded = guarded + labels

        request: dict[str, Any] = {
            "modelId": self._config.model,
            "messages": [{"role": "user", "content": user_blocks}],
            "inferenceConfig": {
                "maxTokens": max_output_tokens or self._config.max_output_tokens,
                "temperature": temperature,
            },
        }
        if system:
            request["system"] = system
        return request, spans, guarded

    def _call(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any] | None,
        max_output_tokens: int | None,
        temperature: float,
    ) -> LLMCompletion:
        if not messages:
            raise LLMResponseError("cannot call a model with no messages")

        request, spans, guarded = self._build_request(
            messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        if not request["messages"][0]["content"]:
            raise LLMResponseError("cannot call Bedrock with no user content")

        screened = bool(spans) and self.guardrail_enabled
        screen_units = GuardrailUnits()
        guardrail_action = "NONE"
        if screened:
            # Before the model call, so a blocked question never buys a
            # completion. Raises LLMGuardrailInterventionError on a block.
            screen_units, guardrail_action = self._screen(spans, guarded)

        with traced(
            _TRACER,
            "llm.bedrock.generate",
            attributes={
                "arie.llm.provider": PROVIDER_NAME,
                "arie.llm.model": self._config.model,
                "arie.llm.structured": json_schema is not None,
                "arie.bedrock.guardrail_id": self._config.bedrock_guardrail_id,
                "arie.bedrock.guardrail_version": self._config.bedrock_guardrail_version,
                "arie.bedrock.guarded_labels": ",".join(guarded),
            },
        ) as span:
            started = time.monotonic()
            try:
                response: dict[str, Any] = self._client.converse(**request)
            except Exception as exc:
                # Deliberately broad: botocore raises ClientError,
                # EndpointConnectionError, NoCredentialsError and several more
                # from a private hierarchy this module does not import. They
                # all mean the same thing to every caller — no completion, so
                # nothing billed and nothing to ledger. The message carries the
                # exception type and its own text; this provider never holds a
                # credential, so there is none to interpolate.
                raise LLMTransportError(
                    f"Bedrock converse failed: {type(exc).__name__}: {exc}"
                ) from exc

            latency_ms = (time.monotonic() - started) * 1000

            # `_collect_units` still runs so that a deployment which later
            # re-enables an inline guardrail is accounted for rather than
            # silently free. Today it is zero: no guardrailConfig is sent.
            units = screen_units + _collect_units(response.get("trace"))
            usage_raw = response.get("usage") or {}
            usage = LLMUsage(
                prompt_tokens=int(usage_raw.get("inputTokens", 0) or 0),
                completion_tokens=int(usage_raw.get("outputTokens", 0) or 0),
                guardrail_text_units=units.total,
                guardrail_cost_usd=units.cost_usd(),
            )
            raw_stop = response.get("stopReason")
            stop_reason = _normalize_stop_reason(raw_stop)
            request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
            self.last_detail = BedrockCompletionDetail(
                request_id=request_id if isinstance(request_id, str) else None,
                stop_reason=raw_stop if isinstance(raw_stop, str) else None,
                guardrail_action=guardrail_action,
                guardrail_applied=screened,
                units=units,
                guarded_labels=guarded,
            )
            set_attributes(
                span,
                {
                    "arie.llm.prompt_tokens": usage.prompt_tokens,
                    "arie.llm.completion_tokens": usage.completion_tokens,
                    "arie.llm.finish_reason": stop_reason or "",
                    "arie.llm.latency_ms": latency_ms,
                    "arie.bedrock.stop_reason": raw_stop if isinstance(raw_stop, str) else "",
                    "arie.bedrock.request_id": request_id if isinstance(request_id, str) else "",
                    "arie.bedrock.guardrail_action": guardrail_action,
                    "arie.bedrock.guardrail_applied": screened,
                    "arie.bedrock.guardrail_text_units": units.total,
                    "arie.bedrock.guardrail_cost_usd": float(usage.guardrail_cost_usd),
                },
            )

            if raw_stop == "guardrail_intervened":
                # Unreachable while no guardrailConfig is sent, and kept so a
                # deployment that re-enables the inline guardrail degrades
                # correctly instead of parsing a masked response as an answer.
                # Carries `usage`, so the blocked attempt is still ledgered.
                raise LLMGuardrailInterventionError(
                    "the request was blocked by the Bedrock content guardrail", usage=usage
                )

            try:
                content = response["output"]["message"]["content"]
                text = next(
                    block["text"] for block in content if isinstance(block.get("text"), str)
                )
            except (KeyError, TypeError, StopIteration) as exc:
                raise LLMResponseError(
                    f"Bedrock response had no readable text completion: {type(exc).__name__}"
                ) from exc

            return LLMCompletion(
                text=text,
                usage=usage,
                model=self._config.model,
                provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                finish_reason=stop_reason,
            )
