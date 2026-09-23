"""ARIE Decision Copilot — the Bedrock model-call layer, hosted on AgentCore Runtime.

**This service explains ARIE decisions. It does not make them, and it cannot.**
It holds no database connection, no tenant table, no scoring code and no
policy. Structurally it is one thing: a remote
:class:`~arie.llm.provider.LLMProvider` — messages in, text out — with the
Bedrock guardrail screening the customer-authored span on the way in.

**What stays on Railway, and why that list is the whole design.** Tenant
authorization, evidence retrieval, budget authorization (``authorize_llm_call``),
the citation/evidence-membership gate (``_sanitize``), the cost ledger, scoring,
routing, identity validation and every deterministic decision remain in the ARIE
API. This container is reached *after* a request has already been authorized and
budgeted, and its answer is gated *afterwards* by code it never sees. Moving any
of that here would mean re-earning guarantees ARIE already holds.

**Why it returns raw text rather than a validated object.** ``arie.llm.structured``
states that validation happens exactly once, against the Pydantic model, because
"two places that both sort of enforce a schema is how a provider that silently
accepts an extra key gets shipped". The real ``LeadListQueryPlan`` /
``_CompareResponse`` classes — with their ``extra="forbid"``, their enums and
their field bounds — live on Railway; only their JSON Schema crosses the wire,
and a schema is a weaker statement than the class. So this service parses JSON
(a convenience, in ``parsed``) but never adjudicates shape, and the caller
validates the authoritative ``text``. The bounded repair retry stays on Railway
with the validator that decides a repair is needed.

**The guardrail scopes itself.** ``BedrockProvider`` selects which fenced spans
to screen by label (``BEDROCK_GUARDED_LABELS``, default ``question``), reading
the fences ``arie.llm.structured.render_untrusted`` already wrote into the user
message. Passing messages through unchanged is therefore what preserves the
"screen the customer's question, never ARIE's evidence" property — this file
adds no scoping logic of its own and must not.

Container contract (AgentCore HTTP protocol): ``GET /ping``, ``POST
/invocations``, port 8080, ARM64.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from arie.config import INTELLIGENCE
from arie.ledger.pricing import model_call_cost_usd
from arie.llm.bedrock_provider import BedrockProvider
from arie.llm.provider import (
    LLMGuardrailInterventionError,
    LLMMessage,
    LLMProviderError,
    LLMTransportError,
)

__all__ = ["INTELLIGENCE", "SERVICE_VERSION", "app", "invocations", "ping", "provider"]
"""``INTELLIGENCE`` is listed because it is this module's configuration seam:
``tests/unit/test_copilot_runtime.py`` rebinds it to exercise the handler
against a known guardrail and model, the same way a real deployment sets the
environment before import."""

SERVICE_VERSION = "0.1.0"

app = FastAPI(title="ARIE Decision Copilot Runtime", version=SERVICE_VERSION)

_client: Any | None = None
_client_lock = threading.Lock()


def _bedrock_client() -> Any:
    """One boto3 client per container, built lazily and shared.

    Lazily because AgentCore health-checks ``/ping`` before any invocation, and
    a container that failed at import because boto3 could not resolve
    credentials would report unhealthy with no useful log line. Shared because
    botocore clients are thread-safe and reusing their connection pool is most
    of the difference between a warm call and a cold one.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3

                _client = boto3.client(
                    "bedrock-runtime", region_name=INTELLIGENCE.bedrock_region
                )
    return _client


def provider() -> BedrockProvider:
    """A fresh provider per request, over the shared client.

    ``BedrockProvider.last_detail`` is per-call mutable state — the request id,
    guardrail action and unit counts of the most recent call — and its own
    docstring says it is not thread-safe. A single provider shared across a
    concurrent server would let two in-flight requests overwrite each other's
    detail and report the wrong request id, which is exactly the bug the local
    battery caught before this was split.

    The wrapper is a few attributes; the expensive part (the botocore client,
    its connection pool and credential resolution) is shared, so per-request
    construction costs effectively nothing.
    """
    return BedrockProvider(config=INTELLIGENCE, client=_bedrock_client())


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user"]
    """No ``assistant``. Mirrors ``arie.llm.provider.MessageRole`` exactly: every
    call is single-turn, and accepting a transcript here would let a caller
    build the agentic shape ARIE's LLM layer is designed to exclude."""

    content: Annotated[str, Field(min_length=1, max_length=200_000)]


class InvocationRequest(BaseModel):
    """The wire form of ``LLMProvider.generate_structured``.

    Deliberately a mirror of that signature rather than a bespoke "copilot
    request". The caller has already rendered and fenced its untrusted blocks
    (``render_untrusted``, which enforces the character cap next to the budget
    estimate that prices it); re-rendering here would move that cap away from
    the code that bills for it.
    """

    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[Message], Field(min_length=1, max_length=16)]
    json_schema: dict[str, Any] | None = None
    max_output_tokens: Annotated[int, Field(ge=1, le=8192)] = 800
    temperature: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


