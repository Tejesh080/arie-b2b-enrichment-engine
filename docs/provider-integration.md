# Provider integration

ARIE talks to data providers through one `EnrichmentProvider` Protocol. A
simulated registry and a real vendor adapter both implement it, and the policy
cannot tell them apart.

> **Which mode runs where.** The public hosted demo runs
> `PROVIDER_MODE=simulated`: it replays a frozen corpus for known identities and
synthesizes deterministic evidence for the rest, so no vendor is called
> and no money is spent. Cost figures there are modelled cost at configured
> rates. The real adapter below is separate, and was verified with real billed
> calls. Do not conflate the two.

---

## The real adapter

ARIE's provider abstraction is wired to one real external service —
**Abstract API's Company Enrichment endpoint**. The point is that the same
`EnrichmentProvider` Protocol the simulator implements also fronts a live
vendor, without touching the frozen benchmark catalogue.

- **Why this one.** A plain `GET` with `api_key`/`domain` query params, a free
  tier (100 requests/month, no card), and a response whose *field names* —
  `employee_count`, `industry` — match ARIE's own `SCORED_FIELDS` exactly. No
  scraping, no browser automation.
- **Matching field names are not matching vocabularies.** P5 shipped assuming
  they were: `industry` arrived as Abstract's own category string, was
  lower-cased, and was handed to the scorer, where `"computer software"` is not
  a key and scored **0.0** — indistinguishable from "we assessed this industry
  and found it worthless". The Live V1 Foundation's canonical taxonomy layer
  (below) fixes that. The defect is recorded here rather than quietly deleted,
  because it is the clearest possible statement of why that layer exists.
- **Env:** `ABSTRACT_COMPANY_API_KEY` (see [`.env.example`](../.env.example)).
  `PROVIDER_MODE=live` refuses to build a worker without it — no silent
  fallback to the simulator.
- **What it populates:** `employee_count` and `industry` for the company
  entity of *any* ingested lead (unlike simulated mode, live mode has no
  frozen-corpus restriction). Every other `SCORED_FIELDS` entry
  (`buying_intent`, `recent_trigger_event`, `disqualifying_flag`,
  `title_seniority`, `title_function`) stays unknown in live mode — there is
  no live source for them yet.
- **Cost model:** an *estimated* unit cost
  (`ABSTRACT_COMPANY_COST_USD_PER_CALL`, derived from Abstract's list price,
  not a per-call figure the provider reports), recorded through the same
  `provider_calls` ledger as every simulated call.
- **Cache reuse, not just a real API call.** A second lead at an
  already-enriched company is served from the durable `evidence` table at
  zero cost — recorded as a zero-cost cache hit, never a silent skip. That is
  the actual interesting claim: ARIE can decide it does *not* need to spend
  real money, not merely that it can make one real HTTP call.
- **Live verification:**

  ```bash
  python scripts/live_provider_smoke.py --domain github.com --confirm-live-spend
  ```

  Calls the real adapter once and prints the normalized result. Pass
  `--api-base-url http://localhost:8000` (with a `PROVIDER_MODE=live` worker
  already running) to also drive a real lead through ingestion and print its
  Decision Receipt — a second real call, which the script says out loud
  before making it.

  Run for real against `github.com` **after** the canonical taxonomy layer:
  Abstract returns the literal string `"Computer Software"`, which now maps to
  canonical `software` — 15.0 ICP points, up from 0.0 — alongside
  `employee_count: 2579`, at `$0.00165`. The result carries the raw-to-canonical
  pair the adapter used, so the mapping is checkable in production rather than
  inferred:

  ```json
  "normalization": {
    "mapped": [
      {"field": "employee_count", "raw": "2579", "canonical": 2579},
      {"field": "industry", "raw": "Computer Software", "canonical": "software"}
    ],
    "unmapped": []
  }
  ```

  The same call before this change produced `industry: "computer software"` and
  an autonomous rejection at score 8.0. Verification also caught a real vendor
  quirk worth knowing about: Abstract 301-redirects the documented `/v2` path to
  `/v2/`; the adapter's `base_url` carries the trailing slash and its client
  sets `follow_redirects=True` as a backstop.

