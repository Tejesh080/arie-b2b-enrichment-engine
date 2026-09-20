# Portfolio material

Interview-ready framing for this project — what to say, in how much time, and
what not to claim. Every number here is pulled from
[`README.md`](../README.md), [`benchmark.md`](benchmark.md#results), and
[`deployment.md`](deployment.md), not restated from memory; if any of those
change, this page is stale until it's updated to match.

---

## 60-second explanation

> Most lead-enrichment tools call every data provider for every lead, then
> score whatever comes back. ARIE asks a different question first: given
> what's already known, is the next purchase even worth making? It buys
> evidence cheapest-first, stops the moment nothing left to buy could change
> the answer, and only acts autonomously above a calibrated confidence
> threshold — otherwise it hands the lead to a person and keeps both the
> machine's recommendation and the human's decision on the record, side by
> side, permanently. When the evidence genuinely isn't enough, it says so
> rather than defaulting to a false "no."
>
> I validated it two ways. A 10-seed synthetic benchmark, honestly reported —
> including where my founding hypothesis, expected-value-of-information
> reasoning, lost to a much simpler policy. And a small real-money live run
> against real vendors in a disposable, fully isolated environment, where it
> caught and correctly suppressed a real vendor sending back the wrong
> person's data. Live autonomous action on real-provider evidence stays
> hard-disabled in code until that's been validated at more than three leads —
> that's a deliberate line, not a gap I missed.

---

## 3-minute technical explanation

**Problem.** B2B lead enrichment tools almost always answer "which provider
should I call next?" — a routing question. Nobody was answering "is another
call worth making at all?" — a stopping question. Calling everything is
expensive; a fixed waterfall doesn't know when it's already confident enough,
or when it should have kept going.

**Decision policy.** Two independent stopping rules. *Settled*: given the
best- and worst-case values of every field still unknown, does the reachable
score already sit entirely on one side of a decision boundary? If so, no
purchasable evidence can change the outcome. *Confident*: a calibrated
confidence model (Platt-scaled, evaluated by ECE and reliability bins) versus
an autonomy threshold τ derived from a Clopper-Pearson upper bound on
selective error — not the raw observed rate, which is optimistic on small
samples. Either rule can fire first; neither subsumes the other.

**The negative result.** The founding hypothesis — expected-value-of-information
(EVoI) reasoning — did not hold. I set a bar before running anything (≤1pp
agreement loss at ≥20% cost reduction) and nothing met it. An ablation with
every stopping signal EVoI has, minus EVoI-guided ordering, beat it on 9 of 10
seeds. Business value spans $5–400 while provider prices span $0.001–0.60 —
three orders of magnitude apart — so the cost term barely moves EVoI's argmax
and it degenerates into "buy the most informative provider," which is
expensive. I selected `calibrated_bounds` *after* EVoI failed that
preregistered win condition — it cuts modeled API spend **41.6%** versus a
tuned waterfall, at **2.3 percentage points lower** agreement with a synthetic
oracle. A stated trade, explicitly not a win against the bar I set for myself.

**Production system.** FastAPI ingestion API, a Postgres job queue
(`SELECT ... FOR UPDATE SKIP LOCKED`, no Redis/Celery/Temporal), a worker that
runs the policy and writes evidence/cost/decisions transactionally, a
human-review workflow, and a Decision Receipt that reconstructs "why did this
stop here" from persisted state alone — reproducible months later under a
policy version you've since replaced. Multi-tenant: org-scoped BYOK provider
credentials in Supabase Vault, an org-level `execution_mode` gate layered
under a process-wide `PROVIDER_MODE` switch, and identity verification that
must return a `VERIFIED` match before any person-provider evidence is allowed
to score.

**Real-provider validation, not just simulation.** Abstract and Hunter have
both made real, billed calls — most recently a three-lead validation in a
disposable Supabase database branch and temporary organization, $0.01965
total spend, production never touched. Hunter reproducibly returned the wrong
person's data for one lead across three independent real calls; the identity
guard suppressed that evidence rather than scoring the wrong person's title.
That's a real, caught vendor defect — explicitly *not* a statistically
meaningful accuracy claim at n=3.

**What's still gated.** Live autonomous action on real-provider evidence is
disabled in code, not by a flag someone could flip under pressure — τ is
fitted on the synthetic calibration split, and applying it to real evidence
with different coverage and error modes would be an unmeasured claim wearing
a calibrated number's clothes. No load testing beyond five concurrent
submissions. Apollo is implemented and fixture-tested but not live-validated.
Single-tenant proof, no paying customer yet.

---

## Resume bullets

Pick the ones that fit the role. All are load-bearing on the actual repo —
verify against current code/docs before using if this page is old.

- Designed and benchmarked a cost-aware lead-enrichment stopping policy
  against a synthetic ground-truth dataset (10 seeds, 300 held-out leads per
  seed), then selected the policy that actually won on a pre-registered bar
  *after* my founding hypothesis (expected-value-of-information reasoning)
  failed it — cutting modeled API spend 41.6% at a documented 2.3pp agreement
  cost, reported as a trade-off rather than smoothed into a win.
- Built and live-validated real third-party provider integrations (Abstract,
  Hunter) end-to-end in a disposable, fully isolated environment — a fresh
  database branch, a temporary organization, real BYOK Vault credentials, full
  teardown after — catching, correctly suppressing, and documenting a real,
  three-times-reproduced vendor identity-mismatch defect for $0.02 in total
  real spend, behind an identity-verification gate that blocks any
  unverified person match from reaching the scorer.
- Built a production FastAPI + Postgres backend (`SKIP LOCKED` job queue, no
  Redis/Temporal, 2,077 tests, CI-gated mypy-strict + migration-drift checks)
  with a calibrated-confidence-gated autonomy/human-review split, org-scoped
  BYOK credentials, and a read-only MCP interface giving an AI agent 11
  schema-scoped, audited diagnostic tools against production — backed by a
  Postgres role verified incapable of reading a single base table.

---

## Interview questions

**Why not just enrich every lead?**
Full enrichment scored highest on synthetic-oracle agreement (0.839) in the
benchmark, but at the highest cost (8 calls, $0.44/lead) — the ceiling, not a
realistic policy at volume. The whole project is the argument that a cheaper
policy can capture most of that value; the measured answer is it captures
~97% of the agreement at ~55% of the cost.

**Why Postgres `SKIP LOCKED` instead of Redis/Celery/Temporal?**
[ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md). The deciding
factor is transactional consistency: a worker claims a job, runs one
state-machine step, and commits the lead's new status in the *same*
transaction as marking the job complete. A separate queue technology
reintroduces a dual-write hazard. Temporal is the right tool at multi-service
scale with cross-service sagas; this is one service against one database at
thousands of leads/day, nowhere near where Postgres polling is a bottleneck.

**Why calibration?**
A raw classifier score isn't a probability. Calibration (Platt/isotonic, fit
on out-of-fold predictions grouped by company so no company leaks between
calibration and evaluation) makes the confidence number mean what it claims
to mean, measured by ECE (0.061) and reliability bins, not asserted.

