# Portfolio Summary — Realistic Pilot on ARIE

## Flagship claim

**Built a cost-aware revenue decision engine that reduced modeled enrichment
spend 36.6% versus a tuned waterfall and 48.9% versus full enrichment across
a frozen 140-company evaluation, while maintaining 93.6% policy-decision
agreement.**

These are modeled/simulated-evidence economics, not real-world lead
accuracy — that qualification is not a footnote, it's part of the claim.

## The full story

Ran ARIE end-to-end against 140 real, named companies (logistics, healthcare
back-office, professional services, financial services, manufacturing,
property management, retail/e-commerce) through its real production API —
real targeting-profile generation, real CSV ingestion, real job queue, real
decision receipts — to find out whether its "buy only the evidence necessary
to decide" thesis holds outside a synthetic benchmark, and whether its
scores mean anything for real companies.

**What held up:**
- 48.9% less modeled spend than full enrichment, 36.6% less than a tuned
  cheapest-first waterfall, at 93.6% decision agreement with both —
  reproduced 140/140 exactly against real production receipts before any
  number was trusted.
- 55% of leads stopped before the full evidence cascade.
- 16.4% escalated to human review, concentrated on genuinely weaker
  evidence, not spread indiscriminately.
- Real platform constraints (a 10-row CSV cap, a 25-lead/month quota) were
  hit and worked through like a real customer would, not bypassed.

**What didn't:** a 30-company blinded review — sampled by ARIE's own output
tiers, independently fit-assessed from public company information — found a
30% stark-disagreement rate with real-world company fit. Traced the exact
mechanism: simulated evidence for any company outside ARIE's benchmark
corpus is generated from a hash of the company's domain name, not its real
attributes. Then ran a real Abstract + Hunter live-shadow reality check (8
leads, real provider calls, all external actions safety-disabled) to see if
real evidence closed that gap — it didn't, in this small sample — and
diagnosed why down to specific provider-coverage and company-identity-
validation limitations, without extrapolating a 4–8-contact sample into a
population-level provider verdict.

## The interview story

*"We discovered that the simulated provider layer could test acquisition
economics but not real-company ranking quality. Instead of hiding that
limitation, I designed blinded and live-shadow validations, which exposed
real provider coverage and company-data-quality constraints."*

## Resume bullet

Built and rigorously evaluated a cost-aware B2B lead-scoring engine on 140
real companies through its production pipeline — 36–49% lower modeled
enrichment spend than baseline strategies at 93.6% decision agreement,
verified via exact reproduction against real receipts — then designed
blinded and live-provider validations that surfaced and honestly reported a
real-world accuracy gap the initial metrics alone would have hidden.

## Numbers used (all measured, none projected)

| Metric | Value |
|---|---|
| Real companies evaluated | 140 |
| Modeled spend reduction vs. tuned waterfall | 36.6% |
| Modeled spend reduction vs. full enrichment | 48.9% |
| Policy-decision agreement vs. both baselines | 93.6% |
| Leads stopping before full evidence cascade | 55% (77/140) |
| Human-escalation rate | 16.4% |
| Offline-replay reproduction of real production decisions | 140/140 |
| Blinded human-fit stark-disagreement rate (n=30) | 30% |
| Live-shadow real spend (n=8 leads) | $0.0132 total |

Full write-up: `docs/case-study-realistic-pilot.md`.
