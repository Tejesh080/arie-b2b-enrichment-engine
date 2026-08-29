"""The DeepSeek-backed extractor. One HTTP call, JSON-mode, Pydantic-validated.

**Scope, stated the way every other M1 module states it:** this calls DeepSeek
exactly once per attempt with a fixed prompt and reads back one JSON object. It
never registers tools or functions with the API — there is nothing in the
request body a model could invoke even if it tried — never chains calls based
on the model's own output, never selects a provider, and never touches
`leads.status` or any other application state. `arie.llm.schema.ExtractedSignal`
is the entire surface: if it isn't a field on that model, this module cannot
produce it. That is what "the LLM may extract signals but must not choose
providers, change lead state, or call tools" means as code rather than as a
sentence in a document.

**Retries are stateless, not agentic.** A failed attempt (network error, or a
response that fails schema validation) is retried by sending the *same*
request again, up to `LLMConfig.max_attempts` times — ordinary API-retry
hygiene, the same shape as `arie.jobs.backoff`. There is deliberately no
multi-turn "here is what you got wrong, try again" correction loop: that would
be the model reasoning about its own prior output across turns, which starts
to look like the agentic behaviour this step is scoped to exclude.

**One fixed model, not a cascade.** `LLMConfig.model` defaults to
`deepseek-chat` and nothing here ever escalates to `deepseek-reasoner` or
chooses between models based on the input. That would be multi-model routing,
named explicitly as out of scope for this step.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from arie.config import LLM, LLMConfig
from arie.ledger.store import LedgerWrite, PostgresCostLedger
from arie.llm.schema import ExtractedSignal
from arie.observability.tracing import get_tracer, record_error, set_attributes, traced

_TRACER = get_tracer("arie.llm.deepseek")

PURPOSE = "buying_signal_extraction"
"""Fixed `model_calls.purpose` value — one narrow task, one label."""

_SYSTEM_PROMPT = f"""You extract buying-intent signals from a short piece of text \
a sales lead submitted: a contact-form message, a demo-request note, or meeting \
notes. You do not make decisions, you do not recommend actions, and you have no \
tools available to you. You only extract what the text itself states or clearly \
implies.

Respond with a single JSON object matching this JSON Schema exactly. No prose, \
no markdown, no code fences — the JSON object and nothing else.

{json.dumps(ExtractedSignal.model_json_schema(), indent=2)}

