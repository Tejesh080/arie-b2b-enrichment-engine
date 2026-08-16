# ADR 0001 — No LangGraph for the enrichment loop

**Status:** Accepted · **Date:** 2026-08-16

## Context

Nearly every comparable open-source project in this space (see
[`../01-research.md`](../01-research.md)) builds its lead pipeline on LangGraph.
Using it would be the path of least resistance and the most familiar-looking
choice to a casual reader.

## Decision

Do **not** use LangGraph for the core enrichment loop. Use a hand-written state
machine over Postgres: a `leads` table holding current state, a pure function
`decide_next_action(state) -> Action`, and an append-only `lead_events` log.

## Rationale

The enrichment loop is **not agentic reasoning**. No LLM decides "should I call
Hunter next?" — an EVoI formula does. LangGraph's value proposition is dynamic
branching driven by an LLM's own tool selection, which is precisely what this
architecture deliberately avoids.

Adopting it anyway would violate the project's founding principle (*deterministic
before probabilistic*) while adding a dependency, a mental model, and a debugging
surface that buy nothing.

## Consequences

**Positive**

- The stopping rule is a pure, directly unit-testable function.
- State transitions are explicit and auditable; no framework-owned hidden state.
- The same policy code runs in the offline benchmark and in the live service.

**Negative**

- We write our own checkpointing (~150 lines against the `jobs` table).
- No free LangGraph Studio visualisation.
- Readers expecting LangGraph need this ADR to understand why it's absent —
  which is exactly why the ADR exists.

## When to revisit

If a Stage-4 open-ended research capability is added — where an LLM genuinely
selects among tools for deep investigation — LangGraph becomes appropriate *for
that isolated subgraph only*. The trigger is measurable: the `hard` difficulty
band showing leads that stay unresolved after all deterministic providers are
exhausted.
