"""The provider-independent normalization layer (Live V1 Foundation, Phase 2/5).

Real vendors speak their own vocabulary. Abstract API says ``"Computer
Software"``; Apollo says ``"software"`` or ``"information technology &
services"``; a CRM export says ``"SaaS"``. All three describe the same thing,
and ``arie.scoring.rules`` scores exactly one of them.

Before this package existed, the live path handed a lower-cased provider string
straight to the scorer, where ``_INDUSTRY_POINTS.get("computer software", 0.0)``
returned ``0.0`` — indistinguishable from "we deliberately assessed this
industry as a poor fit". That is the single most dangerous failure mode a live
enrichment system has: **silent, invisible mis-scoring that looks exactly like
a confident negative assessment.**

The boundary this package defines::

    RAW PROVIDER RESPONSE
        ↓  (provider adapter — arie.providers.*)
    normalized evidence          arie.normalization.contract.NormalizationReport
        ↓  (canonical mapping — arie.normalization.taxonomy)
    canonical ARIE vocabulary    the closed sets in arie.scoring.rules
        ↓
    evidence store               arie.evidence.store
        ↓
    scorer                       arie.scoring.engine

The invariant, stated once here and enforced by tests in
``tests/unit/test_normalization_taxonomy.py`` and
``tests/unit/test_normalization_contract.py``: **no raw provider vocabulary
ever reaches the scorer.** Every string crossing into the evidence store is a
member of a closed canonical set, or is dropped as unknown — never silently
scored as zero.
"""
