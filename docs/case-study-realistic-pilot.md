# Case Study: A Realistic Pilot on ARIE

**Date**: 2026-09-21 to 2026-09-22. **Status**: Complete — Track A (system/economics), Track B (blinded human-fit review), and Track C (real-provider live_shadow reality check + named-contact provider diagnostics) all finished. Frozen.

**Raw artifacts**: `data/evaluation/runs/realistic-pilot-2026-09-21/` (frozen datasets, checksums, raw receipts, baseline comparison, human review, live-shadow results, Hunter diagnostics — reproducible from the committed scripts in `scripts/`; the run directory itself is gitignored by existing project convention, matching every prior evaluation run in this repo).

This was an evaluation exercise, not a customer engagement. The organizations used for it (`ARIE Realistic Pilot Evaluation`, `ARIE Live Shadow Reality Check 2026-09-22`) are explicitly **not paying customers** and should not be read as one anywhere in this document.

---

## 1. Customer scenario

A small AI automation consultancy selling workflow-automation / AI-agent implementations (support triage, document processing, back-office/ops workflows) to established mid-market businesses. Target profile: 30–500 employees, industries with repetitive document/ticket-heavy workflows (logistics, healthcare admin, professional services, insurance/financial-services back-office, manufacturing, property management, specialty retail/e-commerce), an identifiable ops/CX/founder-level decision-maker.

## 2. Initial targeting assumptions

Written in plain English *before* any ARIE run. Fed verbatim into ARIE's real `/intelligence/targeting/draft` → `/intelligence/targeting/confirm` flow (a real DeepSeek call, $0.0034 modeled cost) and confirmed **unmodified from ARIE's own draft** — no hand-tuning after seeing the generated profile. Confirmed thresholds: `qualify_threshold=65.0`, `reject_threshold=55.0`.

## 3. Dataset

140 real, named companies, deliberately spread: 25 strong fits, 50 medium, 40 ambiguous, 25 weak fits (deliberate negative controls: bootstrapped solo SaaS tools, single-location bookstores, pre-seed startups), across 10 industry buckets, collected from public directories/listings. Company name + domain + a public contact where easily found. Validated programmatically (0 duplicates, 0 malformed domains, 0 synthetic placeholders, 0 rows missing required fields) and frozen with a SHA-256 checksum before upload.

---

## Track A — System / Economic Validation

**What it tests**: how ARIE spends its evidence-acquisition budget — stopping behavior, cost versus baselines, escalation mechanics — using 140 real company identities run through the real ARIE customer pipeline (real org, real ICP confirmation, real `/batches` CSV upload, real job queue, real decision receipts) under `execution_mode=simulated`.

**What it does not test**: whether the resulting decisions track real-world company fit. See Track B.

### Results

| Decision | Count | % |
|---|---|---|
| reject | 89 | 63.6% |
| auto_route | 29 | 20.7% |
| escalate_human | 22 | 15.7% |

| Policy | Mean cost/lead | Mean calls/lead |
|---|---|---|
| **calibrated_bounds (production)** | **$0.4705** | **5.564** |
| waterfall_expensive (tuned) | $0.7427 | 6.886 |
| full_enrichment | $0.9207 | 8.000 |

- **36.6% modeled spend reduction vs. the tuned waterfall; 48.9% vs. full enrichment.**
- **93.6% decision agreement** between production and both baselines.
- **77/140 (55%) stopped before the full cascade.**
- **16.4% human-review rate.**
- `CalibratedBoundsPolicy` vs. `TunedWaterfall` vs. `FullEnrichment` run against the exact same deterministic synthesized evidence each of the 140 real leads generated in the live pipeline. `TunedWaterfall`'s gate was tuned only on ARIE's standard 600-lead synthetic calibration split, never on this pilot's own leads.
- **A bug caught mid-analysis, not hidden**: the first offline-replay run showed the waterfall and full-enrichment baselines producing identical results and a suspiciously low ~56% reproduction rate against production's real receipts. Root cause: the replay script forgot to apply this org's confirmed ICP `scoring_config`, silently using ARIE's reference defaults. Fixed, then **re-verified at 140/140 exact reproduction** (decision and stop reason) against production's real recorded receipts before any number above was trusted.
- Real, unplanned product constraints were hit and worked through, not bypassed: the org's real starter-plan limits (10-row CSV cap, 25-lead/month quota) were hit exactly as a genuine starter customer would hit them, then the org was reclassified to the `internal` plan (the same classification every other non-paying ARIE evaluation org already carries) so the frozen dataset could finish uploading.

