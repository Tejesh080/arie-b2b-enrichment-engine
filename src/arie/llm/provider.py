"""The LLM transport contract — what "call a model" means to everything above it.

**Why this exists next to ``arie.llm.deepseek`` rather than inside it.** That
module is one narrow M1 task (buying-signal extraction) wired directly to one
vendor, and its docstring commits to never becoming a general facility. M7
needs the general facility: business-intent interpretation, CSV column
inference, lead explanation, research-question proposal, batch summarisation,
copilot answers, feedback interpretation. Rather than widen the narrow module
until its own documentation is false, the general layer is a separate contract
in the same package, and the narrow module is left exactly as it was. Two
clients of one vendor, with different scopes and — see ``arie.llm``'s package
docstring — deliberately different billable-attempt accounting that a shared
client would have quietly changed.

**What a provider may and may not do.** A provider turns messages into text.
It does not choose which provider to call, does not read or write application
state, does not register tools or functions with the vendor API, and does not
decide whether a call is affordable — that last one belongs to
``arie.llm.budget``, which runs *before* a provider is reached. The interface
is deliberately three methods wide so that "the LLM cannot independently
establish facts or take consequential actions" is a property of the type
system rather than a rule someone has to remember.

**Costs are estimated here, billed in the ledger.** :meth:`LLMProvider.
estimate_cost` prices a completion from its token counts using
``arie.ledger.pricing`` — the same table ``model_calls`` rows are priced
against, so a pre-call estimate and the post-call ledger row can never
disagree about what a model costs. It is an estimate only in the sense that
the token counts are not known until the call returns; the *price* is the
same number either way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from arie.ledger.pricing import model_call_cost_usd

__all__ = [
    "LLMCompletion",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMPurpose",
    "LLMResponseError",
    "LLMTransportError",
    "LLMUnavailableError",
    "LLMUsage",
    "MessageRole",
]

MessageRole = Literal["system", "user"]
"""No ``assistant``. Every M7 call is single-turn: instructions, then data,
then one response. A conversation history would be the model reasoning about
its own prior output across turns, which is the agentic shape both this
milestone and ``arie.llm.deepseek``'s docstring rule out. The lead-list and
single-lead copilots (M7.14/M7.15) answer one question per call against
freshly retrieved, tenant-scoped rows; they do not accumulate a transcript."""


class LLMPurpose(StrEnum):
    """The closed vocabulary of ``model_calls.purpose`` values M7 may write.

    Closed, and an enum rather than a string, because ``purpose`` is what
    every cost view groups by: a free-form label would let one feature's spend
    silently split across three spellings, and "which part of the product is
    expensive" is the question the LLM cost ledger exists to answer.

    ``arie.llm.deepseek.PURPOSE`` (``buying_signal_extraction``) is
    deliberately *not* a member. It is M1's, it predates this enum, and rows
    already exist carrying it; adding it here would imply M7 owns a call path
    it does not.
    """

    PROFILE_GENERATION = "profile_generation"
    CSV_MAPPING = "csv_mapping"
    LEAD_EXPLANATION = "lead_explanation"
    RESEARCH_PLANNING = "research_planning"
    BATCH_SUMMARY = "batch_summary"
    COPILOT = "copilot"
    FEEDBACK_ANALYSIS = "feedback_analysis"


@dataclass(frozen=True)
class LLMMessage:
    """One message in a single-turn request."""

    role: MessageRole
    content: str


@dataclass(frozen=True)
class LLMUsage:
    """Token counts as the vendor reported them.

    Zero-filled rather than ``None`` when a vendor omits ``usage``: a call
    that returned a completion was billed for something, and a missing count
    is a reporting gap, not evidence of a free call. Recording zero keeps the
    row (and therefore the fact that the call happened) in the ledger, which
    matters more than the token figure itself — see ``arie.ledger.store``.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True)