**Deliberately not built:** a second provider, a provider registry/marketplace,
retries beyond one bounded attempt, or any claim that this adapter's accuracy
matches the simulator's declared assumptions — that comparison is exactly what
ADR 0003 defers to "once a real provider is wired," and remains unmeasured
here. See [`deployment.md`](deployment.md)
for the full account, including live-verification status.

---

## Live V1 Foundation

Four things stand between a real provider call and a real business decision.
None of them is optional, and none is a configuration flag.

### 1. No autonomous action on real-provider evidence

A lead enriched by a real provider **cannot** be auto-routed or rejected
without a human. It terminates at `AWAITING_HUMAN` (with a real, actionable
`human_reviews` row) or, if ingested as shadow, at `SHADOW_EVALUATED`.

The reason is narrow and specific: `tau` is fitted on the *synthetic
calibration split*, and its guarantee — at most `TARGET_AUTONOMOUS_ERROR_RATE`
of autonomous decisions are wrong — holds over that distribution and no other.
Real provider evidence has different coverage, different error modes, and
different correlation between fields. Applying that threshold to it is an
unmeasured claim wearing a calibrated number's clothes.

This is not env-configurable (`arie.live.safety`). The gate that lifts it is a
*measurement* — real-world validation and recalibration — not a deployment
switch someone can flip under time pressure. **Simulated mode is untouched**:
its autonomy is validated against an oracle on held-out data, and the demo and
benchmark still exercise it.

The Decision Receipt keeps recommendation, action, and outcome as three
separate facts: `decision.recommended_action` is what ARIE concluded (never
rewritten), `decision.autonomous` is `false`, and `decision.autonomy_guard`
says *why* — so a reviewer seeing an escalated `auto_route` recommendation can
tell "ARIE was unsure" from "ARIE was sure and is not yet permitted to act".

### 2. A canonical taxonomy the scorer owns

```
RAW PROVIDER RESPONSE -> provider adapter -> normalized evidence
                      -> canonical ARIE vocabulary -> evidence store -> scorer
```

`arie.normalization.taxonomy` maps real vendor strings onto the scorer's own
closed vocabularies, explicitly (an alias table) and then heuristically
(ordered, word-boundary phrase rules). `arie.normalization.contract` is the
boundary every adapter passes through; nothing else may build evidence from a
vendor payload. No raw provider vocabulary reaches the scorer.

The canonical sets **are** the scorer's sets, widened only with
recognised-but-unscored families (`construction`, `financial_services`,
`healthcare`, ...) and an `unknown` sentinel. No scoring weight changed, and
the frozen M0 benchmark output is byte-for-byte identical.

### 3. Unknown is not zero

This is the distinction the whole layer exists for:

| | score | bounds | completeness |
|---|---|---|---|
| **known negative** — `"Construction"`, mapped deliberately, not the ICP | 0.0 | tightened | counts as known |
| **unknown** — `"Pet Grooming Franchises"`, no mapping | 0.0 | stays open | counts as missing |

Same number, opposite epistemics. Only the first is a reason to stop buying
evidence. `arie.scoring.rules.is_unknown` is what separates them, and
`arie.scoring.engine` reads it for both bounds and completeness. An unmappable
value is reported in the adapter's `unmapped` audit rather than stored — the
raw string is preserved so a missing alias-table row is actionable rather than
a mystery.

### 4. Spend caps, checked before the call

Two ceilings, enforced against the durable `provider_calls` ledger (so they
hold across workers and restarts, not per-process) *before* any request is
made:

| variable | default | approx. Abstract calls |
|---|---|---|
| `LIVE_PROVIDER_DAILY_BUDGET_USD` | `2.00` | ~1,200/day |
| `LIVE_PROVIDER_PER_LEAD_BUDGET_USD` | `0.05` | ~30/lead |

Server-side only; never exposed to a browser. A refusal is a first-class stop
reason (`per_lead_budget_exhausted` / `daily_budget_exhausted`) that sends the
lead to a human — never a silent skip, and never a decision made on partial
evidence pretending to be complete. A provider timeout or error is likewise
its own stop reason (`provider_failed`) rather than being folded into "every
provider was called", which would claim the evidence does not exist rather than
that ARIE failed to fetch it. Neither case fails the job: a vendor outage is
not a reason to lose a real lead.

