"""The Decision Copilot Runtime behind :class:`~arie.llm.provider.LLMProvider`.

The same three methods as every other provider, except the Bedrock call happens
in an AgentCore Runtime container instead of in this process. Everything above
this class — budget authorisation, schema validation, the bounded repair retry,
the citation gate, the ledger — is unchanged and unaware.

**Why this exists at all: no AWS credential on Railway.** Invoking AgentCore's
data plane the ordinary way (``bedrock-agentcore InvokeAgentRuntime``) is an AWS
API call and needs SigV4, which means an access key wherever ARIE runs. A
Runtime configured with a **JWT authorizer** is reachable with a bearer token
instead, so Railway holds a Cognito client secret — narrowly scoped to one
resource server, independently rotatable, and not an AWS credential — rather
than an IAM key that could be used against any service the key's policy allows.

**Why the customer's own token is not forwarded.** Ask ARIE's endpoints already
authenticate the caller with a Supabase JWT, and AgentCore could be pointed at
Supabase's JWKS to validate it. That would be a mistake. ``authorize_llm_call``
— the budget gate — runs on Railway, so if an end-user token were sufficient to
reach the Runtime directly, any authenticated customer could spend Bedrock
budget without passing it. Machine-to-machine credentials keep the property that
**only ARIE's server can invoke**, which is what makes budget-before-call still
true once the call crosses a network boundary.

**What this provider is not.** It does not validate the model's output, does not
decide anything, and carries no tenant identity. The Runtime it calls holds no
database connection. A compromised token buys the ability to spend Bedrock money
on prompts of the attacker's choosing — bounded by the guardrail and by AWS
quota — and buys no access to customer data, because there is none behind it.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import httpx

from arie.config import INTELLIGENCE, IntelligenceConfig
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

__all__ = ["PROVIDER_NAME", "AgentCoreProvider"]

_TRACER = get_tracer("arie.llm.agentcore_provider")

PROVIDER_NAME = "agentcore"

_TOKEN_REFRESH_SKEW_SECONDS = 60
"""Refresh a minute early. A token that expires in flight fails the request it
was minted for, and one wasted minute per hour is a rounding error against the
cost of a spurious 401 on a customer's question."""


