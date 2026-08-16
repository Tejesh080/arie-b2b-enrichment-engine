"""Split construction and internal consistency.

Company-level leakage is the specific hazard here. Because company intelligence
is cached and shared across that company's contacts, a company appearing in both
splits would let calibration data influence test-set behaviour — inflating
apparent performance in a way that is easy to miss and hard to detect later.
"""

from __future__ import annotations

from arie.evalgen.schema import DatasetManifest, EvalLead


def test_no_company_appears_in_both_splits(leads: list[EvalLead]) -> None:
    """The leakage guard.

    Generation makes this structurally impossible — a company is created into
    exactly one split — but it is asserted anyway, because the property is
    load-bearing and a future refactor could quietly break it.
    """
    calibration = {x.company.company_id for x in leads if x.split == "calibration"}
    test = {x.company.company_id for x in leads if x.split == "test"}
    overlap = calibration & test

    assert not overlap, (
        f"{len(overlap)} companies appear in both splits (e.g. {sorted(overlap)[:5]}). "
        "Company-level evidence is cached and shared across contacts, so this "
        "leaks calibration information into the test set."
    )


def test_split_sizes_are_exact(leads: list[EvalLead]) -> None:
    counts = {"calibration": 0, "test": 0}
    for lead in leads:
        counts[lead.split] += 1
    assert counts == {"calibration": 150, "test": 300}


def test_every_band_appears_in_both_splits(leads: list[EvalLead]) -> None:
    """Both splits must be stratified, or calibrated thresholds will not transfer."""
    for split in ("calibration", "test"):
        bands = {x.difficulty_band for x in leads if x.split == split}
        assert bands == {"easy", "medium", "hard"}, f"{split} split bands: {sorted(bands)}"


def test_person_company_references_are_consistent(leads: list[EvalLead]) -> None:
    for lead in leads:
        assert lead.person.company_id == lead.company.company_id
        assert lead.eval_lead_id == lead.person.person_id
        assert lead.company.canonical_domain in lead.person.email


def test_lead_ids_are_unique(leads: list[EvalLead]) -> None:
    ids = [x.eval_lead_id for x in leads]
    assert len(ids) == len(set(ids))


def test_multiple_contacts_share_companies(manifest: DatasetManifest) -> None:
    """Cache-hit opportunity must actually exist.

    With one contact per company the company-level cache could never hit, and
    the largest real-world cost lever would go unmeasured by the benchmark.
    """
    contacts_per_company = manifest.n_leads / manifest.n_companies
    assert contacts_per_company >= 1.5, (
        f"Only {contacts_per_company:.2f} contacts per company; "
        "company-level caching would be untestable."
    )


def test_contacts_per_company_is_not_confounded_with_difficulty(
    manifest: DatasetManifest,
) -> None:
    """Difficulty must not correlate with cache-hit opportunity.

    If easy leads clustered in companies with many contacts, adaptive enrichment
    would appear to save more on easy leads purely because it got more cache
    hits there — a confound, not a finding.
    """
    ratios = manifest.contacts_per_company_by_band
    assert ratios, "manifest is missing contacts-per-band diagnostics"
    spread = max(ratios.values()) - min(ratios.values())
    assert spread < 1.0, (
        f"Contacts-per-company varies by difficulty band: {ratios}. "
        "Difficulty is confounded with caching opportunity."
    )


def test_ambiguous_identity_subset_exists(manifest: DatasetManifest) -> None:
    """Needed to make deterministic-match failure measurable rather than assumed."""
    assert manifest.ambiguous_identity_count > 0
