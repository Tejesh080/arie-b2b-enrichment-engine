# Provider integration

ARIE talks to data providers through one `EnrichmentProvider` Protocol. A
simulated registry and a real vendor adapter both implement it, and the policy
cannot tell them apart.

> **Which mode runs where.** The public hosted demo runs
> `PROVIDER_MODE=simulated`: it replays a frozen corpus, so no vendor is called
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
  tier (100 requests/month, no card), and a response whose field names —
  `employee_count`, `industry` — match ARIE's own `SCORED_FIELDS` exactly. No
  scraping, no browser automation, no new normalization layer.
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

  Run for real against `github.com`: `employee_count: 2579`,
  `industry: "computer software"`, cost `$0.00165`, autonomously rejected
  (score 8.0 — Abstract's free-text industry string doesn't match ARIE's
  closed vocabulary, the exact caveat above, seen live). Also caught a real
  bug worth knowing about: Abstract 301-redirects the documented `/v2` path
  to `/v2/`; the adapter's `base_url` now carries the trailing slash and its
  client sets `follow_redirects=True` as a backstop.

**Deliberately not built:** a second provider, a provider registry/marketplace,
retries beyond one bounded attempt, or any claim that this adapter's accuracy
matches the simulator's declared assumptions — that comparison is exactly what
ADR 0003 defers to "once a real provider is wired," and remains unmeasured
here. See [`deployment.md`](deployment.md)
for the full account, including live-verification status.

---

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
