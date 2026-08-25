"""Deterministic synthetic identities for simulated mode.

The frozen corpus answers only for identities ``arie.evalgen`` generated.
Public-demo visitors submit their own — before this module, those leads'
jobs raised ``UnknownCorpusIdentityError`` and dead-lettered, leaving the
lead stranded (an honest outcome for a benchmark harness, a dead end for a
product demo). This module gives an out-of-corpus identity the same
simulated economics instead: latent facts are drawn from a hash of the
lead's own canonical keys, then observed through the exact noise, coverage,
cost, and correlated-miss model the corpus itself was generated with
(``arie.evalgen.generator._generate_observations``, over the same frozen
``CATALOG`` rates). Nothing about the frozen corpus, the catalogue, or the
benchmark changes — corpus identities still replay their frozen
observations byte-for-byte; this is a parallel path for everything else.

**Determinism contract.** The same canonical ``(email, domain)`` always
synthesizes the same lead — across processes, workers, and redeploys —
because every random draw is seeded from those strings (``hashlib``, never
``hash()``, which is salted per process). That is what keeps the durable
evidence cache coherent: a second contact at the same company must reuse
company evidence identical to what a fresh call would have returned, and
resubmitting the same person must reproduce the same receipt.

**Honesty.** Nothing here pretends to be real data. Simulated mode's
receipts are already labelled modelled/simulated; synthesis extends the
same label to identities outside the corpus rather than inventing a new
kind of claim.
"""

from __future__ import annotations

import hashlib
import random

from arie.evalgen.generator import (
    _FUNCTIONS,
    _INDUSTRIES,
    _LEGAL_SUFFIXES,
    _SENIORITY_LADDER,
    _TRIGGER_EVENTS,
    VALUE_TIER_WEIGHTS,
    _generate_observations,
    _weighted_choice,
    latent_facts,
)
from arie.evalgen.schema import EvalLead, LatentCompany, LatentPerson
from arie.scoring.rules import decide, score_facts

# Version stamp for the synthesis rules themselves, carried on the produced
# EvalLead. Bump when the priors below change: the determinism contract is
# per-version, and a silent prior change would make cached company evidence
# disagree with what a fresh call now returns.
SYNTHETIC_VERSION = "synthetic-1.0.0"

# Priors, chosen so the public demo shows every outcome the product has:
# most synthesized leads resolve autonomously (route or reject), a meaningful
# minority escalates to a human, and disqualifying flags appear often enough
# to be discoverable. Verified empirically over a sample of identities in
# tests/unit/test_synthetic_identities.py rather than trusted from here.
_OBSCURITY_RANGE = (0.10, 0.80)
_DISQUALIFYING_PROB = 0.10
_TRIGGER_PROB = 0.30


def _seed_from(*parts: str) -> int:
    """A stable 64-bit seed from strings — same inputs, same seed, forever."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _display_name_from_email(canonical_email: str) -> str:
    """`jordan.p.lee@x.com` -> `Jordan P Lee` — a readable fallback when the
    submitter gave no full name. Display-only; never keys anything."""
    local = canonical_email.split("@", 1)[0]
    words = [w for w in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if w]
    return " ".join(w.capitalize() for w in words) or canonical_email


def _company_stem_from_domain(canonical_domain: str) -> str:
    """`northwind-logistics.co.uk` -> `Northwind Logistics` — display-only."""
    label = canonical_domain.split(".", 1)[0]
    words = [w for w in label.replace("-", " ").replace("_", " ").split() if w]
    return " ".join(w.capitalize() for w in words) or canonical_domain


def synthesize_company(canonical_domain: str, company_name: str | None = None) -> LatentCompany:
    """Latent company facts, seeded by the canonical domain alone.

    Seeding by domain — never by the submitting lead — is what makes two
    contacts at one company share a company: same facts, same observations,
    and therefore lossless company-level cache reuse, exactly the property
    the corpus's own keying guarantees.
    """
    rng = random.Random(_seed_from(SYNTHETIC_VERSION, "company", canonical_domain))
    stem = _company_stem_from_domain(canonical_domain)

    industry = rng.choice(sorted(_INDUSTRIES))
    obscurity = rng.uniform(*_OBSCURITY_RANGE)
    disqualifying = rng.random() < _DISQUALIFYING_PROB
    trigger = rng.choice(sorted(_TRIGGER_EVENTS)) if rng.random() < _TRIGGER_PROB else None
    buying_intent = rng.betavariate(2.0, 2.5)
    employee_count = max(1, min(20_000, round(rng.lognormvariate(mu=5.0, sigma=1.25))))

    return LatentCompany(
        # The id doubles as the observation-generator's seeding scope, so it
        # must be canonical-key-derived, stable, and disjoint from corpus ids.
        company_id=f"syn:{canonical_domain}",
        canonical_domain=canonical_domain,
        legal_name=company_name or f"{stem} {rng.choice(sorted(_LEGAL_SUFFIXES))}",
        employee_count=employee_count,
        industry=industry,
        recent_trigger_event=trigger,
        disqualifying_flag=disqualifying,
        buying_intent=round(buying_intent, 4),
        obscurity=round(obscurity, 4),
    )


def synthesize_person(
    canonical_email: str, company: LatentCompany, full_name: str | None = None
) -> LatentPerson:
    """Latent person facts, seeded by the canonical email alone."""
    rng = random.Random(_seed_from(SYNTHETIC_VERSION, "person", canonical_email))
    return LatentPerson(
        person_id=f"syn:{canonical_email}",
        company_id=company.company_id,
        full_name=full_name or _display_name_from_email(canonical_email),
        email=canonical_email,
        title_seniority=rng.choice(sorted(_SENIORITY_LADDER)),
        title_function=rng.choice(sorted(_FUNCTIONS)),
    )


def synthesize_corpus_lead(
    *,
    canonical_email: str,
    canonical_domain: str,
    full_name: str | None = None,
    company_name: str | None = None,
) -> EvalLead:
    """A complete, deterministic ``EvalLead`` for an out-of-corpus identity.

    The observations come from the corpus generator's own
    ``_generate_observations`` — same provider specs, rates, coverage,
    noise, ``bill_on_miss`` semantics, and correlated obscurity misses — so
    a receipt over a synthesized lead reads exactly like a corpus one. The
    global seed is a constant: all per-identity variation flows through the
    generator's own per-entity seeding scope (``company_id``/``person_id``),
    which the ids above deliberately derive from the canonical keys.
    """
    company = synthesize_company(canonical_domain, company_name)
    person = synthesize_person(canonical_email, company, full_name)
    observations = _generate_observations(_seed_from(SYNTHETIC_VERSION, "obs"), company, person)

    breakdown = score_facts(latent_facts(company, person))
    tier_rng = random.Random(_seed_from(SYNTHETIC_VERSION, "tier", canonical_domain))

    return EvalLead(
        eval_lead_id=f"syn:{canonical_email}",
        company=company,
        person=person,
        observations=observations,
        oracle_decision=decide(breakdown.total_score),
        oracle_score=breakdown.total_score,
        oracle_components=breakdown.components,
        # Bookkeeping the runtime never reads (bands/splits shape corpus
        # composition; policy.run touches neither) — filled honestly enough
        # to satisfy the schema without implying corpus membership.
        difficulty_band="medium",
        value_tier=_weighted_choice(tier_rng, dict(VALUE_TIER_WEIGHTS)),
        split="test",
        cheap_misleads=False,
        seed=_seed_from(SYNTHETIC_VERSION, "obs"),
    )
