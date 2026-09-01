"""Choosing which :class:`~arie.llm.provider.LLMProvider` to construct.

Small on purpose. The rule that matters is the one about what *not* to do on
an unrecognised or unconfigured selection: raise
:class:`~arie.llm.provider.LLMUnavailableError`, never quietly substitute
another provider. A typo'd ``LLM_PROVIDER`` that silently resolved to DeepSeek
would spend real money on a deployment that believed it had none configured,
and a keyless deployment that silently resolved to the fake would serve
customers confident, deterministic, entirely made-up answers. Both failures
are worse than not starting.

Nor does it ever reach for a *customer's* credential. Organizations do supply
their own enrichment-provider keys through the Vault
(``arie.live.provider_availability``); there is no equivalent for models, and
inventing one here — falling back to a BYOK key because the deployment's own
was missing — would spend a customer's money to cover an operator's
misconfiguration.
"""

from __future__ import annotations

import dataclasses

from arie.config import INTELLIGENCE, IntelligenceConfig
from arie.ledger.pricing import MODEL_PRICES
from arie.llm.deepseek_provider import DeepSeekProvider
from arie.llm.fake_provider import FAKE_MODEL, FakeLLMProvider
from arie.llm.provider import LLMProvider, LLMUnavailableError

__all__ = ["SUPPORTED_PROVIDERS", "build_llm_provider", "resolve_model"]

SUPPORTED_PROVIDERS = ("deepseek", "fake", "none")


def resolve_model(config: IntelligenceConfig, preferred_model: str | None) -> str:
    """The model to use, given a deployment default and an organization's preference.

    An organization's ``preferred_llm_model`` wins when it names a model this
    build knows how to price, and is ignored otherwise — a model withdrawn from
    ``arie.ledger.pricing.MODEL_PRICES`` between the setting being saved and
    being used should degrade that organization to the deployment default, not
    fail its batch. The deployment default is *not* forgiving in the same way:
    an unpriced ``LLM_MODEL`` is an operator error with no sensible fallback,
    and it raises.
    """
    if preferred_model and preferred_model in MODEL_PRICES:
        return preferred_model
    if config.model not in MODEL_PRICES:
        raise LLMUnavailableError(
            f"LLM_MODEL={config.model!r} has no price in arie.ledger.pricing.MODEL_PRICES, "
            "so its calls could not be ledgered — add a price rather than letting them "
            "record as free"
        )
    return config.model


def build_llm_provider(
    *,
    config: IntelligenceConfig | None = None,
    preferred_model: str | None = None,
) -> LLMProvider:
    """Construct the configured provider, or raise :class:`LLMUnavailableError`.

    Raises rather than returning ``None`` because there is exactly one correct
    caller-side handling — degrade to deterministic behaviour — and a ``None``
    return invites a caller to forget the check and get an ``AttributeError``
    three frames later. ``arie.llm.service`` catches this and turns it into a
    :attr:`~arie.llm.budget.LLMBudgetReason.PROVIDER_UNAVAILABLE` result, which
    is the shape every M7 feature branches on.
    """
    settings = config or INTELLIGENCE
    provider = settings.provider.lower()

    if provider == "none":
        raise LLMUnavailableError(
            "LLM_PROVIDER=none — the intelligence layer is switched off for this "
            "deployment and every AI-assisted feature will fall back to its "
            "deterministic behaviour"
        )

    if provider == "fake":
        # The fake is priced (`fake-llm`, $0.00) rather than borrowing whatever
        # LLM_MODEL happens to say: a test asserting a cost figure should be
        # asserting against the fake's own price, not against DeepSeek's.
        return FakeLLMProvider(model_name=FAKE_MODEL)

    if provider == "deepseek":
        model = resolve_model(settings, preferred_model)
        return DeepSeekProvider(config=dataclasses.replace(settings, model=model))

    raise LLMUnavailableError(
        f"LLM_PROVIDER={settings.provider!r} is not one of {SUPPORTED_PROVIDERS} — "
        "refusing to guess which model provider was meant"
    )
