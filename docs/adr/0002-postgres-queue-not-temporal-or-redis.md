# ADR 0002 — Postgres `SKIP LOCKED` queue instead of Temporal or Redis

**Status:** Accepted · **Date:** 2026-08-16

## Context

The system needs durable execution: survive crashes, retry with backoff,
dead-letter after N attempts, resume from checkpoints, and never double-charge a
provider on retry. The obvious candidates are Temporal (journal/replay), Redis +
Celery/RQ, or Postgres-native queueing.

## Decision

A single `jobs` table polled with `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis,
no Temporal, for the MVP.

## Rationale

**Transactional consistency is the deciding factor.** A worker claims a job, runs
one state-machine step, and commits the lead's new status *in the same
transaction* as marking the job complete. There is no window where the queue
believes work finished but the lead state didn't move — a dual-write hazard that
both Redis-backed queues and any external orchestrator reintroduce.

Temporal is genuinely the right tool at multi-service scale with cross-service
sagas. This is one deployable service against one database. Its journal/replay
model would also require wrapping every non-deterministic call as an activity —
real work, for a guarantee we already get from a database transaction.

Volume is thousands of leads/day, not thousands/second — nowhere near where
Postgres polling becomes a bottleneck.

## Consequences

**Positive**

- One fewer service to run, deploy, and explain.
- Exactly-once *effect* via transactional state transitions + idempotency keys.
- Queue depth, retry counts, and dead letters are inspectable with plain SQL.

**Negative**

- Polling latency (~2s) instead of push delivery. Irrelevant for this workload.
- No built-in workflow visualisation or scheduling primitives.
- We own backoff, lease expiry, and dead-lettering logic.

## When to revisit

Stated explicitly in the docs rather than left implicit: **> ~1k jobs/sec, or a
genuine second service needing cross-service orchestration.** The queue interface
(`claim`, `complete`, `fail`) is deliberately small so the backend can be swapped
without touching business logic.