If the text gives no support for a field, use its default (false, null, or an \
empty description as the schema specifies). Never invent a trigger event or \
buying intent that the text does not support."""


class DeepSeekConfigurationError(RuntimeError):
    """Raised at construction, not at call time, if no API key is configured.

    Failing here — before any request is attempted — is what lets a caller
    tell "the feature is off" apart from "the feature tried and failed",
    which matters for `bench/llm_signal_eval.py`'s optional live-run path.
    """


@dataclass(frozen=True)
class ExtractionAttempt:
    """One HTTP call, whether or not its content passed schema validation.

    `request_succeeded` is the ledgering boundary: DeepSeek bills for a
    completion it returned even if that completion doesn't parse as the schema
    this module demands, so `request_succeeded=True` attempts are billable
    (and recorded) regardless of `validation_error`. A `request_succeeded=False`
    attempt (network error, timeout, non-2xx) never reached a billable
    completion and is never ledgered.
    """

    request_succeeded: bool
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    validation_error: str | None


@dataclass(frozen=True)
class ExtractionOutcome:
    """Result of one `extract_signal` call, across every attempt it took."""

    model: str
    signal: ExtractedSignal | None
    """None only if every attempt failed — either the request itself, or
    schema validation, on every try."""
    attempts: tuple[ExtractionAttempt, ...]

    @property
    def succeeded(self) -> bool:
        return self.signal is not None

    @property
    def total_prompt_tokens(self) -> int:
        return sum(a.prompt_tokens for a in self.attempts)

    @property
    def total_completion_tokens(self) -> int:
        return sum(a.completion_tokens for a in self.attempts)

    @property
    def total_latency_ms(self) -> float:
        return sum(a.latency_ms for a in self.attempts)


class DeepSeekSignalExtractor:
    """Wraps one `httpx.Client` bound to the DeepSeek chat-completions endpoint.

    Inject `client` in tests with an `httpx.MockTransport`-backed client — see
    `tests/unit/test_llm_deepseek_client.py` — so nothing here ever needs a
    live API key or network access to be exercised deterministically.
    """

    def __init__(
        self, *, config: LLMConfig | None = None, client: httpx.Client | None = None
    ) -> None:
        self._config = config or LLM
        if client is None and not self._config.configured:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_API_KEY is not set — see .env.example. "
                "Pass an explicit `client` (e.g. in tests) to bypass this check."
            )
        self._client = client or httpx.Client(
            base_url=self._config.deepseek_base_url,
            headers={"Authorization": f"Bearer {self._config.deepseek_api_key}"},
            timeout=self._config.timeout_seconds,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DeepSeekSignalExtractor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def extract_signal(self, text: str) -> ExtractionOutcome:
        """Extract one `ExtractedSignal` from `text`, retrying transient failures.

        Traced as a single `llm.extract_signal` span covering every attempt —
        an operator asking "why did this take 4 seconds" should see one span
        with an attempt count, not have to reconstruct a retry sequence from
        several unrelated ones.
        """
        with traced(
            _TRACER, "llm.extract_signal", attributes={"arie.llm.model": self._config.model}
        ) as span:
            attempts: list[ExtractionAttempt] = []
            signal: ExtractedSignal | None = None

            for _ in range(self._config.max_attempts):
                attempt, parsed = self._one_attempt(text)
                attempts.append(attempt)
                if parsed is not None:
                    signal = parsed
                    break

            set_attributes(
                span,
                {
                    "arie.llm.attempts": len(attempts),
                    "arie.llm.succeeded": signal is not None,
                    "arie.llm.total_prompt_tokens": sum(a.prompt_tokens for a in attempts),
                    "arie.llm.total_completion_tokens": sum(a.completion_tokens for a in attempts),
                },
            )
            if signal is None:
                # Every attempt failed. This does not raise — a caller must be
                # able to branch on ExtractionOutcome.succeeded without a
                # try/except — but the span still has to come out ERROR, the
                # same reasoning arie.jobs.worker applies to a job that
                # exhausts its retries: a failure that doesn't propagate is
                # still a failure, and a span that looks successful because
                # nothing raised would hide it from every "show me the
                # errors" trace query.
                record_error(span, attempts[-1].validation_error or "unknown")

            return ExtractionOutcome(
                model=self._config.model, signal=signal, attempts=tuple(attempts)
            )

    def _one_attempt(self, text: str) -> tuple[ExtractionAttempt, ExtractedSignal | None]:
        started = time.monotonic()
        try:
            response = self._client.post(
                "/chat/completions",
                json={
                    "model": self._config.model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "response_format": {"type": "json_object"},
                    # Zero, not just low: this is extraction against a fixed
                    # schema, not creative generation, and the eval corpus's
                    # comparison against a *deterministic* baseline is only a
                    # fair comparison if the LLM side is as close to
                    # deterministic as the API allows.
                    "temperature": 0.0,
                },
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            return (
                ExtractionAttempt(
                    request_succeeded=False,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.monotonic() - started) * 1000,
                    validation_error=f"request failed: {exc}",
                ),
                None,
            )
        except ValueError as exc:  # response.json() on a non-JSON body
            return (
                ExtractionAttempt(
                    request_succeeded=False,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.monotonic() - started) * 1000,
                    validation_error=f"response was not valid JSON: {exc}",
                ),
                None,
            )

        latency_ms = (time.monotonic() - started) * 1000
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = ExtractedSignal.model_validate_json(content)
        except (KeyError, IndexError, TypeError, ValidationError, ValueError) as exc:
            # A billable completion that didn't match the schema. Still
            # request_succeeded=True: DeepSeek returned and billed for this
            # one, so it still belongs in the ledger.
            return (
                ExtractionAttempt(
                    request_succeeded=True,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    validation_error=str(exc),
                ),
                None,
            )

        return (
            ExtractionAttempt(
                request_succeeded=True,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                validation_error=None,
            ),
            parsed,
        )


def record_extraction_cost(
    ledger: PostgresCostLedger,
    outcome: ExtractionOutcome,
    *,
    organization_id: UUID,
    lead_id: UUID | None = None,
    idempotency_key_base: str | None = None,
) -> tuple[LedgerWrite, ...]:
    """Record every billed attempt in `outcome` as its own `model_calls` row.

    Every `request_succeeded=True` attempt is recorded, whether or not it
    passed schema validation — a retry DeepSeek billed for costs money whether
    or not the response was usable, and dropping it would make this feature's
    true cost invisible in `v_lead_cost`. Request-level failures (no
    completion returned) cost nothing and are not recorded, matching what was
    actually billed — see `arie.ledger.store`'s own module docstring for why
    the ledger commits independently of whatever transaction the caller is in.

    `idempotency_key_base` is suffixed with the attempt index so retries of
    *this same* extraction call never double-record if `record_extraction_cost`
    itself were ever called twice for one outcome (e.g. a crashed-and-resumed
    caller) — each attempt keeps a stable, distinct key across such a replay.
    """
    writes: list[LedgerWrite] = []
    for index, attempt in enumerate(outcome.attempts):
        if not attempt.request_succeeded:
            continue
        key = f"{idempotency_key_base}:attempt{index}" if idempotency_key_base else None
        writes.append(
            ledger.record_model_call(
                model=outcome.model,
                purpose=PURPOSE,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                organization_id=organization_id,
                lead_id=lead_id,
                latency_ms=attempt.latency_ms,
                idempotency_key=key,
            )
        )
    return tuple(writes)
