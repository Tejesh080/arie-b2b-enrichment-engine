# Portfolio material

Interview-ready framing for this project — what to say, in how much time,
and what not to claim. Every number here is pulled from
[`README.md`](../README.md), [`05-results.md`](05-results.md), and
[`07-deployment.md`](07-deployment.md), not restated from memory; if any of
those change, this page is stale until it's updated to match.

---

## 30-second explanation

> Most lead-enrichment systems call every data provider for every lead, then
> score whatever comes back. I built a system that treats enrichment as a
> sequential decision: given what's already known, is the next API call worth
> its price? The interesting part isn't that it works — it's that my first
> hypothesis for *how* to do that, expected-value-of-information reasoning,
> lost to a much simpler ablation on a real benchmark. I shipped the simpler
> policy, kept the negative result, and then took the whole thing to a hosted
> deployment: a FastAPI + Postgres backend on Railway, a Next.js console on
> Vercel, real autonomous decisions and human escalations running end-to-end
> against a live database.

---

## 2-minute explanation

**Problem.** B2B lead enrichment (Clearbit, ZoomInfo-style tools) almost
always answers "which provider should I call next?" — a routing question.
Nobody was answering "is another call worth making at all?" — a stopping
question. Calling everything is expensive and slow; a fixed waterfall
(cheapest-first, stop at N) is cheap but doesn't know when it's already
confident enough, or when it should have kept going.

**Architecture.** A synthetic benchmark first (M0): 10 seeds, 300 held-out
leads per seed, latent ground truth never visible to the policy, correlated
provider noise so "try the next provider" isn't free information every time.
Then a production backend (M1+): FastAPI ingestion API, a Postgres job queue
(`SELECT ... FOR UPDATE SKIP LOCKED`, no Redis/Celery/Temporal), a worker
that runs the policy and writes evidence/cost/decisions transactionally, a
human-review API for escalations, and a Decision Receipt endpoint that
answers "why did this stop here?" from persisted state alone.

**Decision policy.** Two independent stopping rules. *Settled*: given the
best- and worst-case values of every field still unknown, does the reachable
score already sit entirely on one side of a decision boundary? If so, no
purchasable evidence can change the outcome. *Confident*: a calibrated
confidence model (Platt-scaled, evaluated by ECE and reliability bins) versus
an autonomy threshold τ derived from a Clopper-Pearson upper bound on
selective error — not the raw observed rate, which is optimistic on small
samples. Either rule can fire first; neither subsumes the other.

**Hosted proof.** Not just built — deployed. Railway runs the API and worker
as two services against the same Supabase Postgres project; a public lead
submission reaches an autonomous decision, a second escalates to human
review with the machine's original recommendation preserved separately from
the human's action and the final outcome, and a third demonstrates shadow
mode — a full recommendation computed with zero authoritative effect. State
survives a live worker redeploy, proving it's the database persisting, not
the process.

**Trade-offs and the negative result.** The founding hypothesis — that
expected-value-of-information (EVoI) reasoning would beat a well-tuned fixed
policy — did not hold. An ablation with every stopping signal EVoI has,
minus EVoI-guided ordering, won on 9 of 10 seeds. Business value spans
$5–400 while provider prices span $0.001–0.60 — three orders of magnitude
apart — so the cost term barely moves EVoI's argmax and it degenerates into
"buy the most informative provider," which is expensive. The shipped policy
(`calibrated_bounds`) cuts modeled API spend **41.6%** versus a tuned
waterfall, at **2.3 percentage points lower** synthetic-oracle agreement —
a stated trade, not a win against the pre-registered bar (≤1pp loss at
≥20% cost reduction), which nothing met.

---

## Resume bullets

Pick 3–5 depending on the role. All are load-bearing on the actual repo —
verify against current code/docs before using if this page is old.

- Designed and benchmarked a cost-aware lead-enrichment decision policy
  against a synthetic ground-truth dataset (10 seeds, 300 held-out leads),
  comparing a full-information ceiling, an industry-standard waterfall
  baseline, and an expected-value-of-information controller — then shipped
  the policy that actually won the benchmark, not the more sophisticated one,
  and documented the 41.6%-cost-reduction / 2.3pp-agreement trade honestly
  against a pre-registered bar it didn't clear.
- Built a production FastAPI + Postgres backend around that policy: a
  transactional ingestion API, a `SKIP LOCKED` job queue with retry/backoff/
  dead-lettering (no Redis or Temporal), a calibrated-confidence-gated
  autonomous/human-review split, and a Decision Receipt endpoint that
  reconstructs *why* any past decision stopped where it did from persisted
  state alone.
- Shipped a real third-party provider integration (Abstract API company
  enrichment) behind the same `EnrichmentProvider` protocol the simulator
  implements, plus a shadow-evaluation mode that runs the full pipeline
  alongside an existing workflow with zero authoritative effect, gated so it
  can never accidentally spend real money outside explicit opt-in.
