"""Dataset record types.

The central structural choice: a lead carries a **latent truth vector** that the
policy never sees, and separately a set of **provider observations** that are
noisy, incomplete views of it. The oracle decision is computed from latent truth
alone.

Without that separation, "agreement with the oracle" would be circular — the
oracle would be defined by the same observations the policy reads.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from arie.core.types import Decision, ProviderStatus

DifficultyBand = Literal["easy", "medium", "hard"]
ValueTier = Literal["small", "medium", "large", "whale"]
Split = Literal["calibration", "test"]

GENERATOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class LatentCompany:
    """Ground truth about a company. Never visible to any policy."""

    company_id: str
    canonical_domain: str
    legal_name: str
    employee_count: int
    industry: str
    recent_trigger_event: str | None
    disqualifying_flag: bool

    # Intent is an *account*-level property, not a personal one — intent vendors
    # sell surge signals about the company, not the individual. Modelling it here
    # keeps company-scoped providers coherent (a company-scoped observation of a
    # person-scoped truth would be meaningless) and makes the most expensive
    # field in the catalogue company-cacheable, which is what gives the
    # company-level cache its cost leverage.
    buying_intent: float

    # Single per-company draw in [0, 1] that degrades every provider's coverage.
    # This is the mechanism that makes provider misses *correlated* — real
    # vendors fail together on the same thin, messy accounts.
    obscurity: float

    # Alternate surface forms used by the ambiguous-identity subset, e.g.
    # "Acme Inc" / "Acme Corporation". Makes deterministic-match failure
    # measurable instead of assumed.
    name_variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class LatentPerson:
    """Ground truth about a contact."""

    person_id: str
    company_id: str
    full_name: str
    email: str
    title_seniority: str
    title_function: str


@dataclass(frozen=True)
class ProviderObservation:
    """One provider's frozen response for one lead.

    Pre-generated so that every strategy in the benchmark faces identical
    provider behaviour. Sampling per run would let luck differ between
    strategies and contaminate the comparison.
    """

    provider: str
    status: ProviderStatus
    fields: dict[str, Any]
    latency_ms: float
    cost_usd: float


@dataclass(frozen=True)
class EvalLead:
    """One benchmark record: hidden truth, visible observations, oracle answer."""

    eval_lead_id: str
    company: LatentCompany
    person: LatentPerson

    observations: dict[str, ProviderObservation]

    oracle_decision: Decision
    oracle_score: float
    oracle_components: dict[str, float]

    difficulty_band: DifficultyBand
    value_tier: ValueTier
    split: Split

    # MEASURED: cheap-tier evidence alone yields a different decision than the
    # oracle. Verified per lead after generation, not assumed. The size of this
    # subset bounds the maximum achievable gain from adaptive enrichment, so it
    # is reported rather than left implicit.
    cheap_misleads: bool

    # CONSTRUCTED: this lead was *deliberately* built to mislead cheap evidence
    # (hidden blocker, or expensive-only intent that flips the answer).
    #
    # Kept separate from `cheap_misleads` on purpose. Most disagreement arises
    # naturally because the cheap tier cannot see buying intent at all;
    # reporting that as "adversarial by design" would overstate how much of the
    # dataset's difficulty was engineered.
    constructed_adversarial: bool = False

    generator_version: str = GENERATOR_VERSION
    seed: int = 0

    def to_json(self) -> dict[str, Any]:
        """Stable, sorted-key dict for reproducible JSONL output."""
        raw = asdict(self)
        raw["oracle_decision"] = str(self.oracle_decision)
        raw["observations"] = {
            name: {**asdict(obs), "status": str(obs.status)}
            for name, obs in sorted(self.observations.items())
        }
        return raw


@dataclass(frozen=True)
class DatasetManifest:
    """Everything needed to prove a dataset was generated as claimed."""

    generator_version: str
    seed: int
    rules_version: str
    n_leads: int
    n_companies: int
    content_sha256: str

    band_counts: dict[str, int] = field(default_factory=dict)
    split_counts: dict[str, int] = field(default_factory=dict)
    decision_counts: dict[str, int] = field(default_factory=dict)
    value_tier_counts: dict[str, int] = field(default_factory=dict)
    cheap_misleads_count: int = 0
    constructed_adversarial_count: int = 0
    ambiguous_identity_count: int = 0
    contacts_per_company_by_band: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
