# ADR 0003 — Simulated providers are the default, not a testing convenience

**Status:** Accepted · **Date:** 2026-08-16

## Context

The benchmark must compare enrichment strategies across a cost-threshold sweep.
Run against live APIs, that means real money per run, non-reproducible results
(provider data changes underneath you), and a repo nobody else can verify.

## Decision

`SimulatedProvider` is the default implementation of the `EnrichmentProvider`
Protocol. Real adapters implement the same Protocol and are opt-in via
`PROVIDER_MODE=live`.

## Rationale

This is a **property of the project**, not a shortcut. The benchmark is the
central claim; a claim nobody can reproduce is not evidence. Simulator-first
gives:

- **Reproducibility** — seeded, deterministic, byte-identical across machines
  and across time.
- **Zero-cost CI** — the full benchmark runs on every push, forever, for free.
- **Controlled realism** — coverage, noise, latency, price, and *correlated
  failure* are explicit parameters rather than uncontrolled confounds.

Correlation matters more than it first appears: if provider misses were
independent, "just try the next one" would always eventually succeed, and the
benchmark would never meaningfully test the *stopping* decision. Real providers
fail together on the same messy accounts, so the simulator models a shared
per-lead obscurity draw.

## Consequences

**Positive**

- Anyone can clone and reproduce every published number with one command.
- Strategy comparison is apples-to-apples — identical provider behaviour across
  all three strategies.
- No API budget constrains iteration speed.

**Negative — stated plainly**

- **Simulator parameters are assumptions, not measurements.** Every one is
  documented with its justification in [`../ASSUMPTIONS.md`](../ASSUMPTIONS.md).
  Results are conditional on those assumptions being roughly right, and the
  README says so.
- A live demo needs at least one real adapter wired (planned, deferred).
- Risk of tuning the simulator until the desired result appears. Mitigated by
  fixing parameters *before* any benchmark run and versioning the dataset; if
  parameters change, both versions' results are reported, not silently replaced.

## When to revisit

Once ≥1 real provider is wired, compare its observed coverage/latency/failure
distribution against the simulator's assumed profile and publish the delta. That
turns an assumption into a measurement, and is the honest way to strengthen the
result.