- Deployed the backend to Railway (two services, one Docker image, sharing a
  Postgres queue and a hosted Supabase database) and the frontend to Vercel,
  then proved it live end-to-end through the public API: an autonomous
  decision, a human-review escalation with the machine recommendation kept
  visibly separate from the human action and the final outcome, and state
  surviving a production worker redeploy.
- Built the operator-facing console in Next.js/TypeScript with a
  server-side proxy architecture (the backend has no CORS layer, by design),
  a typed HTTP contract mirrored 1:1 against the backend's actual schemas,
  and a Decision Receipt UI that never visually collapses machine
  recommendation, human action, and final outcome into one signal.

---

## Interview questions

**Why not just enrich every lead?**
Full enrichment scored highest on synthetic-oracle agreement (0.839) in the
benchmark, but at the highest cost (8 calls, $0.44/lead) — the ceiling, not
a realistic policy at volume. The whole project is the argument that a
cheaper policy can capture most of that value; the measured answer is it
captures ~97% of the agreement at ~55% of the cost.

**Why Postgres `SKIP LOCKED` instead of Redis/Celery/Temporal?**
[ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md). The deciding
factor is transactional consistency: a worker claims a job, runs one
state-machine step, and commits the lead's new status in the *same*
transaction as marking the job complete. A separate queue technology
reintroduces a dual-write hazard — the queue thinks work finished but the
lead state didn't move, or vice versa. Temporal is the right tool at
multi-service scale with cross-service sagas; this is one service against
one database at thousands of leads/day, nowhere near where Postgres polling
is a bottleneck. The queue interface (`claim`/`complete`/`fail`) stays
small on purpose so the backend could be swapped later without touching
policy code.

**Why calibration?**
A raw classifier score isn't a probability — a model that says "0.9" isn't
necessarily right 90% of the time. Calibration (Platt/isotonic, fit on
out-of-fold predictions grouped by company so no company leaks between
calibration and evaluation) makes the confidence number mean what it claims
to mean, measured by expected calibration error (ECE 0.061) and reliability
bins, not asserted.

**What is τ (tau)?**
The autonomy threshold — the calibrated-confidence value above which a
decision is taken without a human. It's derived from a **Clopper-Pearson
upper bound** on selective error at the target error rate, not the raw
observed error rate on calibration data. Zero errors in ten accepted
predictions is weak evidence at small sample sizes; the bound accounts for
that instead of taking the empirical rate at face value.

**What happened to the EVoI idea?**
[ADR 0004](adr/0004-evoi-is-a-negative-result.md). It's the project's
headline negative result. The founding hypothesis was that
expected-value-of-information reasoning would beat a well-tuned fixed
policy. An ablation — identical to the EVoI-driven policy except for
EVoI-guided provider ordering — beat it on 9 of 10 seeds, at both lower cost
and (in the single-seed case that looked good at first) not-actually-better
agreement. The failure mode: business value spans $5–400, provider price
spans $0.001–0.60 — three orders of magnitude apart — so the cost term
barely moves EVoI's argmax, and it degenerates into "buy the most
informative provider available," which is usually the expensive one. The
EVoI implementation, tests, and benchmark position are kept in the repo as a
documented negative result, not deleted.

**What did the benchmark actually prove?**
That a simple two-rule stopping policy (score-bounds "settled" + calibrated
"confident") beats a tuned industry-standard waterfall on cost by 41.6% at a
measured 2.3pp agreement cost — a real trade-off, explicitly *not* meeting
the pre-registered bar (≤1pp loss at ≥20% cost reduction) the project set
for itself before looking at results. It also proved several specific,
regression-tested things the hard way — a 5%-error target that was
small-sample luck, an EVoI estimator that paralyzed the policy from a cold
start, a budget cap silently set below the priciest provider — documented in
the README's "found the hard way" table rather than smoothed over.