Known limit, stated rather than papered over: the cap is *soft* under
concurrency. Two workers can both read the same total and both proceed. The
overshoot is bounded by (concurrent workers times per-call cost) — cents — and
the exact alternative buys that precision with a new failure mode.

### The reference ICP

`arie.icp.REFERENCE_ICP_V1` is one named, versioned profile — B2B software/SaaS,
50-1,000 employees, US/UK/AU/CA, sold to revenue-side leaders — in one place
rather than as constants scattered through adapters. It is *a* reference
profile chosen to make the live path concrete, not a claim about every
business, and it is **descriptive, not a second scorer**: `arie.scoring.rules`
remains the only thing that turns facts into a number.

Two folds are recorded rather than hidden. `revenue_operations` and `growth`
have no weight in the scorer, so they map to `operations`/`marketing` —
introducing them as canonical values would make ARIE's own highest-intent
functions score 0.0. `head` maps to `director` rather than `vp`, the
conservative direction.

Geography and the student/freelancer disqualifiers are **declared but
unobservable**: no configured provider supplies them. They are reported as
`UNOBSERVABLE` rather than dropped or, far worse, guessed. ARIE never invents a
`disqualifying_flag`.

### Second provider: Apollo person enrichment

Abstract supplies company firmographics only, which left `title_seniority` (20
points) and `title_function` (15) unknown for every live lead — 35 of the
scorer's 100 reachable points. Apollo's People Enrichment endpoint
(`POST /api/v1/people/match`, `x-api-key` header, matched on the work email
ARIE already holds) closes that gap.

The adapter arrived in two reviewable halves, and the split survives:
`arie.providers.apollo_contract` is the raw→canonical mapping, with no HTTP
client and no credential, tested entirely against fixtures in
`tests/fixtures/apollo/`; `arie.providers.live_apollo` is the transport that
sits behind it. Everything the second emits still goes through the first.

**Cost is modelled from credits, not from dollars Apollo reports.** Apollo
meters this endpoint in credits — one for a demographics match, zero when it
finds nobody. `APOLLO_PERSON_COST_USD_PER_SUCCESS` converts that at a stated
rate ($49/month ÷ 2,500 credits = $0.0196), and the ledger row carries
`credits_consumed` alongside the dollars so the modelled figure is never
mistaken for billed spend. This corrected an earlier estimate in the codebase
that put Apollo credits "well under a cent" — verification against the
published plans put them an order of magnitude higher.

`reveal_personal_emails` and `reveal_phone_number` are sent as `false`
explicitly. A returned mobile number costs eight extra credits, and ARIE scores
neither — they would be PII fetched for no decision-relevant purpose.

Apollo's intent and job-change data is deliberately unused: `buying_intent` is
the largest field in the ruleset and the vendor's methodology is not
inspectable.

### Third provider: Hunter combined enrichment

Hunter's Combined Enrichment (`GET /v2/combined/find`, `X-API-KEY` header)
returns a Clearbit-style person+company pair for one email at **0.2 credits per
successful enrichment** — modelled at ~$0.0049 from the published Starter plan,
making it the cheaper of the two person providers and second in the default
order. Its no-match is a **404** (free), and its error table is inverted from
convention: 403 means rate-limited, 429 means quota exhausted — the adapter
maps per Hunter's documentation, not per habit.

Two Hunter-specific mapping decisions live in `arie.providers.hunter_contract`:
seniority is parsed **title-first** (Hunter's five-value enum folds C-level and
VP into one `executive` bucket, and trusting it first would over-credit every
VP), while function keeps the enum-first rule (no coarseness problem). The
combined response's company half — Abstract's own two fields — rides on the
result as a canonical-audit preview for the bake-off and is deliberately **not
persisted as evidence** until measurements justify it.

### Strategies, cooldowns, and the bake-off

