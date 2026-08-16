# ADR 0004 — Expected-value-of-information is a negative result

**Status:** Accepted · **Date:** 2026-08-16

## Context

The project's founding thesis was that expected-value-of-information reasoning
would let a lead-qualification system buy less data without deciding worse. The
EVoI controller was built, benchmarked, and — on a single seed — appeared to
work: 29.0% cheaper than a tuned waterfall at a 1.00pp agreement cost.

Two things undid that.

**An ablation.** `CalibratedBoundsPolicy` was built to answer the question a
sceptic asks first: the EVoI policy stops most leads on `decision_settled`, so
is the value-of-information machinery contributing anything, or are the score
bounds underneath doing the work? The ablation has every stopping signal the
EVoI policy has, except EVoI-guided ordering.

**Ten seeds instead of one.** The 1.00pp gap was three leads out of three
hundred.

## Decision

Adopt the ablation as the production policy. Keep the EVoI implementation, its
tests, and its benchmark position as a documented negative result. Do not tune
it further.

## Evidence

Mean across 10 seeds, on 300 held-out test leads:

| | agreement | API $/lead | calls |
|---|---|---|---|
| calibrated_bounds | 0.8113 | 0.2463 | 5.26 |
| adaptive_voi | 0.8093 | 0.2906 | 2.19 |

EVoI costs **16.1% more** than the policy without it, at slightly worse
agreement, on 9 of 10 seeds. It is also more expensive at every human-review
price tested.

## Why it failed

Business value ranges $5–400 while provider prices range $0.001–0.60. The cost
term is three orders of magnitude smaller than the value term, so it barely
moves the argmax and EVoI degenerates into "buy the most informative provider
available".

The signature is visible in the call counts: EVoI makes 2.19 calls to the
ablation's 5.26 and still spends more. It reaches straight for the expensive
high-information sources, while cheapest-first frequently reaches the confidence
threshold before touching them. **Fewer calls is not cheaper when the calls are
the expensive ones.**

## Consequences

**Positive**

- The production policy is simpler: two stopping rules, no value model, no
  business-value assumptions, nothing to calibrate beyond the confidence model.
- One fewer set of assumptions in the critical path. EVoI required a business
  value per deal tier and a uniform prior over reachable scores; both are gone.
- The result is more interesting than a marginal win. The project built the
  sophisticated version, built the ablation that could kill it, and reported
  that the ablation won.

**Negative**

- The founding thesis is not supported. The README says so.
- Some genuinely good machinery is now unused in production: the flip-probability
  estimator and the escalation-aware extension both work as designed and neither
  earns its cost.

## What would change this

Not pursued here, recorded so the door stays open:

1. **Normalise value against provider cost scale** so the cost term actually
   bites. The current failure is arguably a units problem rather than a
   conceptual one.
2. **A steeper provider price ladder.** With costs spanning three orders of
   magnitude instead of two, choosing well would matter more.
3. **A latency- or rate-limit-constrained setting**, where EVoI's real advantage
   — reaching a decision in 2.19 calls instead of 5.26 — is the binding
   constraint rather than a curiosity.

Point 3 is the most promising: EVoI is decisively better at *calls*, and this
benchmark simply does not price that.
