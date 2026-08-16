"""Seeded generator for the evaluation dataset.

Design commitments, each defending against a specific way a synthetic benchmark
can quietly become meaningless:

1. **Latent truth is separate from observations.** The oracle reads truth; the
   policy reads noisy partial views of it.
2. **Misses correlate.** One per-company obscurity draw degrades every
   provider's coverage together. Independent misses would make "try the next
   provider" always work, so the stopping decision would never be tested.
3. **Splits are disjoint by company, structurally.** A company is generated
   *into* one split; it cannot appear in both. Company-level evidence learned
   during calibration therefore cannot leak into the test set.
4. **Difficulty is constructed, not labelled after the fact.** Bands are
   generation directives, so proportions are controlled rather than hoped for.
5. **Everything derives from one seed** via stable hashing, so any record can be
   regenerated independently and byte-identically.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any

from arie.core.types import Decision, ProviderStatus
from arie.evalgen.schema import (
    GENERATOR_VERSION,
    DatasetManifest,
    DifficultyBand,
    EvalLead,
    LatentCompany,
    LatentPerson,
    ProviderObservation,
    Split,
    ValueTier,
)
from arie.providers.catalog import CATALOG, CHEAP_TIER, ProviderSpec
from arie.scoring.merge import merge_observations
from arie.scoring.rules import (
    QUALIFY_THRESHOLD,
    REJECT_THRESHOLD,
    RULES_VERSION,
    decide,
    score_facts,
)

# --- generation targets ------------------------------------------------------

# The calibration split feeds the confidence model, the conformal threshold, and
# the waterfall gate. At 150 leads the threshold was badly under-determined —
# tau ranged 0.77 to 1.01 across seeds, with one run degenerating to
# accept-nothing. Enlarging it is the cheapest available fix: it costs
# generation time only, and the test split is unaffected by construction.
DEFAULT_CALIBRATION_LEADS = 600
DEFAULT_TEST_LEADS = 300

# Per-split namespaces. Company ids and name blocks must not overlap, or a
# calibration company could collide with a test company on its domain — which
# keys the observation store.
SPLIT_ID_PREFIX: dict[str, str] = {"calibration": "k", "test": "t"}
SPLIT_NAME_BLOCK: dict[str, int] = {"calibration": 0, "test": 500}

BAND_WEIGHTS: dict[DifficultyBand, float] = {"easy": 0.40, "medium": 0.35, "hard": 0.25}
VALUE_TIER_WEIGHTS: dict[ValueTier, float] = {
    "small": 0.45,
    "medium": 0.32,
    "large": 0.18,
    "whale": 0.05,
}

# Share of medium/hard companies deliberately built so cheap evidence points the
# wrong way. This bounds the maximum achievable gain from adaptive enrichment,
# so it is a reported quantity rather than an accident of sampling.
CHEAP_MISLEADS_RATE = 0.28
AMBIGUOUS_IDENTITY_RATE = 0.05

_MAX_SAMPLING_ATTEMPTS = 60

# --- vocabularies ------------------------------------------------------------

_INDUSTRIES = (
    "software",
    "fintech",
    "healthtech",
    "ecommerce",
    "logistics",
    "manufacturing",
    "education",
    "nonprofit",
)
_SENIORITY_LADDER = ("ic", "manager", "director", "vp", "c_level")
_FUNCTIONS = (
    "data",
    "engineering",
    "operations",
    "marketing",
    "sales",
    "finance",
    "other",
)
_TRIGGER_EVENTS = (
    "hired_vp_data",
    "series_b_funding",
    "platform_migration",
    "new_cto",
    "opened_second_office",
)

# Confusable neighbours. Real provider errors are near-misses, not uniform
# randomness — a source that confuses "director" with "vp" is realistic; one
# that reports "ic" for a CEO is not. Uniform noise would make cross-provider
# disagreement trivially easy to detect and would flatter any policy.
_INDUSTRY_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "software": ("fintech", "healthtech"),
    "fintech": ("software", "ecommerce"),
    "healthtech": ("software", "education"),
    "ecommerce": ("fintech", "logistics"),
    "logistics": ("manufacturing", "ecommerce"),
    "manufacturing": ("logistics", "education"),
    "education": ("nonprofit", "healthtech"),
    "nonprofit": ("education", "manufacturing"),
}
_FUNCTION_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "data": ("engineering", "operations"),
    "engineering": ("data", "operations"),
    "operations": ("engineering", "finance"),
    "marketing": ("sales", "other"),
    "sales": ("marketing", "other"),
    "finance": ("operations", "other"),
    "other": ("marketing", "operations"),
}

_LEGAL_SUFFIXES = ("Inc", "LLC", "Ltd", "GmbH", "Corp")
_NAME_STEMS = (
    "Northwind",
    "Lumen",
    "Cobalt",
    "Vertex",
    "Harbor",
    "Quanta",
    "Meridian",
    "Aster",
    "Bramble",
    "Cinder",
    "Dovetail",
    "Ember",
    "Fathom",
    "Granite",
    "Halcyon",
    "Ironwood",
    "Juniper",
    "Kestrel",
    "Lantern",
    "Mosaic",
    "Nimbus",
    "Orchard",
    "Pinnacle",
    "Quarry",
    "Ridgeline",
    "Sable",
    "Tessellate",
    "Umbra",
    "Vantage",
    "Willow",
    "Xenon",
    "Yarrow",
    "Zephyr",
)
_FIRST_NAMES = (
    "Avery",
    "Rowan",
    "Priya",
    "Marcus",
    "Ingrid",
    "Diego",
    "Nadia",
    "Kofi",
    "Elena",
    "Tomas",
    "Yuki",
    "Samir",
    "Freya",
    "Lucas",
    "Amara",
    "Jonas",
)
_LAST_NAMES = (
    "Okafor",
    "Lindqvist",
    "Marchetti",
    "Nakamura",
    "Delacroix",
    "Haddad",
    "Petrov",
    "Silva",
    "Fontaine",
    "Bergstrom",
    "Ramanathan",
    "Novak",
)


def _child_rng(seed: int, *parts: str) -> random.Random:
    """Derive a stable child RNG.

    ``hash()`` is salted per-process for strings, so it cannot be used here —
    a dataset generated today would not match one generated tomorrow.
    """
    digest = hashlib.sha256(f"{seed}:{'|'.join(parts)}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _weighted_choice(rng: random.Random, weights: dict[Any, float]) -> Any:
    keys = sorted(weights)  # sorted for determinism
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _perturb_categorical(
    rng: random.Random, value: str, neighbours: dict[str, tuple[str, ...]], error_rate: float
) -> str:
    if rng.random() < error_rate:
        options = neighbours.get(value, ())
        if options:
            return rng.choice(sorted(options))
    return value


def _perturb_seniority(rng: random.Random, value: str, error_rate: float) -> str:
    """Seniority errors move one rung, never across the ladder."""
    if rng.random() >= error_rate:
        return value
    idx = _SENIORITY_LADDER.index(value)
    step = rng.choice([-1, 1])
    return _SENIORITY_LADDER[max(0, min(len(_SENIORITY_LADDER) - 1, idx + step))]


def _lognormal_latency(rng: random.Random, p50: int, p95: int) -> float:
    """Latency is right-skewed; a normal draw would understate the tail."""
    if p50 <= 0:
        return 0.0
    import math

    mu = math.log(p50)
    sigma = max(1e-6, (math.log(max(p95, p50 + 1)) - mu) / 1.645)
    return round(rng.lognormvariate(mu, sigma), 2)


# --- band-conditioned latent generation --------------------------------------


@dataclass(frozen=True)
class _BandProfile:
    obscurity_range: tuple[float, float]
    boundary_distance: tuple[float, float]
    trigger_prob: float


_BAND_PROFILES: dict[DifficultyBand, _BandProfile] = {
    # Clearly qualified or clearly rejected; cheap evidence should settle it.
    "easy": _BandProfile((0.00, 0.25), (16.0, 60.0), 0.10),
    # Cheap evidence lands near the boundary; a mid-tier source is needed.
    "medium": _BandProfile((0.20, 0.55), (5.0, 16.0), 0.30),
    # Near-boundary and/or obscured; may hinge on a late-only signal.
    "hard": _BandProfile((0.50, 0.90), (0.0, 5.0), 0.55),
}


class _BandAllocator:
    """Deterministic largest-remainder allocation of difficulty bands.

    Bands are *not* drawn randomly. Random draws hit target proportions only in
    expectation, and at ~200 companies the sampling error is several points —
    enough to matter, because cost savings concentrate in easy leads. An
    easy-heavy draw would inflate the headline result, and correcting it by
    trying seeds would be seed-shopping.

    Allocating to whichever band is furthest below its target share removes the
    confound entirely and makes proportions a property of the design rather than
    of the seed. Company *attributes* remain fully randomised.
    """

    def __init__(self, weights: dict[DifficultyBand, float]) -> None:
        self._weights = weights
        self._counts: dict[DifficultyBand, int] = dict.fromkeys(weights, 0)
        self._total = 0

    def take(self) -> DifficultyBand:
        """Assign the next *company* to whichever band is most under-served.

        Balancing companies rather than leads is deliberate. An earlier version
        allocated on lead count, which let the band choice depend on how many
        contacts a company had — and because the rule favours the
        highest-weight band for large claims, big companies piled into `easy`.
        That correlated difficulty with cache-hit opportunity, which would have
        inflated the cache savings measured on easy leads.

        Since contacts-per-company is drawn independently, balancing companies
        leaves lead-level shares unbiased, with only sampling noise from
        contact-count variation. Realised shares are reported in the manifest.
        """
        projected = self._total + 1
        band = min(
            sorted(self._weights),
            key=lambda b: (self._counts[b] + 1) / (self._weights[b] * projected),
        )
        self._counts[band] += 1
        self._total += 1
        return band

    def realised(self) -> dict[str, float]:
        if self._total == 0:
            return {}
        return {b: round(c / self._total, 4) for b, c in sorted(self._counts.items())}


def latent_facts(company: LatentCompany, person: LatentPerson) -> dict[str, Any]:
    """Assemble complete ground-truth facts for a lead.

    Deliberately the *only* place this mapping is written. The oracle, the
    band-targeting sampler, and the tests all route through here — three
    hand-maintained copies would eventually disagree, and a divergence would
    make "agreement with the oracle" measure rule drift instead of acquisition
    behaviour.
    """
    return {
        "employee_count": company.employee_count,
        "industry": company.industry,
        "title_seniority": person.title_seniority,
        "title_function": person.title_function,
        "buying_intent": company.buying_intent,
        "recent_trigger_event": company.recent_trigger_event,
        "disqualifying_flag": company.disqualifying_flag,
    }


def _boundary_distance(score: float) -> float:
    """Distance to the nearest decision boundary."""
    return min(abs(score - QUALIFY_THRESHOLD), abs(score - REJECT_THRESHOLD))


def _generate_company(
    rng: random.Random,
    company_index: int,
    band: DifficultyBand,
    cheap_misleads: bool,
    ambiguous_identity: bool,
    split: Split = "calibration",
) -> LatentCompany:
    profile = _BAND_PROFILES[band]
    stem = _NAME_STEMS[company_index % len(_NAME_STEMS)]
    # Each split names companies from a disjoint block, so a calibration
    # company and a test company can never land on the same domain. Domains key
    # the observation store, so a collision would silently merge two companies
    # across the split boundary — the exact leak the split exists to prevent.
    suffix_n = company_index // len(_NAME_STEMS) + SPLIT_NAME_BLOCK[split]
    base_name = stem if suffix_n == 0 else f"{stem}{suffix_n}"
    domain = f"{base_name.lower()}.com"

    variants: tuple[str, ...] = ()
    if ambiguous_identity:
        # Surface-form variation that a naive exact match will fail on. Makes
        # the identity layer's failure rate measurable rather than assumed.
        variants = (
            f"{base_name} {rng.choice(sorted(_LEGAL_SUFFIXES))}",
            f"{base_name} Corporation",
            base_name.upper(),
        )

    industry = rng.choice(sorted(_INDUSTRIES))
    obscurity = rng.uniform(*profile.obscurity_range)

    # Type A misleading construction: cheap signals look strong, but a blocker
    # visible only to deep_research makes the true answer "reject".
    disqualifying = cheap_misleads and rng.random() < 0.5
    if disqualifying:
        industry = rng.choice(["software", "fintech"])

    # Type B misleading construction: intent and a trigger event — both visible
    # only to expensive providers — flip the true answer to "qualify" while the
    # cheap tier sees an unremarkable lead.
    type_b = cheap_misleads and not disqualifying

    trigger: str | None
    if type_b:
        trigger = rng.choice(sorted(_TRIGGER_EVENTS))
        buying_intent = rng.uniform(0.80, 1.0)
    else:
        trigger = (
            rng.choice(sorted(_TRIGGER_EVENTS)) if rng.random() < profile.trigger_prob else None
        )
        buying_intent = rng.betavariate(2.0, 2.5)

    employee_count = max(1, min(20_000, round(rng.lognormvariate(mu=5.0, sigma=1.25))))
    if disqualifying:
        employee_count = rng.randint(60, 900)  # squarely in the ideal band

    return LatentCompany(
        company_id=f"{SPLIT_ID_PREFIX[split]}{company_index:05d}",
        canonical_domain=domain,
        legal_name=f"{base_name} {rng.choice(sorted(_LEGAL_SUFFIXES))}",
        employee_count=employee_count,
        industry=industry,
        recent_trigger_event=trigger,
        disqualifying_flag=disqualifying,
        buying_intent=round(buying_intent, 4),
        obscurity=round(obscurity, 4),
        name_variants=variants,
    )


def _generate_person(
    rng: random.Random,
    company: LatentCompany,
    person_index: int,
    band: DifficultyBand,
    cheap_misleads: bool,
    used_locals: set[str],
) -> LatentPerson:
    """Sample person attributes, steering the oracle score into the band.

    Rejection sampling rather than closed-form inversion: the scoring rules are
    meant to stay editable, and inverting them analytically would couple this
    generator to their current shape.
    """
    profile = _BAND_PROFILES[band]
    lo, hi = profile.boundary_distance

    # The company half of a Type B construction (high intent + trigger) is set
    # in _generate_company. Here the person is kept deliberately unremarkable,
    # so cheap evidence — which sees only title — reads the lead as weak.
    type_b = cheap_misleads and not company.disqualifying_flag

    best: tuple[float, LatentPerson] | None = None

    for attempt in range(_MAX_SAMPLING_ATTEMPTS):
        if type_b:
            seniority = rng.choice(["manager", "director"])
            function = rng.choice(["operations", "marketing", "finance"])
        else:
            seniority = rng.choice(sorted(_SENIORITY_LADDER))
            function = rng.choice(sorted(_FUNCTIONS))

        person = LatentPerson(
            person_id=f"{company.company_id}p{person_index:02d}",
            company_id=company.company_id,
            full_name=(f"{rng.choice(sorted(_FIRST_NAMES))} {rng.choice(sorted(_LAST_NAMES))}"),
            email="",  # filled below, once the name is settled
            title_seniority=seniority,
            title_function=function,
        )
        score = score_facts(latent_facts(company, person)).total_score
        distance = _boundary_distance(score)

        # A disqualified company scores zero by rule, so boundary distance is
        # meaningless for it — accept immediately.
        if company.disqualifying_flag or lo <= distance <= hi:
            best = (0.0, person)
            break

        penalty = min(abs(distance - lo), abs(distance - hi))
        if best is None or penalty < best[0]:
            best = (penalty, person)

        if attempt == _MAX_SAMPLING_ATTEMPTS - 1:
            break

    assert best is not None
    person = best[1]

    # Email is the canonical person key, so it must be unique. Two contacts at
    # one company can independently draw the same name from a finite pool, and
    # a duplicate would silently collapse two distinct people into one entity.
    # Real directories disambiguate the same way.
    local = person.full_name.lower().replace(" ", ".")
    if local in used_locals:
        suffix = 2
        while f"{local}{suffix}" in used_locals:
            suffix += 1
        local = f"{local}{suffix}"
    used_locals.add(local)

    return LatentPerson(
        person_id=person.person_id,
        company_id=person.company_id,
        full_name=person.full_name,
        email=f"{local}@{company.canonical_domain}",
        title_seniority=person.title_seniority,
        title_function=person.title_function,
    )


# --- observation generation --------------------------------------------------


def _observe_field(rng: random.Random, spec: ProviderSpec, field_name: str, truth: Any) -> Any:
    # A provider that covers a field always reports *something* about it. For
    # the trigger event that means either a signal it found, or `False` meaning
    # "looked, found nothing". `None` is reserved for "this provider does not
    # cover the field at all".
    #
    # The distinction matters for score bounds: "no trigger exists" caps the
    # reachable score, whereas "unchecked" leaves 10 points in play. Conflating
    # them would keep leads looking unsettled after they were in fact settled,
    # making the policy buy more than necessary and understating the savings
    # adaptive enrichment can achieve.
    if field_name == "recent_trigger_event":
        if truth is None:
            # False positive: a provider occasionally invents a signal.
            if rng.random() < spec.categorical_error * 0.5:
                return rng.choice(sorted(_TRIGGER_EVENTS))
            return False
        # False negative: it missed a real one, and reports "none found".
        return False if rng.random() < spec.categorical_error else truth

    if truth is None:
        return None

    if field_name == "employee_count":
        noisy = float(truth)
        if spec.numeric_noise:
            noisy *= rng.lognormvariate(0.0, spec.numeric_noise)
        return max(1, round(noisy))

    if field_name == "buying_intent":
        noisy = float(truth) + rng.gauss(0.0, spec.numeric_noise)
        return round(max(0.0, min(1.0, noisy)), 4)

    if field_name == "industry":
        return _perturb_categorical(rng, str(truth), _INDUSTRY_NEIGHBOURS, spec.categorical_error)

    if field_name == "title_seniority":
        return _perturb_seniority(rng, str(truth), spec.categorical_error)

    if field_name == "title_function":
        return _perturb_categorical(rng, str(truth), _FUNCTION_NEIGHBOURS, spec.categorical_error)

    if field_name == "disqualifying_flag":
        flipped = rng.random() < spec.categorical_error
        return (not bool(truth)) if flipped else bool(truth)

    return truth


def _generate_observations(
    seed: int, company: LatentCompany, person: LatentPerson
) -> dict[str, ProviderObservation]:
    truth: dict[str, Any] = {
        "employee_count": company.employee_count,
        "industry": company.industry,
        "recent_trigger_event": company.recent_trigger_event,
        "disqualifying_flag": company.disqualifying_flag,
        "buying_intent": company.buying_intent,
        "title_seniority": person.title_seniority,
        "title_function": person.title_function,
    }

    observations: dict[str, ProviderObservation] = {}
    for spec in CATALOG:
        # Seed by the entity the provider actually describes, not by the lead.
        # A company-scoped provider must return the SAME answer for every
        # contact at that company — otherwise caching it would be lossy
        # (contact B would receive contact A's draw) and full enrichment would
        # get different answers from calling one company API twice.
        scope = company.company_id if spec.entity_type == "company" else person.person_id
        rng = _child_rng(seed, "obs", scope, spec.name)
        latency = _lognormal_latency(rng, spec.p50_latency_ms, spec.p95_latency_ms)

        if rng.random() < spec.failure_rate:
            observations[spec.name] = ProviderObservation(
                provider=spec.name,
                status=ProviderStatus.ERROR,
                fields={},
                latency_ms=latency,
                cost_usd=0.0,  # failed calls are not billed
            )
            continue

        # The correlated-miss mechanism: one obscurity draw, shared by every
        # provider, scaled by how sensitive each is to thin data.
        coverage = spec.base_coverage * (1.0 - spec.obscurity_sensitivity * company.obscurity)
        if rng.random() > max(0.0, coverage):
            observations[spec.name] = ProviderObservation(
                provider=spec.name,
                status=ProviderStatus.MISS,
                fields={},
                latency_ms=latency,
                cost_usd=spec.base_cost_usd if spec.bill_on_miss else 0.0,
            )
            continue

        fields = {
            name: _observe_field(rng, spec, name, truth.get(name)) for name in spec.provides_fields
        }
        observations[spec.name] = ProviderObservation(
            provider=spec.name,
            status=ProviderStatus.SUCCESS,
            fields=fields,
            latency_ms=latency,
            cost_usd=spec.base_cost_usd,
        )

    return observations


# --- assembly ----------------------------------------------------------------


def _oracle(
    company: LatentCompany, person: LatentPerson
) -> tuple[Decision, float, dict[str, float]]:
    breakdown = score_facts(latent_facts(company, person))
    return decide(breakdown.total_score), breakdown.total_score, breakdown.components


def _contacts_for_company(rng: random.Random) -> int:
    """Multiple contacts per company is essential.

    Without it the company-level cache never hits, and the single largest
    real-world cost lever goes unmeasured by the benchmark.
    """
    return int(_weighted_choice(rng, {1: 0.38, 2: 0.30, 3: 0.20, 5: 0.12}))


def generate_dataset(
    seed: int = 42,
    calibration_leads: int = DEFAULT_CALIBRATION_LEADS,
    test_leads: int = DEFAULT_TEST_LEADS,
) -> tuple[list[EvalLead], DatasetManifest]:
    """Generate the full dataset.

    Splits are produced sequentially, each company belonging to exactly one.
    That makes cross-split company leakage structurally impossible rather than
    something to be checked for afterwards.
    """
    leads: list[EvalLead] = []

    split_targets: tuple[tuple[Split, int], ...] = (
        ("calibration", calibration_leads),
        ("test", test_leads),
    )
    for split, target in split_targets:
        produced = 0
        # Company indices, RNG namespaces, and name blocks all restart per
        # split. That is what makes the test set a pure function of
        # (seed, test_leads): enlarging the calibration set cannot shift a
        # single test company. With a shared counter, adding calibration leads
        # would slide every test company's index and silently regenerate the
        # held-out data the results are measured on.
        company_index = 0
        # Reset per split so each is independently stratified — otherwise the
        # calibration set could be balanced while the test set drifts.
        allocator = _BandAllocator(BAND_WEIGHTS)

        while produced < target:
            company_id = f"{SPLIT_ID_PREFIX[split]}{company_index:05d}"

            # Contact count is drawn from its own RNG stream and *before* the
            # band is chosen. Drawing it from the company RNG would make it
            # depend on how many draws company generation happened to consume,
            # which varies by branch (ambiguous-identity and disqualifying
            # cases consume extra draws) — silently correlating difficulty with
            # cache-hit opportunity.
            contacts_rng = _child_rng(seed, "contacts", company_id)
            n_contacts = min(_contacts_for_company(contacts_rng), target - produced)

            band: DifficultyBand = allocator.take()

            crng = _child_rng(seed, "company", split, str(company_index))
            misleads = band != "easy" and crng.random() < CHEAP_MISLEADS_RATE
            ambiguous = crng.random() < AMBIGUOUS_IDENTITY_RATE

            company = _generate_company(crng, company_index, band, misleads, ambiguous, split)

            # Scoped per company: contacts are disambiguated against their own
            # colleagues, and the sequence is deterministic because
            # person_index ascends.
            used_locals: set[str] = set()

            for person_index in range(n_contacts):
                prng = _child_rng(seed, "person", company.company_id, str(person_index))
                person = _generate_person(prng, company, person_index, band, misleads, used_locals)
                observations = _generate_observations(seed, company, person)
                decision, score, components = _oracle(company, person)

                # Verified, not assumed: does cheap-tier evidence actually
                # disagree with the oracle for this lead?
                cheap_facts = merge_observations(observations, CHEAP_TIER)
                cheap_decision = decide(score_facts(cheap_facts).total_score)

                leads.append(
                    EvalLead(
                        eval_lead_id=person.person_id,
                        company=company,
                        person=person,
                        observations=observations,
                        oracle_decision=decision,
                        oracle_score=round(score, 4),
                        oracle_components={k: round(v, 4) for k, v in components.items()},
                        difficulty_band=band,
                        value_tier=_weighted_choice(prng, dict(VALUE_TIER_WEIGHTS)),
                        split=split,
                        cheap_misleads=cheap_decision is not decision,
                        constructed_adversarial=misleads,
                        seed=seed,
                    )
                )
                produced += 1

            company_index += 1

    manifest = _build_manifest(leads, seed)
    return leads, manifest


def _build_manifest(leads: list[EvalLead], seed: int) -> DatasetManifest:
    def _tally(key: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lead in leads:
            counts[str(key(lead))] = counts.get(str(key(lead)), 0) + 1
        return dict(sorted(counts.items()))

    payload = "\n".join(
        json.dumps(lead.to_json(), sort_keys=True, separators=(",", ":")) for lead in leads
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()

    return DatasetManifest(
        generator_version=GENERATOR_VERSION,
        seed=seed,
        rules_version=RULES_VERSION,
        n_leads=len(leads),
        n_companies=len({lead.company.company_id for lead in leads}),
        content_sha256=digest,
        band_counts=_tally(lambda x: x.difficulty_band),
        split_counts=_tally(lambda x: x.split),
        decision_counts=_tally(lambda x: x.oracle_decision),
        value_tier_counts=_tally(lambda x: x.value_tier),
        cheap_misleads_count=sum(1 for x in leads if x.cheap_misleads),
        constructed_adversarial_count=sum(1 for x in leads if x.constructed_adversarial),
        ambiguous_identity_count=sum(1 for x in leads if x.company.name_variants),
        # Reported so that band-vs-contact-count correlation is visible rather
        # than trusted. These should be roughly equal across bands; a skew would
        # mean difficulty is confounded with cache-hit opportunity.
        contacts_per_company_by_band=_contacts_by_band(leads),
    )


def _contacts_by_band(leads: list[EvalLead]) -> dict[str, float]:
    companies_by_band: dict[str, set[str]] = {}
    leads_by_band: dict[str, int] = {}
    for lead in leads:
        band = str(lead.difficulty_band)
        companies_by_band.setdefault(band, set()).add(lead.company.company_id)
        leads_by_band[band] = leads_by_band.get(band, 0) + 1
    return {
        band: round(leads_by_band[band] / len(companies), 3)
        for band, companies in sorted(companies_by_band.items())
    }


def leads_for_split(leads: list[EvalLead], split: Split) -> list[EvalLead]:
    return [lead for lead in leads if lead.split == split]