**This validates ARIE's acquisition/stopping economics under the deterministic simulated-evidence universe. It does not validate real-world lead accuracy — that claim is not made here.**

---

## Track B — Real-World Fit Validation

**What it tests**: whether ARIE's simulated-mode output tracks independently-researched real-world company fit.

30 leads sampled *after* Track A finished, stratified by **ARIE's own output** (10 highest-confidence `auto_route`, 10 `escalate_human`, 10 highest-confidence `reject`) — not by the dataset's pre-assigned fit tier. Each company's real-world fit was independently assessed from public information before comparing to ARIE's decision.

| | Count | % |
|---|---|---|
| Clear agreement | 8 | 27% |
| Partial disagreement | 13 | 43% |
| **Stark disagreement** | **9** | **30%** |

**Root cause, traced in source before this was ever observed empirically**: for any company outside ARIE's frozen benchmark corpus (every real company in this pilot), simulated-mode evidence is generated by seeding a `random.Random` from a hash of the company's **domain string alone** (`arie/providers/synthetic.py`). Employee count, industry, and buying intent are deterministic-but-arbitrary — the real company's real attributes are never read. This is intentional, documented behavior, not a bug — but it means **simulated-mode scores cannot be treated as real-company-fit validation.**

- 5 of ARIE's 10 highest-confidence, fully-autonomous `auto_route` decisions were real companies independently assessed as WEAK fits: Help Scout and Greenway Health (real software vendors, not ops-heavy targets), Carrd (a literal one-person company), GEODIS (a global logistics conglomerate), The Strand Book Store (a single-location bookstore since 1927).
- 4 of ARIE's 10 highest-confidence `reject` decisions were real companies independently assessed as STRONG fits: AMS Fulfillment, ITS Logistics, Physicians Group Management, Ravix Group.
- A compounding architectural fact found while diagnosing this: the synthetic generator's fixed 8-industry vocabulary overlaps only 3-of-8 with this org's own confirmed ICP industry list — five of the org's own preferred industries can never be represented by synthesized evidence, regardless of a real company's real industry.

**This is a product limitation, being stated plainly, not hidden.**

---

## Track C — Real Provider Reality Check

**What it tests**: whether replacing simulated evidence with real Abstract + Hunter evidence changes the picture in Track B, using ARIE's real `execution_mode=live_shadow` path.

### Setup and safety

