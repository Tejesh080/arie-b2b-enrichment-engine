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
from arie.llm.agentcore_provider import AgentCoreProvider
from arie.llm.bedrock_provider import BEDROCK_MODELS, DEFAULT_BEDROCK_MODEL, BedrockProvider
from arie.llm.deepseek_provider import DeepSeekProvider
from arie.llm.fake_provider import FAKE_MODEL, FakeLLMProvider
from arie.llm.provider import LLMProvider, LLMUnavailableError

__all__ = [
    "SUPPORTED_PROVIDERS",
    "build_llm_provider",
    "copilot_config",
    "resolve_model",
]

SUPPORTED_PROVIDERS = ("agentcore", "bedrock", "deepseek", "fake", "none")


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


def resolve_bedrock_model(config: IntelligenceConfig, preferred_model: str | None) -> str:
    """The Bedrock model to use, ignoring preferences that belong to another vendor.

    :func:`resolve_model`'s rule — "an organization's preference wins when this
    build can price it" — is wrong here, because ``MODEL_PRICES`` spans
    vendors. An organization that set ``preferred_llm_model='deepseek-chat'``
    long before Bedrock existed would otherwise have that preference honoured
    by a :class:`~arie.llm.bedrock_provider.BedrockProvider`, which would ask
    Bedrock for a DeepSeek model and get an opaque validation error on the
    customer's first question. A preference naming a model of a different
    vendor is not a preference about *this* provider, so it is ignored the same
    way a withdrawn model is.

    Falls back to :attr:`IntelligenceConfig.model` only when that, too, is a
    Bedrock model — otherwise to
    :data:`~arie.llm.bedrock_provider.DEFAULT_BEDROCK_MODEL`, because the
    global ``LLM_MODEL`` describes the *global* provider and under the copilot
    override the two are deliberately different.
    """
    if preferred_model and preferred_model in BEDROCK_MODELS:
        return preferred_model
    if config.model in BEDROCK_MODELS:
        return config.model
    if DEFAULT_BEDROCK_MODEL not in MODEL_PRICES:  # pragma: no cover - guards a bad edit
        raise LLMUnavailableError(
            f"the default Bedrock model {DEFAULT_BEDROCK_MODEL!r} has no price in "
            "arie.ledger.pricing.MODEL_PRICES, so its calls could not be ledgered"
        )
    return DEFAULT_BEDROCK_MODEL


def copilot_config(config: IntelligenceConfig | None = None) -> IntelligenceConfig:
    """The intelligence config Ask ARIE should run under.

    Returns `config` **unchanged** unless ``COPILOT_LLM_PROVIDER`` is set. That
    identity case is the whole design: the Bedrock experiment is scoped to one
    surface, and unsetting one variable has to restore the previous behaviour
    exactly, for every workload including the copilot.

    When the override *is* set, only the provider and model are replaced.
    Budgets, attempt limits, output ceilings and the untrusted-character cap
    are deployment-wide policy, not vendor settings, and an override that
    quietly relaxed them would make the experiment unreviewable.
    """
    settings = config or INTELLIGENCE
    override = settings.copilot_provider.strip().lower()
    if not override:
        return settings
    if override not in SUPPORTED_PROVIDERS:
        raise LLMUnavailableError(
            f"COPILOT_LLM_PROVIDER={settings.copilot_provider!r} is not one of "
            f"{SUPPORTED_PROVIDERS} — refusing to guess which model provider was meant"
        )
    model = settings.copilot_model.strip()
    if not model:
        # Both Bedrock-backed providers default to the Bedrock model, because
        # `agentcore` is Bedrock with a network hop in front of it. Falling back
        # to `settings.model` here would name the *global* provider's model —
        # `deepseek-chat` on a default deployment — in a config that is about to
        # build a Bedrock client. `resolve_bedrock_model` would still correct it
        # downstream, but a config object that misreports its own model is a
        # trap for the next reader.
        model = (
            DEFAULT_BEDROCK_MODEL
            if override in ("bedrock", "agentcore")
            else settings.model
        )
    return dataclasses.replace(settings, provider=override, model=model)


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

    if provider == "bedrock":
        model = resolve_bedrock_model(settings, preferred_model)
        return BedrockProvider(config=dataclasses.replace(settings, model=model))

    if provider == "agentcore":
        # Same model resolution as `bedrock`: the Runtime calls Bedrock, so the
        # model must still be one this build can price, and an organization's
        # preference for another vendor's model is still ignored rather than
        # forwarded to a service that would reject it.
        model = resolve_bedrock_model(settings, preferred_model)
        return AgentCoreProvider(config=dataclasses.replace(settings, model=model))

    raise LLMUnavailableError(
        f"LLM_PROVIDER={settings.provider!r} is not one of {SUPPORTED_PROVIDERS} — "
        "refusing to guess which model provider was meant"
    )
