# ARIE — Adaptive Revenue Intelligence Engine

**Lead enrichment that decides when to stop buying data.**

Most enrichment pipelines call every data provider for every lead, then ask a
model to score the result. ARIE asks a different question first: *given what I
already know, is the next API call worth paying for?*

**[Live demo](https://arie-web.vercel.app/)** · [Frontend repo](https://github.com/Tejesh080/arie-decision-console) · [Docs](#documentation)

![The ARIE Decision Console overview: the decision schematic, and recently submitted leads with their live status and modeled cost](docs/assets/console-overview.png)

---

## Why it exists

A sales team buys contact and company data per lookup. The usual pipeline runs
every provider on every lead, because deciding which ones to skip is harder than
just calling them all. Most of that spend buys nothing — the answer was already
obvious three providers ago.

ARIE treats it as a stopping problem instead. It buys the cheapest evidence
first, and after each purchase asks two questions: *could anything I haven't
bought yet still change this answer?* and *am I confident enough to act without
a person?* When both say no more is needed, it stops and decides.

The interesting part is what happens when it isn't confident. ARIE doesn't guess.
It hands the lead to a human, records what it would have done, and keeps both
records side by side afterwards — so you can always see where the machine and
the person disagreed.

I built this to find out whether that kind of adaptive stopping actually beats a
well-tuned fixed pipeline. **It does, on cost — and it costs you some accuracy.**
The honest numbers are [below](#results).

---

## How it works

```mermaid
flowchart LR
    A["New lead"] --> B["Buy cheapest<br/>useful evidence"]
    B --> C{"Could more data<br/>change the answer?"}
    C -->|yes, and affordable| B
    C -->|no| D{"Confident enough<br/>to act alone?"}
    D -->|yes| E["Route or reject<br/>automatically"]
    D -->|no| F["Send to a human"]
    E --> G["Decision Receipt"]
    F --> G
```

Two separate rules, answering two different questions. *Settled* asks whether
anything left to buy could still flip the outcome. *Confidence* asks whether the
answer is actually right. A decision can be settled and still wrong, so neither
rule replaces the other.

More detail in [architecture.md](docs/architecture.md).

---

## What you can try

On the [live demo](https://arie-web.vercel.app/), submit a lead and watch it
resolve one of three ways:

| | What happens |
|---|---|
| **Autonomous decision** | Confidence clears the threshold, so ARIE routes or rejects the lead on its own. |
| **Human review** | Confidence falls short. ARIE stops, explains why, and waits for a person to approve, reject, or override. |
| **Shadow evaluation** | ARIE computes the full recommendation and takes no action at all — useful for running it alongside an existing workflow to see what it *would* have done. |

The form offers one-click examples that reliably produce each outcome, plus a
shadow-mode toggle — and accepts any identity you type, which gets its own
deterministic simulated evidence.

---

## The Decision Receipt

This is the part I'd point at first.

Every lead produces a receipt: what ARIE decided, how confident it was, why it
stopped buying data, what that cost, and which providers were involved. It is
reconstructed from stored facts, not re-derived later, so a receipt from three
months ago still explains a decision made under a policy version you've since
replaced.

One rule holds it together: **a machine recommendation and a human's decision
never collapse into a single "outcome" field.** If ARIE said reject and a
reviewer approved, the receipt shows both, in order, permanently.

| Autonomous decision | Human override |
|---|---|
| ![Receipt for an autonomously routed lead: 83.2% confidence against an 80.4% autonomy threshold, the stopping reason, and the provider ledger](docs/assets/receipt-autonomous.png) | ![Receipt for a lead ARIE recommended rejecting that a reviewer approved, showing machine recommendation, human action and final outcome as three separate stages](docs/assets/receipt-human-override.png) |
| Confidence cleared the threshold, so ARIE acted alone. | ARIE recommended reject; a reviewer approved. Both records stand. |

The provider ledger is deliberately blunt about waste. It separates evidence
bought fresh from evidence reused out of cache, and it names any provider that
charged for a call and returned nothing.

---

## Architecture

```mermaid
flowchart LR
    U["Browser"] --> V["Vercel<br/>Next.js proxy"]
    N["n8n Cloud"] --> R
    V --> R["Railway — API"]
    R <--> S[("Supabase<br/>Postgres")]
    W["Railway — worker"] <--> S
```

The API writes identity resolution, the lead row and its first job in one
transaction. A worker claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, so
adding workers needs no coordination between them. No Redis, no Celery, no
Temporal — Postgres already gives transactional consistency with the lead state
those jobs mutate.

The same database carries a multi-tenant layer on top of that: Supabase-issued
sessions and scoped API keys for auth, row-level security per organization,
BYOK provider credentials in Supabase Vault, and a self-serve commercial layer
— signup, Stripe subscriptions, plan entitlements, transactional email.
Entitlements only ever decide what an organization may *configure*; they can
never grant autonomy the calibration data doesn't support, and every
entitlement change goes through one signature-verified Stripe webhook rather
than a browser redirect. All of it is optional to run — with no Stripe, email,
or CAPTCHA credentials configured, the system still boots and behaves
correctly, which is how the demo above runs.

Full topology, environment variables and rollback path:
[deployment.md](docs/deployment.md). Design decisions and the ones deliberately
rejected: [architecture.md](docs/architecture.md) and [docs/adr/](docs/adr/).

---

## Results

Ten seeds, 300 held-out test leads each, dataset regenerated and the baseline
re-tuned per seed.

| policy | agreement | API $/lead | calls | autonomy |
|---|---|---|---|---|
| full enrichment (call everything) | 0.8390 | 0.4447 | 8.00 | 0.816 |
| tuned waterfall (industry baseline) | 0.8347 | 0.4205 | 7.58 | 0.795 |
| **calibrated bounds** ← production | **0.8113** | **0.2463** | 5.26 | **0.833** |
| adaptive EVoI | 0.8093 | 0.2906 | 2.19 | 0.786 |

**41.6% cheaper than a tuned waterfall, at 2.3 percentage points lower decision
agreement.**

Two things I'd rather say up front than have you find later. First, the bar I set
before running anything was ≤1pp agreement loss at ≥20% cost reduction, and
nothing met it — this is a trade-off, not a win. Second, the standard deviation
on that saving is 11.0pp, which is large next to the effect.

The project's founding idea was expected-value-of-information. It lost, to a much
simpler policy, on 9 of 10 seeds. That negative result is written up rather than
buried: [ADR 0004](docs/adr/0004-evoi-is-a-negative-result.md).

Method, dataset design and every parameter assumption: [benchmark.md](docs/benchmark.md).

---

## Simulated vs. real providers

Worth being precise about, because they're easy to conflate.

**The hosted demo runs in simulated mode.** Known example identities replay a
frozen evaluation corpus; any other identity gets deterministic synthetic
evidence generated from the same provider catalogue and noise model, seeded by
the lead's own email and domain — so the same lead always resolves the same
way, and a second contact at the same company reuses its cached company
evidence. No vendor is called and no money is spent either way, so the cost
figures you see are modelled cost at configured provider rates — not billed
spend. Everything
around it is real: real Postgres queue, real worker, real persistence, real
receipts, real human-review workflow, real n8n orchestration.

**Three real provider integrations exist**, all behind the same interface the
simulator implements: Abstract API's Company Enrichment (firmographics),
Hunter's Combined Enrichment, and Apollo's People Enrichment (both supplying
seniority and function, deliberately overlapping so they can be compared).
Abstract was verified with real billed calls — verifying it turned up a genuine
bug on the first one, documented rather than quietly fixed. Hunter and Apollo
are complete and green against fixtures and mocked transports; their real smoke
calls are pending API keys.

Live mode has two explicit strategies. The **optimized** default walks the
providers cheapest-first (Abstract $0.00165 → Hunter ~$0.0049 → Apollo
~$0.0196, all modelled figures) and stops the moment existing evidence answers
the question — a lead that firmographics already settle costs one cheap call,
not three. A private **evaluation** strategy deliberately calls the person
providers in parallel on controlled identities so their coverage, quality,
latency, credits, and agreement can be measured (`scripts/provider_bakeoff.py`)
before any waterfall order is declared the winner. A provider whose credits run
out is cooled down from the durable ledger rather than re-dialled, and the
pipeline continues on the remaining vendors.

Details of both: [provider-integration.md](docs/provider-integration.md).

---

## Tech stack

Python 3.12 · FastAPI · Postgres (Supabase) · pytest · Docker
Next.js 16 · React 19 · TypeScript · Tailwind CSS v4 · Motion
Railway (API + worker) · Vercel (frontend) · n8n Cloud (edge workflows)
Supabase Auth + Vault · Stripe · OpenTelemetry

---

## Run locally

Reproduce the benchmark — no API keys, no network:

```bash
pip install -e ".[dev,service]"
make dataset      # generate the seeded evaluation set
make bench        # single-seed benchmark
python -m bench.multi_seed   # 10 seeds
```

Or run the whole stack and watch it decide, escalate, and honour an override.
Needs Docker:

```powershell
.\scripts\demo.ps1
```

The demo brings up Postgres, the API and the worker, submits a few leads from the
frozen corpus, and prints their receipts.

---

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | How it works, the invariants, what's where in the code |
| [benchmark.md](docs/benchmark.md) | Dataset design, measured results, every assumption |
| [deployment.md](docs/deployment.md) | Hosted topology, config, migrations, rollback |
| [provider-integration.md](docs/provider-integration.md) | The real adapter, and shadow mode |
| [portfolio.md](docs/portfolio.md) | Short explanations, and what not to claim |
| [docs/adr/](docs/adr/) | Decision records, including the negative result |

---

## Limitations

- The benchmark is synthetic. It proves the policy against a modelled provider
  distribution, not against real-world data drift or vendor-specific failures.
- Three real providers are wired, populating four of seven scored fields.
  `buying_intent`, `recent_trigger_event`, and `disqualifying_flag` have no
  trustworthy source and stay honestly unknown — which keeps the score floor at
  zero for every live lead.
- Hunter's and Apollo's adapters have not yet made a real API call. Both
  contracts were verified against the vendors' published documentation and
  every path is covered by fixture and mock tests, but "verified against the
  docs" is not "verified against the API".
- The cheapest-first provider order is a reasoned prior, not a measured result.
  The bake-off harness exists to replace it with data; until a controlled live
  run happens, no order claims to be optimal.
- The commercial layer is built and tested end to end against a mocked Stripe
  boundary — real local HMAC signatures, no Stripe account required — but it
  has not yet processed a real Stripe test-mode payment, and no transactional
  email has been sent to a real inbox. Both are dashboard-side setup, not code.
- The hosted concurrency check is small — five simultaneous submissions
  completed without duplicate processing. That is not load testing.
- `arie.llm` (DeepSeek buying-signal extraction) is built and tested but not
  called by the worker; ingestion carries no free-text field for it yet.

---

## License

MIT — see [LICENSE](LICENSE).
