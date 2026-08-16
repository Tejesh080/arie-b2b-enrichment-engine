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
    from arie.evalgen.generator import DEFAULT_CALIBRATION_LEADS, DEFAULT_TEST_LEADS

    counts = {"calibration": 0, "test": 0}
    for lead in leads:
        counts[lead.split] += 1
    assert counts == {
        "calibration": DEFAULT_CALIBRATION_LEADS,
        "test": DEFAULT_TEST_LEADS,
    }


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


def test_person_emails_are_unique(leads: list[EvalLead]) -> None:
    """Email is the canonical person key, so collisions merge distinct people.

    Regression test: contacts at one company draw names from a finite pool and
    two independently produced the same address, which silently collapsed them
    into a single entity in the observation store.
    """
    emails = [x.person.email for x in leads]
    duplicates = {e for e in emails if emails.count(e) > 1}
    assert not duplicates, f"duplicate person emails: {sorted(duplicates)[:5]}"


def test_company_domains_are_unique(leads: list[EvalLead]) -> None:
    """The company analogue — domain is the canonical company key."""
    domains = {x.company.company_id: x.company.canonical_domain for x in leads}
    assert len(set(domains.values())) == len(domains)


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


def test_enlarging_calibration_leaves_the_test_split_untouched() -> None:
    """The held-out data must not move when the calibration set grows.

    Company indices, RNG namespaces, and name blocks restart per split, so the
    test split is a pure function of (seed, test_leads). Under a shared counter
    — the original design — adding calibration leads slid every test company's
    index and silently regenerated the data the results are measured on, which
    would make any before/after comparison meaningless.
    """
    from arie.evalgen.generator import generate_dataset

    small, _ = generate_dataset(seed=42, calibration_leads=150, test_leads=300)
    large, _ = generate_dataset(seed=42, calibration_leads=600, test_leads=300)

    small_test = [x for x in small if x.split == "test"]
    large_test = [x for x in large if x.split == "test"]

    assert len(small_test) == len(large_test) == 300
    for a, b in zip(small_test, large_test, strict=True):
        assert a.eval_lead_id == b.eval_lead_id
        assert a.company == b.company
        assert a.person == b.person
        assert a.observations == b.observations
        assert a.oracle_decision == b.oracle_decision


def test_split_namespaces_do_not_collide(leads: list[EvalLead]) -> None:
    """Domains key the observation store, so a cross-split collision would merge
    a calibration company into a test company."""
    calibration_domains = {x.company.canonical_domain for x in leads if x.split == "calibration"}
    test_domains = {x.company.canonical_domain for x in leads if x.split == "test"}
    assert not (calibration_domains & test_domains)


def test_ambiguous_identity_subset_exists(manifest: DatasetManifest) -> None:
    """Needed to make deterministic-match failure measurable rather than assumed."""
    assert manifest.ambiguous_identity_count > 0