**How does human review preserve auditability?**
The Decision Receipt keeps three facts distinct and never merges them: the
machine's `recommended_action` (frozen at decision time, in a dedicated
`decision_receipts` row written inside the same transaction that made the
decision), the human's `action`/`notes` (from the `human_reviews` table,
read live), and the `final_status` (the lead's live state). A human
approving a machine's "reject" doesn't rewrite what the machine said — the
receipt shows recommendation, human action, and final outcome side by side,
permanently. The frontend enforces the same separation visually; it's not
just a backend guarantee that a UI could still flatten.

**Why shadow mode?**
To let ARIE prove itself against a live workflow before it's trusted to
control anything. `mode: "shadow"` on ingestion runs the full pipeline —
real evidence acquisition, real cost if a live provider is enabled, a real
`decision_receipts` row — but lands on a dedicated terminal status
(`SHADOW_EVALUATED`) instead of an authoritative branch, and never opens a
human review. Pipeline metrics explicitly exclude shadow leads so a shadow
run can't quietly inflate real business numbers. It does *not* mean free —
if live-provider mode is on, shadow evaluation can still spend real money;
shadow only suppresses the *authoritative outcome*, not the underlying work.

**Why the Supabase Session Pooler instead of a direct connection?**
The project's Supabase instance's plain direct-connection host is
IPv6-only, unreachable from both the original dev machine and Railway's
network. The Session Pooler (port 5432, session-mode pgbouncer — one
backend connection per client for the life of the session) stands in for
both the pooled app connection and the migration connection. That's not a
workaround with a hidden cost: `scripts/migrate.py` opens one plain
`psycopg.connect()` and needs exactly the session-level DDL semantics
session-mode pgbouncer already provides. The actual thing to avoid is the
*Transaction* Pooler (port 6543, transaction-mode pgbouncer), which doesn't
guarantee those semantics — never in use here.

**Why separate API and worker services?**
They scale differently and fail differently. The API is a stateless HTTP
service behind a load balancer; the worker is a long-running poller with no
HTTP surface at all. Deploying them as one process would mean the API's
health/scaling story is entangled with the worker's job-processing load for
no reason — Railway (and `docker-compose.yml` locally, with two worker
replicas) treats them as two independently scalable things from the same
Docker image, differing only in start command.

**How would you scale this?**
Horizontally on both axes, with no architecture change. Workers coordinate
via `SKIP LOCKED` alone, so running more of them needs no new coordination
primitive. The API is stateless behind any load balancer. The real
bottleneck at scale would be Postgres write throughput on the jobs/evidence
tables — [ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md)
names the revisit trigger explicitly: north of ~1k jobs/sec, or a genuine
second service needing cross-service orchestration, is where Temporal-style
infrastructure would start earning its complexity.

**What would you change for production multi-tenancy?**
Nothing here has tenant isolation today — one Supabase project, one
provider budget, RLS disabled at the database level because the FastAPI
layer is the only thing that talks to Postgres directly (the browser never
does). Real multi-tenancy would need a `tenant_id` on every table and every
query path, per-tenant budget caps instead of one global
`LEAD_BUDGET_USD_CAP`, and RLS turned on as defense-in-depth once anything
besides this API can reach the database. None of that exists yet — it's a
known, named gap, not an oversight discovered here.

**What are the biggest limitations?**
The synthetic benchmark's honesty is also its limit — it proves the policy
beats alternatives against a *modeled* provider/noise distribution, not
against real-world data drift, adversarial providers, or vendor-specific
failure modes. Only one real provider is wired (Abstract API, two fields).
The only concurrency proof against the hosted deployment is small — 5
leads submitted simultaneously against the same identity, all settled
correctly with no duplicate processing (below) — not real load testing. No
auth/tenancy — this is a single-tenant proof, not a multi-customer product.
And the EVoI result stays open: [ADR 0004](adr/0004-evoi-is-a-negative-result.md)
names three concrete conditions (value normalized against cost scale, a
steeper price ladder, a latency-constrained setting) under which it might
actually win — none tested here.

---

## Portfolio claims

**Safe claims**

- A synthetic benchmark, honestly reported across 10 seeds, showing a
  calibrated two-rule stopping policy cuts modeled API spend ~41.6% versus a
  tuned waterfall baseline, at a measured ~2.3pp agreement cost.
- A negative result on the founding hypothesis (EVoI), reported rather than
  hidden, with the ablation that beat it and the reasoning why.
- A production backend with real transactional guarantees: exactly-once
  *effect* per job via `SKIP LOCKED` + idempotency keys, a Decision Receipt
  that reconstructs any past decision from persisted state.
- One real third-party provider integration, live-verified (not simulated)
  against Abstract API, with a documented real bug found and fixed during
  that verification.
- A hosted deployment (Railway + Supabase + Vercel) with a genuinely
  exercised end-to-end proof — autonomous decision, human-review escalation
  with the three-way distinction preserved, shadow evaluation, state
  surviving a production redeploy, and 5 concurrent submissions against the
  same identity all settling correctly (no duplicate processing, correct
  cache reuse under concurrency) — not just "it deploys."

**Claims to avoid**

- "Beats human-level decision quality" — never measured against human
  reviewers; the comparison is a synthetic oracle, not people.
- "Same quality as full enrichment" — it's 2.3pp worse, explicitly, and the
  README says so.
- "EVoI won" / "proves expected-value-of-information works" — the opposite;
  it's the project's headline negative result.
- "Zero quality loss" or any claim implying the pre-registered bar (≤1pp
  loss at ≥20% cost reduction) was met — it wasn't, on any seed.
- "Production-ready at scale" without qualification — no load testing beyond
  one small hosted reliability check, no multi-tenancy, no auth.
- "Saves $X in real provider spend" from the P6 hosted proof specifically —
  that proof ran with `PROVIDER_MODE=simulated`; its costs are modeled/
  configured figures, not real vendor spend. Only the P5 Abstract API
  verification involved actual billed calls (and at $0.00165/call, real but
  small).