**What is τ (tau)?**
The autonomy threshold — the calibrated-confidence value above which a
decision is taken without a human. Derived from a **Clopper-Pearson upper
bound** on selective error at the target error rate, not the raw observed
error rate on calibration data. Zero errors in ten accepted predictions is
weak evidence at small sample sizes; the bound accounts for that instead of
taking the empirical rate at face value.

**What happened to the EVoI idea?**
[ADR 0004](adr/0004-evoi-is-a-negative-result.md). The project's headline
negative result. An ablation — identical to the EVoI-driven policy except for
EVoI-guided provider ordering — beat it on 9 of 10 seeds, at both lower cost
and not-actually-better agreement. The failure mode: business value spans
$5–400, provider price spans $0.001–0.60 — three orders of magnitude apart —
so the cost term barely moves EVoI's argmax, and it degenerates into "buy the
most informative provider available," usually the expensive one. Kept in the
repo as a documented negative result, not deleted.

**How does human review preserve auditability?**
The Decision Receipt keeps three facts distinct and never merges them: the
machine's `recommended_action` (frozen at decision time), the human's
`action`/`notes`, and the `final_status` (the lead's live state). A human
approving a machine's "reject" doesn't rewrite what the machine said.

**Why shadow mode?**
To let ARIE prove itself against a live workflow before it's trusted to
control anything. `mode: "shadow"` runs the full pipeline — real evidence
acquisition, real cost if a live provider is enabled, a real
`decision_receipts` row — but lands on a dedicated terminal status
(`SHADOW_EVALUATED`) instead of an authoritative branch, and never opens a
human review. Pipeline metrics explicitly exclude shadow leads.

**What did the real-provider validation actually prove, and what didn't it?**
It proved the live architecture is wired correctly end to end — real BYOK
credentials resolved per organization, real vendor calls made and cost
tracked to the cent, a real vendor identity-mismatch caught and suppressed
rather than silently scored, and the whole thing torn down leaving production
untouched. It did **not** prove anything about accuracy, ROI, or cost savings
at any meaningful scale — three leads is a correctness check, not a sample
size a statistic can be computed from, and none is claimed.

**Why does Hunter get a stronger "verified" claim than Apollo?**
Because it's been exercised, repeatedly, against the real API — successes,
misses, and a reproduced wrong-person match — while Apollo is complete and
green against fixtures and the vendor's published documentation only.
"Contract-tested" and "live-verified" are kept as distinct claims on purpose;
collapsing them would overstate Apollo's status for no reason.

**What are the biggest limitations?**
The synthetic benchmark's honesty is also its limit — it proves the policy
beats alternatives against a *modelled* provider/noise distribution, not
real-world data drift or vendor-specific failure modes at scale. The n=3
real-provider validation is a correctness proof, not statistically
meaningful. Apollo has no real API call behind it yet. The only concurrency
proof is small — five simultaneous submissions, no duplicate processing — not
load testing. No auth/tenancy beyond a single-tenant proof. And the EVoI
result stays open: [ADR 0004](adr/0004-evoi-is-a-negative-result.md) names
three concrete conditions under which it might actually win — none tested
here.

**What would you change for production multi-tenancy?**
Nothing here has *customer-facing* production traffic today — one deployed
instance, no paying customer, RLS present but not exercised under real
multi-tenant load. Real production multi-tenancy would need per-tenant budget
caps at the ledger-write level (today's org-budget check and the eventual
ledger write aren't atomic — a known, documented, bounded gap), and load
testing beyond five concurrent submissions.

---

## Portfolio claims

**Safe claims**

- A synthetic benchmark, honestly reported across 10 seeds, showing a
  calibrated two-rule stopping policy cuts modeled API spend ~41.6% versus a
  tuned waterfall baseline, at a measured ~2.3pp agreement cost — selected
  *after* the founding EVoI hypothesis failed its own preregistered bar, not
  instead of testing it.
- A negative result on that founding hypothesis, reported rather than hidden,
  with the ablation that beat it and the reasoning why.
- A production backend with real transactional guarantees: exactly-once
  *effect* per job via `SKIP LOCKED` + idempotency keys, a Decision Receipt
  that reconstructs any past decision from persisted state, 2,077 tests, and
  a CI pipeline that includes real-Postgres integration tests.
- **Two real third-party provider integrations, live-verified with real
  billed calls** (Abstract, Hunter) — including a documented, three-times
  reproduced vendor identity-mismatch defect that the system's own identity
  guard correctly suppressed rather than silently scored.
- A small, isolated, real-money validation (three leads, $0.01965 total
  spend, disposable infrastructure, full teardown, production untouched) that
  verifies architecture and correctness — explicitly not an accuracy or ROI
  claim.
- A read-only MCP interface giving an AI agent 11 schema-scoped, audited,
  timeout-capped diagnostic tools against production, backed by a Postgres
  role verified incapable of reading a single base table directly.

**Claims to avoid**

- "Beats human-level decision quality" — never measured against human
  reviewers; the comparison is a synthetic oracle, not people.
- "Same quality as full enrichment" — it's 2.3pp worse, explicitly.
- "The policy won the benchmark" (unqualified) — say instead: *selected
  calibrated bounds after the original EVoI hypothesis failed the
  preregistered win condition; calibrated bounds reduced modeled spend ~41.6%
  vs. tuned waterfall with lower synthetic-oracle agreement.*
- "EVoI won" / "proves expected-value-of-information works" — the opposite;
  it's the project's headline negative result.
- "Zero quality loss" or any claim implying the pre-registered bar (≤1pp loss
  at ≥20% cost reduction) was met — it wasn't, on any seed.
- "Production-ready at scale" without qualification — no load testing beyond
  one small concurrency check, no multi-tenant customer traffic, no auth
  stress-testing.
- Any accuracy, ROI, or cost-savings figure attributed to the n=3
  real-provider validation — that run proves architecture and correctness,
  not a statistic. The 41.6%/2.3pp figures come from the 10-seed synthetic
  benchmark only, and the two should never be blended into one number.
- "Apollo is live-verified" — it isn't. Contract-tested against fixtures and
  documentation only.
- "ARIE autonomously qualifies real leads" — it does not, and is blocked in
  code from doing so. A lead enriched by a real provider always ends at a
  human. See [provider-integration.md](provider-integration.md).