`LIVE_PROVIDER_STRATEGY` selects between the **optimized** waterfall (the
default: selective, sequential, cheapest-first, early-stopping — with two
person providers selling the same fields, a provider whose fields are already
held from *another* source is skipped as redundant with no fabricated ledger
row) and **evaluation_parallel**, a private experiment mode that calls both
person providers concurrently per lead under its own explicit budget, records
every call separately, classifies cross-provider agreement
(AGREE/PARTIAL/CONFLICT/UNKNOWN over canonical values, symmetrically — no rule
prefers a vendor), and self-identifies on the receipt
(`versions.policy = live_evaluation_parallel`). Conflicting answers keep both
provenance rows, feed the merge layer's score-relevant `contested` flag, and
land in front of a human.

A provider that returns a credit/quota-exhausted error (Apollo 402, Hunter
429, Abstract 422) enters a ledger-backed cooldown
(`LIVE_PROVIDER_QUOTA_COOLDOWN_SECONDS`): the quota row in `provider_calls`
(migration 0010 added `error_kind`, `credits_used`, `cost_basis`) keeps every
worker from re-dialling a dead allowance, with no retry storm and no new
infrastructure. Leads that needed the cooled provider stop with
`provider_unavailable` and go to a human; everything else continues.

`scripts/provider_bakeoff.py` is the measurement instrument: a controlled
identity list in, per-provider match/title/canonical/usable rates, latency
percentiles, credits, modelled cost-per-useful-result, cross-provider
agreement, and the Hunter-vs-Abstract company overlap out — plus how often
Abstract alone would have ended optimized acquisition, computed with the
pipeline's own stopping rule. `--mock` proves the harness with zero spend;
real runs require every key, `--confirm-live-spend`, a `--limit`, and a
`--max-spend-usd` ceiling, and cache their results so re-runs never re-spend.

### Conditional acquisition (the optimized default)

Ordering is deterministic — Abstract, then Hunter, then Apollo
(`arie.live.providers.REGISTERED_LIVE_PROVIDER_NAMES`, overridable for
experiments via `LIVE_PROVIDER_ORDER`). Company evidence is an
order of magnitude cheaper and is shared by every future lead at the same
employer; person evidence is per-person and can never be amortised that way, so
buying it before finding out whether it is needed is the expensive mistake.

Between the two, the loop re-asks the existing stopping rule. A five-person
construction firm is a confident reject on firmographics alone, so Apollo is
never contacted and the receipt reports it under `providers.not_called` with a
`confidence_reached` stop reason. A 240-person software company sits near the
qualify boundary with seniority unknown, so Apollo is called. Both paths are
asserted end-to-end in
`tests/integration/test_live_multi_provider_integration.py`.

This is **not** a marketplace or an EVoI optimiser over live providers. It is
the smallest arrangement that makes "did ARIE decide it needed to spend?" a
question the system can actually be asked.

---


## Shadow mode

ARIE can observe a live enrichment workflow and record what it would have
done — evidence acquired, cost spent, confidence reached, decision reached —
**without controlling the downstream routing outcome.** No autonomous action,
no human review opened, nothing an outcome-sync consumer would treat as
finalized.

Requested per lead, not with a server-wide switch — `POST /leads` takes an
optional `"mode": "shadow"` (default `"normal"`, and normal mode's behaviour
is completely unchanged):

```bash
curl -X POST http://localhost:8000/leads \
  -H "Content-Type: application/json" \
  -d '{"source": "shadow-demo", "email": "nadia.haddad@cobalt500.com", "external_ref": "shadow-1", "mode": "shadow"}'
```

The lead still runs the full acquisition loop — real evidence, real cost if
`PROVIDER_MODE=live` is enabled, a real `decision_receipts` row — but instead
of branching into `AUTO_ROUTED`/`AWAITING_HUMAN`/`REJECT`, it lands on a
dedicated terminal, `SHADOW_EVALUATED`, and never calls `request_review`. The
receipt's top-level `"shadow": true` field makes this explicit, and
`v_pipeline_metrics`/`v_escalation_rate` exclude shadow leads so a shadow run
never inflates real business metrics.

**"Shadow" does not mean "free."** If `PROVIDER_MODE=live` is on, a shadow
lead's evidence acquisition can still make a real, billed provider call —
shadow mode only suppresses the *authoritative outcome*, not the underlying
work. And no counterfactual savings are claimed here: P5 does not compare
shadow-mode spend against a baseline enrichment workflow's actual cost unless
one is explicitly supplied, and none is in this repo.



---
