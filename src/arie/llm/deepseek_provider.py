"""DeepSeek behind :class:`~arie.llm.provider.LLMProvider`.

One HTTP call per :meth:`generate_text`/:meth:`generate_structured`. No
retries here: retry policy belongs to whoever knows what a retry costs and
whether the request is worth repeating, which is ``arie.llm.structured``
(bounded at ``IntelligenceConfig.max_attempts``). A provider that retried on
its own would make that budget a lie by multiplying every attempt the layer
above thought it was bounding.

**The vendor API shape**, as of DeepSeek's OpenAI-compatible
``/chat/completions``: ``response_format={"type": "json_object"}`` is the JSON
mode, and it requires the word "json" to appear somewhere in the prompt or the
API rejects the request outright. :meth:`generate_structured` therefore appends
the schema as a system message rather than relying on the flag alone — which
is also what actually gets a correctly *shaped* object back, since JSON mode
only guarantees syntactically valid JSON, never a particular schema.

**Why this file does not import ``arie.llm.deepseek``, and is not imported by
it.** The two are independent clients of the same vendor. That is duplication,
and it is deliberate — see ``arie.llm``'s package docstring for the accounting
difference that makes folding the M1 extractor onto this provider a change to
what a frozen benchmark counts as billed. A vendor contract change means
editing both files; ``tests/unit/test_llm_deepseek_provider.py`` and
``tests/unit/test_llm_deepseek_client.py`` both pin the request body, so
neither can be updated without the other failing loudly.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import httpx

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.llm.provider import (
    LLMCompletion,
    LLMMessage,
    LLMProvider,
    LLMResponseError,
    LLMTransportError,
    LLMUnavailableError,
    LLMUsage,
)
from arie.observability.tracing import get_tracer, set_attributes, traced

__all__ = ["PROVIDER_NAME", "DeepSeekProvider"]

_TRACER = get_tracer("arie.llm.deepseek_provider")

PROVIDER_NAME = "deepseek"

_JSON_INSTRUCTION = """Respond with a single JSON object matching this JSON Schema exactly. \
Output the JSON object and nothing else — no prose, no explanation, no markdown code fences.

{schema}"""
"""Sent as a system message alongside the JSON-mode flag. The literal word
"json" appearing here is load-bearing: DeepSeek rejects a request that sets
``response_format={"type": "json_object"}`` without it."""


class DeepSeekProvider(LLMProvider):
    """A pooled ``httpx.Client`` bound to DeepSeek's chat-completions endpoint.

    Inject ``client`` in tests with an ``httpx.MockTransport``-backed client —
    the same seam ``arie.llm.deepseek.DeepSeekSignalExtractor`` has always
    offered — so every path here is exercisable with no key and no network.
    Passing a client also bypasses the configured-credential check, which is
    what makes ``tests/unit/test_llm_deepseek_provider.py`` runnable on a
    machine that has never seen ``DEEPSEEK_API_KEY``.
    """

    def __init__(
        self,
        *,
        config: IntelligenceConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config or INTELLIGENCE
        if client is None and not self._config.api_key:
            raise LLMUnavailableError(
                "no LLM API key is configured (set LLM_API_KEY, or DEEPSEEK_API_KEY) — "
                "see .env.example. Pass an explicit `client` (e.g. in tests) to bypass "
                "this check."
            )
        self._client = client or httpx.Client(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {self._config.api_key}"},
            timeout=self._config.timeout_seconds,
        )
        self._owns_client = client is None
        self._closed = False

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._config.model

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._client.close()
        self._closed = True

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

        payload_messages = [{"role": m.role, "content": m.content} for m in messages]
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens or self._config.max_output_tokens,
        }
        if json_schema is not None:
            # Prepended, not appended: the schema is an instruction, and this
            # keeps every instruction ahead of the untrusted-data blocks the
            # caller has already fenced into the user message. Ordering is not
            # a security control on its own — the fencing in
            # `arie.llm.structured` is — but putting data before instructions
            # gives an injection attempt the last word for no benefit.
            payload_messages.insert(
                0,
                {
                    "role": "system",
                    "content": _JSON_INSTRUCTION.format(
                        schema=json.dumps(json_schema, indent=2, sort_keys=True)
                    ),
                },
            )
            body["response_format"] = {"type": "json_object"}

        with traced(
            _TRACER,
            "llm.deepseek.generate",
            attributes={
                "arie.llm.provider": PROVIDER_NAME,
                "arie.llm.model": self._config.model,
                "arie.llm.structured": json_schema is not None,
            },
        ) as span:
            started = time.monotonic()
            try:
                response = self._client.post("/chat/completions", json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # `exc` is httpx's own message (URL, status, timeout kind). The
                # credential lives in a header httpx does not render into it,
                # so this cannot leak the key — pinned by a test rather than
                # left as an assumption.
                raise LLMTransportError(f"DeepSeek request failed: {exc}") from exc

            latency_ms = (time.monotonic() - started) * 1000

            try:
                parsed: dict[str, Any] = response.json()
            except ValueError as exc:
                raise LLMResponseError(f"DeepSeek response was not valid JSON: {exc}") from exc

            usage_raw = parsed.get("usage") or {}
            usage = LLMUsage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            )

            try:
                choice = parsed["choices"][0]
                text = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMResponseError(
                    f"DeepSeek response had no readable completion: {exc}"
                ) from exc
            if not isinstance(text, str):
                raise LLMResponseError(
                    f"DeepSeek returned a non-string completion of type {type(text).__name__}"
                )

            set_attributes(
                span,
                {
                    "arie.llm.prompt_tokens": usage.prompt_tokens,
                    "arie.llm.completion_tokens": usage.completion_tokens,
                    "arie.llm.finish_reason": finish_reason or "",
                    "arie.llm.latency_ms": latency_ms,
                },
            )
            return LLMCompletion(
                text=text,
                usage=usage,
                model=self._config.model,
                provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            )