class LLMCompletion:
    """One model response: the text, what it cost in tokens, and how long it took."""

    text: str
    usage: LLMUsage
    model: str
    provider: str
    latency_ms: float
    finish_reason: str | None = None
    """The vendor's own stop reason where it reports one. ``"length"`` is the
    interesting value: it means the response was cut off at
    ``IntelligenceConfig.max_output_tokens`` and any JSON in ``text`` is
    almost certainly truncated, so ``arie.llm.structured`` can name that as
    the failure rather than reporting a confusing parse error."""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class LLMProviderError(RuntimeError):
    """Base for every failure a provider raises.

    Callers catch this one type and degrade. That is the whole point: M7's
    failure rule is that an unavailable or malfunctioning model must never
    fail a batch, so every distinct vendor failure mode has to arrive as one
    catchable thing rather than as ``httpx.HTTPError`` in one place and
    ``KeyError`` in another.

    Messages built by subclasses never interpolate a credential. The API key
    reaches the provider as an ``Authorization`` header and is not otherwise
    held in a formattable position — ``tests/unit/test_llm_provider.py`` pins
    that.
    """


class LLMTransportError(LLMProviderError):
    """The request never produced a completion — network error, timeout, non-2xx.

    Nothing was billed, so nothing is ledgered. Distinct from
    :class:`LLMResponseError` for exactly that reason.
    """


class LLMResponseError(LLMProviderError):
    """A completion came back but could not be read as one.

    The vendor billed for it. The caller is expected to ledger the attempt
    anyway where token counts are known — the same rule
    ``arie.llm.deepseek.ExtractionAttempt`` applies, for the same reason.
    """


class LLMUnavailableError(LLMProviderError):
    """No provider is configured, so no call can be attempted.

    Raised at construction (``arie.llm.factory.build_llm_provider``), not at
    call time, so "the feature is off" is distinguishable from "the feature
    tried and failed" — the same distinction ``DeepSeekConfigurationError``
    draws for M1's extractor.
    """


class LLMProvider(ABC):
    """One hosted (or fake) model, behind three methods.

    Implementations must be safe to construct without performing I/O, and
    ``close()`` must be idempotent — the API process builds one provider per
    request scope and the worker builds one per job, so construction cost is
    paid often and teardown may be attempted twice on an error path.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier, written to ``model_calls.provider``."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The model identifier, written to ``model_calls.model``. Must be a
        key of ``arie.ledger.pricing.MODEL_PRICES``."""

    @abstractmethod
    def generate_text(
        self,
        messages: Sequence[LLMMessage],
        *,
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        """One completion, as free text.

        ``temperature`` defaults to 0.0 everywhere in M7 for the reason
        ``arie.llm.deepseek`` gives: these are extraction and interpretation
        tasks against fixed schemas, not creative generation, and a
        reproducible answer is worth more than a varied one — particularly
        for an explanation a customer may re-read after a page refresh.

        Raises :class:`LLMTransportError` if no completion was produced and
        :class:`LLMResponseError` if one was produced but unreadable.
        """

    @abstractmethod
    def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict[str, Any],
        max_output_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        """One completion constrained to a single JSON object.

        ``json_schema`` is the Pydantic-derived JSON Schema of the type the
        caller will validate against. A provider uses it to put the vendor
        into whatever JSON mode it offers and to state the shape in the
        prompt; it is **not** a validation step. Validation happens exactly
        once, in ``arie.llm.structured``, against the Pydantic model itself —
        two places that both "sort of" enforce a schema is how a provider that
        silently accepts an extra key gets shipped.

        ``LLMCompletion.text`` is the raw response body. It is untrusted:
        the caller parses and validates it, and it may be prose, an apology,
        or a JSON document with the wrong shape.
        """

    def estimate_cost(self, usage: LLMUsage) -> Decimal:
        """What ``usage`` costs on this provider's model, in USD.

        Concrete rather than abstract because the arithmetic is a property of
        the *model*, not of the provider implementation, and both the real
        and fake providers must agree with the ledger to the cent. Raises
        ``arie.ledger.pricing.UnknownModelError`` for an unpriced model rather
        than returning zero — see that module for why a free fallback is worse
        than a failure.
        """
        return model_call_cost_usd(
            self.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )

    def close(self) -> None:
        """Release any transport resource. Idempotent; default is a no-op."""
        return None

    def __enter__(self) -> LLMProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