@app.get("/ping")
def ping() -> dict[str, str]:
    """AgentCore's health probe for every protocol but MCP."""
    return {"status": "Healthy"}


@app.post("/invocations")
def invocations(request: InvocationRequest) -> JSONResponse:
    """One Bedrock call. Never raises past this handler.

    Every outcome is a 200 with an ``outcome`` discriminator, including a
    guardrail block, because the caller has to be able to **ledger what was
    spent** in each case. A bare 4xx would throw away the guardrail units AWS
    charged for blocking the request, and an unbilled block is the most
    expensive kind of invisible. Only a genuine transport failure — where
    nothing was billed and there is nothing to record — answers non-2xx.
    """
    started = time.monotonic()
    messages = [LLMMessage(role=m.role, content=m.content) for m in request.messages]
    # Bound once. `p.last_detail` belongs to *this* request, and calling
    # provider() again would hand back a different instance with no detail.
    p = provider()

    try:
        if request.json_schema is None:
            completion = p.generate_text(
                messages,
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
            )
        else:
            completion = p.generate_structured(
                messages,
                json_schema=request.json_schema,
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
            )
    except LLMGuardrailInterventionError as exc:
        detail = p.last_detail
        usage = exc.usage
        return JSONResponse(
            status_code=200,
            content={
                "outcome": "guardrail_intervened",
                "error": str(exc),
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "guardrail_text_units": usage.guardrail_text_units if usage else 0,
                },
                "guardrail": _guardrail_block(detail, action="GUARDRAIL_INTERVENED"),
                "cost": _cost_block(Decimal(0), usage.guardrail_cost_usd if usage else Decimal(0)),
                "request_id": detail.request_id if detail else None,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "service_version": SERVICE_VERSION,
            },
        )
    except LLMTransportError as exc:
        # Nothing was billed, so there is nothing to ledger. 502 so the caller's
        # provider raises LLMTransportError and the deterministic path takes over.
        return JSONResponse(
            status_code=502,
            content={"outcome": "transport_error", "error": str(exc)},
        )
    except LLMProviderError as exc:
        return JSONResponse(
            status_code=502,
            content={"outcome": "provider_error", "error": str(exc)},
        )

    detail = p.last_detail
    model_cost = model_call_cost_usd(
        completion.model,
        prompt_tokens=completion.usage.prompt_tokens,
        completion_tokens=completion.usage.completion_tokens,
    )
    return JSONResponse(
        status_code=200,
        content={
            "outcome": "ok",
            # `text` is authoritative and is what the caller validates against
            # its own Pydantic model. `parsed` is a convenience for humans and
            # smoke tests; it asserts nothing about shape.
            "text": completion.text,
            "parsed": _try_parse(completion.text),
            "model": completion.model,
            "provider": completion.provider,
            "finish_reason": completion.finish_reason,
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "guardrail_text_units": completion.usage.guardrail_text_units,
            },
            "guardrail": _guardrail_block(detail, action=detail.guardrail_action if detail else "NONE"),
            "cost": _cost_block(model_cost, completion.usage.guardrail_cost_usd),
            "request_id": detail.request_id if detail else None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "bedrock_latency_ms": round(completion.latency_ms, 1),
            "service_version": SERVICE_VERSION,
        },
    )


def _guardrail_block(detail: Any, *, action: str) -> dict[str, Any]:
    units = detail.units if detail else None
    return {
        "id": INTELLIGENCE.bedrock_guardrail_id or None,
        "version": INTELLIGENCE.bedrock_guardrail_version,
        "action": action,
        "applied": bool(detail.guardrail_applied) if detail else False,
        "guarded_labels": list(detail.guarded_labels) if detail else [],
        "units": {
            "topic": units.topic if units else 0,
            "content": units.content if units else 0,
            "sensitive_information": units.sensitive_information if units else 0,
            "contextual_grounding": units.contextual_grounding if units else 0,
            "word": units.word if units else 0,
            "total": units.total if units else 0,
        },
    }


def _cost_block(model_usd: Decimal, guardrail_usd: Decimal) -> dict[str, str]:
    """Strings, not floats. These are summed into ``model_calls`` on the caller
    and ``arie.ledger.pricing`` is Decimal end to end for the reason that
    module gives — JSON floats would reintroduce exactly the binary rounding it
    exists to keep out of money."""
    return {
        "model_usd": str(model_usd),
        "guardrail_usd": str(guardrail_usd),
        "total_usd": str(model_usd + guardrail_usd),
    }


def _try_parse(text: str) -> Any:
    import json

    from arie.llm.structured import strip_code_fence

    try:
        return json.loads(strip_code_fence(text))
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