A dedicated, disposable local Postgres database (never the shared dev database or the deployed Supabase instance) ran ARIE's real, unmodified live-acquisition code path — the same `execution_mode`/credential-resolution/scoring logic production uses, not a test shortcut. The org's ICP `scoring_config` was copied from the exact confirmed Track A production config and verified byte-identical before any lead was scored. 8 companies were selected from the frozen 140 (2 strong / 2 plausible / 2 weak / 2 deliberately ambiguous fits, independently researched, without looking at ARIE's prior simulated decision for any of them) and run through real Abstract + Hunter calls.

- **All external actions remained disabled** — `LIVE_AUTONOMY_ENABLED=False` is a structural code constant, not a runtime setting.
- **All 8 leads ended `SHADOW_EVALUATED`** — no CRM sync, no authoritative customer-facing outcome, by design.
- **Total real spend: $0.0132 across 8 leads** ($0.00165/lead).

### Did real evidence improve human-fit agreement?

**No — not in this tiny diagnostic sample.** Simulated-mode agreement for these same 8 companies was 3/8 (37.5%); live-shadow agreement was 2/8 (25%). One decision changed (indinero: `auto_route` → `reject`) — a genuinely strong real fit moved further from correct, not closer. n=8 is a diagnostic reality check, not a statistical claim either way.

### Provider findings

- **Hunter** initially provided no usable person evidence across the 8-lead run (0/4 successes on generic placeholder-email lookups). A follow-up, code-unchanged diagnostic tested Hunter Combined Enrichment directly against 4 real, Hunter-Email-Finder-verified named contacts: **person match 2/4, correct employer among matches 2/2, usable ARIE title evidence 1/4.** One matched identity (Jon Yongfook) had a correct person/employer match but no title data at all; two contacts (Jessica Mah, Josh Spencer) had no Combined record whatsoever. **No wrong-person or wrong-employer match was observed in this small sample.** Hunter's separate Email Finder endpoint demonstrated broader identity/email coverage than Combined for this sample, but did not guarantee the title evidence ARIE's scoring actually needs (the one contact tested through both endpoints had no title in either).

  *In a small named-contact diagnostic, Hunter Combined produced usable title evidence for only one of four contacts. Correct matches were identity-consistent, suggesting the observed limitation was evidence availability/coverage rather than wrong-person matching. A larger provider benchmark would be required before selecting or rejecting Hunter for production use.*

- **Abstract** returned useful company data in several cases but also returned company evidence that conflicted materially with independently researched company facts in important cases (Higginbotham: `employee_count=2`/`industry="media"` vs. real 3,200–4,000 employees/insurance; Alexandria Industries: `industry="energy"` vs. real manufacturing).

  *In the small live-shadow sample, Abstract usually returned company evidence, but several outputs conflicted materially with independently researched company facts. ARIE currently lacks a company-level identity/consistency gate analogous to its person-evidence identity checks.*

**None of the above should be extrapolated into a population-level provider-accuracy claim — every provider figure in this section comes from a sample of 4–8.**

---

## Final Product Conclusion

**ARIE HAS demonstrated:**
- Functioning end-to-end customer ingestion (real org, real ICP confirmation, real CSV upload, real job queue)
- Deterministic, reproducible policy evaluation (140/140 offline-replay reproduction against real production receipts)
- Selective evidence acquisition (55% of real-company leads stopped before the full cascade)
- Meaningful modeled-cost reduction (36.6–48.9% versus two credible baselines)
- Explicit stop decisions with stated reasons
- Evidence provenance (every receipt names its sources)
- Human escalation that is not a rubber stamp (16.4% rate, concentrated on genuinely weaker evidence)
- Live-provider safety controls (autonomy structurally disabled, shadow-only outcomes, org-scoped credentials, strict spend caps — all verified, not assumed)
- Identity protection for person evidence (verification gating before person data scores)
- Decision Receipts with full audit trail
- Honest failure analysis (a real bug in this pilot's own analysis script was caught and fixed before any result was trusted; every provider/scoring limitation found here is reported, not patched over)

**ARIE HAS NOT demonstrated:**
- Production lead-ranking accuracy
- Reliable provider coverage
- Reliable company-enrichment correctness
- First-customer readiness
- Autonomous production decision readiness
- Proven financial ROI

## Readiness

| Surface | Status |
|---|---|
| Portfolio demonstration | **Yes** |
| Engineering/interview case study | **Yes** |
| External research pilot | **Possible only with a controlled live_shadow setup and manual review** |
| First paying customer | **Not yet** |
| Autonomous production decisions | **No** |

## If ARIE Becomes a Real Product — Three Validation Needs

Not a backlog — exactly three, in priority order, each directly evidenced by this pilot:

1. **A 20–50 contact/provider coverage benchmark on the actual target ICP** — this pilot's 4–8-contact provider samples are diagnostic, not sized to select or reject a vendor.
2. **A company-evidence identity/consistency validation mechanism** — ARIE validates person identity before scoring person evidence; it has no equivalent check that company-enrichment data actually describes the requested company, and this pilot found concrete cases where that gap mattered.
3. **A larger live_shadow evaluation before any autonomous behavior** — this pilot's 8-lead reality check is a diagnostic floor, not sufficient evidence either way for autonomous decisions.

## Limitations

- Human-review labeling (Track B, and the Track C ground-truth sheet) was done by the same person operating the rest of this pilot — not a genuinely blinded third party. Reasoning was recorded from public information before consulting ARIE's decision in each case, but true blinding wasn't achievable; treat the agreement percentages as indicative, not certified statistics.
- 140, 30, and 8/4 are all small samples; no claim in this document is statistically powered.
- The "internal" plan reclassification mid-Track-A-run, while narrow and fully documented (`validation-and-freeze.md`), means the dataset wasn't uploaded under one uniform plan tier throughout.
