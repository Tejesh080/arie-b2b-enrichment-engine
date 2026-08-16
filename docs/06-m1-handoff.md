# M1 Handoff

Written to be read cold, in a fresh session, with no memory of how M0 went.

---

## Where things stand

**M0 is complete.** The intelligence engine exists, is benchmarked, and its
result is published — including the part that failed. Everything runs offline
and deterministically; no credentials, no network.

**M1 has not started.** No database has been provisioned, no service written, no
n8n workflow built.

Read [`05-results.md`](05-results.md) before writing code. The single most
important thing to absorb: **the sophisticated policy lost to the simple one.**
Do not resurrect EVoI. Do not tune it. It is retained deliberately as a negative
result and [ADR 0004](adr/0004-evoi-is-a-negative-result.md) explains why.

---

## What to build on

The production policy is `arie.policy.production.CalibratedBoundsPolicy`.

```python
from arie.confidence.model import fit_confidence_model
from arie.policy.production import CalibratedBoundsPolicy

model = fit_confidence_model(calibration_leads, target_error_rate=0.10)
policy = CalibratedBoundsPolicy(model=model)
outcome = policy.run(lead, ctx)   # -> PolicyOutcome
```

Its dependencies, in the order M1 will need to wire them:

| Module | Role in M1 |
|---|---|
| `arie.scoring.rules` | ICP rules. Shared by oracle and runtime — do not fork. |
| `arie.scoring.engine` | Score bounds, evidence signals. Pure, no I/O. |
| `arie.confidence.model` | Fits on calibration data; produces τ. Refit, never pickle. |
| `arie.providers.base` | `EnrichmentProvider` Protocol — real adapters implement this. |
| `arie.providers.simulated` | Replays frozen observations. Keep for tests and CI. |
| `arie.policy.base` | `RunContext`, `EvidenceCache`, `CallLedger`. |

Everything in `src/arie/scoring/` and `src/arie/core/` is pure — no database, no
network, no framework imports. That is deliberate and worth preserving: it is
what lets the benchmark exercise the same code that runs in production rather
than a reimplementation of it.

---

## The five things most likely to be got wrong

**1. `is_settled` does not mean "certainly correct".**
Bounds are computed from *observed* facts, which are noisy. A settled decision
can still disagree with the oracle when a provider lied. Settled means "nothing
left worth buying"; confidence means "this is probably right". Wiring the
autonomy gate to `is_settled` would hand out unearned autonomy. Use
`confidence >= model.tau`.

**2. The confidence model must be refit, not pickled.**
It trains in seconds. A checked-in artefact drifts out of sync with the code that
produced it, silently. `fit_confidence_model` raises if handed a test-split lead
— keep that guard when you wire real data.

**3. τ is unstable and will move.**
Across seeds it ranges 0.69–0.93. It is a function of the calibration data, so it
will change every time you refit on new leads. Do not hardcode it, do not surface
it as a config constant, and do log it per fit. If τ ever comes back as `1.01`
(`REJECT_ALL_THRESHOLD`), the model could not certify any operating point and the
correct behaviour is to escalate everything — not to lower the bar.

**4. The 10% error budget is a policy choice, not a law.**
5% is not achievable on this model — see
`test_five_percent_budget_is_not_achievable`. If a stakeholder asks for 5%, the
honest answer is "then we automate nothing", not "we'll tune it".

**5. Cache hits must be recorded, not skipped.**
`CallLedger.record(..., cache_hit=True)` logs a zero-cost call. Dropping them
makes cache hit rate unmeasurable, and that metric is the justification for the
company-level evidence store.

---

## Schema already written

`migrations/0001_init.sql` and `0002_metrics_views.sql` exist and have never been
run against a live database. Expect to fix things.

Design notes worth honouring:

- `lead_events` is append-only and authoritative. `leads.status` is a
  materialised convenience that can be rebuilt by replay.
- **`evidence` IS the cache.** There is no separate cache subsystem to keep in
  sync — a hit is a row whose `expires_at` is still in the future.
- `voi_decisions` stores rejected candidates too. With EVoI out of production
  this table is either unused or repurposed to record *why enrichment stopped*
  (`settled` / `confidence_reached`). Repurposing is the better option; the audit
  trail is worth keeping.

The `EvidenceCache` in `arie.policy.base` is an in-memory stand-in for benchmark
runs. M1 replaces it with the `evidence` table plus TTL, behind the same
interface.

---

## Suggested order

1. **Supabase + migrations.** Run `0001`/`0002` for real. Use the *pooled*
   connection string for workers, direct only for migrations.
2. **Evidence store with TTL**, replacing `EvidenceCache`. This is the cache, and
   it is where the company-level cost saving actually lives.
3. **Identity resolution** — deterministic domain/email normalisation only.
   Splink is deferred; the ambiguous-identity subset in the dataset exists to
   tell you whether exact matching is failing enough to justify it. Measure
   before adding it.
4. **Job queue** — `SKIP LOCKED`, backoff, dead-letter. Claim the job and commit
   the lead's state transition in *one* transaction; that is the whole reason
   Postgres was chosen over Redis ([ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md)).
5. **State machine + worker** running `CalibratedBoundsPolicy`.
6. **FastAPI ingest**, one `POST /leads`.
7. **Cost ledger + metric views** — `0002_metrics_views.sql` is already written.
8. **LLM signal extraction** (DeepSeek), one narrow task: buying signals from
   free text. Measure it as a delta against the deterministic baseline. If it
   does not move decision agreement, cut it — that is the precedent this project
   has already set once.
9. **Human approval path**, then **n8n edge workflows** last.

---

## Do not

- Resurrect or tune EVoI.
- Add RAG or pgvector. No use case survived scrutiny.
- Add LangGraph. The loop is a calculation, not agentic reasoning
  ([ADR 0001](adr/0001-no-langgraph-for-core-loop.md)).
- Add Temporal or Redis at this scale ([ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md)).
- Call anything an "agent" that is a deterministic function.
- Replace the simulator. It is what keeps CI free and the benchmark reproducible;
  real adapters go *alongside* it behind the same Protocol.

---

## Guard rails to keep green

```bash
make lint && make type && make test    # ruff, mypy --strict, 162 tests
make validate-dataset                  # dataset must stay non-trivial
python -m bench.multi_seed             # ~10 min; run before claiming an improvement
```

CI runs the benchmark on every push with zero credentials. If a change to
scoring, confidence, or the policy moves the numbers, that is a result and
belongs in `05-results.md` — not a regression to be silenced.

The dataset content hash is in `data/eval/manifest.json`. If it changes
unexpectedly, something in the generator changed and every prior number is no
longer comparable.

---

## Environment

- Supabase project `phkytiiwrkhuyedhkfrd`, MCP server registered in `.mcp.json`
  at project scope. OAuth must be completed interactively (`claude` then `/mcp`)
  — it cannot be done from a non-interactive session.
- DeepSeek API key available. Anthropic/OpenAI not confirmed.
- n8n connected via MCP.
- Firecrawl key to be provided.
- `gh` CLI is **not** installed; push over HTTPS with the existing git remote.
- Python 3.11, venv at `.venv/`. Note `mypy` cannot follow numpy's stubs on 3.11
  — already handled via a `follow_imports = "skip"` override in `pyproject.toml`.

---

## The honest framing, for the README and for interviews

This project built a sophisticated thing, built the ablation that could kill it,
and reported that the ablation won. The temptation in M1 will be to quietly make
the sophisticated version the story again because it sounds better.

Do not. The negative result *is* the story, and it is a stronger one.