class AgentCoreProvider(LLMProvider):
    """Calls the Decision Copilot Runtime over HTTPS with a Cognito M2M token.

    Inject ``client`` (an ``httpx.Client``, typically ``MockTransport``-backed)
    and ``token_client`` in tests — the same seam ``DeepSeekProvider`` offers —
    so every path here runs with no network, no AWS account and no Cognito.
    """

    def __init__(
        self,
        *,
        config: IntelligenceConfig | None = None,
        client: httpx.Client | None = None,
        token_client: httpx.Client | None = None,
    ) -> None:
        self._config = config or INTELLIGENCE
        injected = client is not None
        if not injected:
            missing = [
                name
                for name, value in (
                    ("AGENTCORE_RUNTIME_URL", self._config.agentcore_runtime_url),
                    ("AGENTCORE_TOKEN_URL", self._config.agentcore_token_url),
                    ("AGENTCORE_CLIENT_ID", self._config.agentcore_client_id),
                    ("AGENTCORE_CLIENT_SECRET", self._config.agentcore_client_secret),
                )
                if not value
            ]
            if missing:
                raise LLMUnavailableError(
                    "the AgentCore copilot runtime is not configured — missing "
                    f"{', '.join(missing)}. See .env.example. Pass an explicit `client` "
                    "(e.g. in tests) to bypass this check."
                )
        self._client = client or httpx.Client(timeout=self._config.timeout_seconds)
        self._token_client = token_client or self._client
        self._owns_client = client is None
        self._closed = False
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        """The model the *Runtime* is configured to call.

        Held here too because ``model_calls.model`` is written on this side and
        the ledger must name the real model, not the transport. It is asserted
        against the Runtime's own reply on every call — see :meth:`_call` — so a
        Runtime redeployed onto a different model cannot silently be ledgered at
        this one's price.
        """
        return self._config.model

    def close(self) -> None:
        if self._owns_client and not self._closed:
            self._client.close()
        self._closed = True

    # ------------------------------------------------------------------ auth --

    def _access_token(self) -> str:
        """A cached Cognito M2M access token, refreshed shortly before expiry.

        Cached because the token is valid for an hour and minting one per
        question would add a round trip to every customer request for no
        security benefit — the token's scope and lifetime are what bound it,
        not how often it is reissued.
        """
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        try:
            response = self._token_client.post(
                self._config.agentcore_token_url,
                data={
                    "grant_type": "client_credentials",
                    "scope": self._config.agentcore_scope,
                },
                auth=(self._config.agentcore_client_id, self._config.agentcore_client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            # The client secret travels in the Authorization header, which httpx
            # does not render into its own error messages, so this cannot leak
            # it — pinned by a test rather than left as an assumption.
            raise LLMTransportError(f"AgentCore token request failed: {exc}") from exc
        except ValueError as exc:
            raise LLMTransportError(f"AgentCore token response was not JSON: {exc}") from exc

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise LLMTransportError("AgentCore token response contained no access_token")
        expires_in = int(payload.get("expires_in", 3600) or 3600)
        self._token = token
        self._token_expires_at = now + max(0, expires_in - _TOKEN_REFRESH_SKEW_SECONDS)
        return token

    # ------------------------------------------------------------- generation --

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

        body: dict[str, Any] = {
            # Messages cross the wire already fenced by
            # `arie.llm.structured.render_untrusted`. The Runtime selects which
            # fenced spans to screen by label, so forwarding them unchanged is
            # what keeps "screen the question, never the evidence" true across
            # the network boundary.
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_output_tokens": max_output_tokens or self._config.max_output_tokens,
            "temperature": temperature,
        }
        if json_schema is not None:
            body["json_schema"] = json_schema

        with traced(
            _TRACER,
            "llm.agentcore.generate",
            attributes={
                "arie.llm.provider": PROVIDER_NAME,
                "arie.llm.model": self._config.model,
                "arie.llm.structured": json_schema is not None,
            },
        ) as span:
            started = time.monotonic()
            try:
                response = self._client.post(
                    self._config.agentcore_runtime_url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._access_token()}"},
                )
            except httpx.HTTPError as exc:
                raise LLMTransportError(f"AgentCore runtime request failed: {exc}") from exc
            latency_ms = (time.monotonic() - started) * 1000

            if response.status_code >= 500:
                raise LLMTransportError(
                    f"AgentCore runtime returned {response.status_code}"
                )
            if response.status_code in (401, 403):
                # Not a transport blip: the token was rejected. Still a
                # transport error to the caller — nothing was billed and the
                # deterministic path must take over — but named so an operator
                # reading the trace sees an auth problem, not an outage.
                raise LLMTransportError(
                    f"AgentCore runtime rejected the access token ({response.status_code})"
                )
            if response.status_code != 200:
                raise LLMResponseError(
                    f"AgentCore runtime returned {response.status_code}"
                )

            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise LLMResponseError(f"AgentCore response was not JSON: {exc}") from exc

            usage = _usage_from(payload)
            outcome = payload.get("outcome")

            set_attributes(
                span,
                {
                    "arie.llm.prompt_tokens": usage.prompt_tokens,
                    "arie.llm.completion_tokens": usage.completion_tokens,
                    "arie.llm.latency_ms": latency_ms,
                    "arie.agentcore.outcome": str(outcome or ""),
                    "arie.agentcore.request_id": str(payload.get("request_id") or ""),
                    "arie.bedrock.guardrail_action": str(
                        (payload.get("guardrail") or {}).get("action") or ""
                    ),
                    "arie.bedrock.guardrail_text_units": usage.guardrail_text_units,
                    "arie.agentcore.bedrock_latency_ms": float(
                        payload.get("bedrock_latency_ms") or 0.0
                    ),
                },
            )

            if outcome == "guardrail_intervened":
                raise LLMGuardrailInterventionError(
                    "the question was blocked by the Bedrock content guardrail", usage=usage
                )
            if outcome != "ok":
                raise LLMResponseError(
                    f"AgentCore runtime reported outcome {outcome!r}: "
                    f"{payload.get('error', 'no detail')}"
                )

            text = payload.get("text")
            if not isinstance(text, str):
                raise LLMResponseError(
                    f"AgentCore returned a non-string completion of type {type(text).__name__}"
                )

            reported_model = payload.get("model")
            if isinstance(reported_model, str) and reported_model != self._config.model:
                # The ledger prices what this side believes the model was. If
                # the Runtime is serving a different one, every cost row would
                # be wrong at a price nobody chose — fail instead.
                raise LLMResponseError(
                    f"AgentCore runtime served {reported_model!r} but this deployment is "
                    f"configured and priced for {self._config.model!r}"
                )

            finish_reason = payload.get("finish_reason")
            return LLMCompletion(
                text=text,
                usage=usage,
                model=self._config.model,
                provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            )


def _usage_from(payload: dict[str, Any]) -> LLMUsage:
    """Token counts from the Runtime, guardrail **cost** re-derived locally.

    The units are taken from the Runtime because only it saw them; the money is
    computed here, from this deployment's own ``arie.ledger.pricing`` table, so
    a Runtime reporting an unexpected figure cannot write a price into ARIE's
    ledger that ARIE's own price table does not agree with.
    """
    from arie.ledger.pricing import guardrail_cost_usd

    raw = payload.get("usage") or {}
    units = (payload.get("guardrail") or {}).get("units") or {}
    return LLMUsage(
        prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
        completion_tokens=int(raw.get("completion_tokens", 0) or 0),
        guardrail_text_units=int(raw.get("guardrail_text_units", 0) or 0),
        guardrail_cost_usd=guardrail_cost_usd(
            topic_units=int(units.get("topic", 0) or 0),
            content_units=int(units.get("content", 0) or 0),
            sensitive_information_units=int(units.get("sensitive_information", 0) or 0),
            contextual_grounding_units=int(units.get("contextual_grounding", 0) or 0),
            word_units=int(units.get("word", 0) or 0),
        ),
    )
