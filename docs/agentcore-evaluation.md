# Amazon Bedrock AgentCore Runtime — evaluation and outcome

**Status: an evaluated AWS-native deployment path, not adopted.** ARIE's
production request path does not use AgentCore. The Runtime described here was
deployed to AWS, measured, and left in place as a working reference; no ARIE
traffic is routed through it.

Dated 2026-09-23. Every number below was measured, not estimated. Where a figure
is a list price rather than a measurement, it says so.

---

## 1. Why AgentCore was evaluated

ARIE's Ask ARIE copilot calls Amazon Bedrock. ARIE runs on Railway, which is not
AWS, so the ordinary way to make that call is an IAM user's access key stored as
a Railway environment variable — a long-lived AWS credential living outside AWS.

AgentCore Runtime offers an alternative: a runtime configured with a **JWT
authorizer** is reachable with an OAuth bearer token instead of SigV4, so the
caller needs no AWS credential at all. That property — and only that property —
is what made AgentCore worth evaluating for this workload.

The secondary question was whether hosting the model call inside AWS, adjacent
to Bedrock, would pay for the extra network hop it introduces.

## 2. What was actually deployed

One narrowly scoped service. It performs a single stateless Bedrock call and
holds no application state.

```
Railway (ARIE core, unchanged)                AWS
────────────────────────────────              ─────────────────────────────
tenant authorization                          AgentCore Runtime (HTTP, ARM64)
evidence retrieval (Postgres/RLS)               GET  /ping
budget authorization                            POST /invocations   :8080
untrusted-data fencing                            ApplyGuardrail (question only)
        │  HTTPS + Cognito M2M bearer              Converse → Nova Micro
        └───────────────────────────────────▶    returns text + usage + cost
citation/evidence-membership gate     ◀──────┘
cost ledger
```

| Component | Value |
|---|---|
| Runtime ARN | `arn:aws:bedrock-agentcore:us-east-1:698271072326:runtime/arie_decision_copilot-Z8puGdHz1m` |
| Endpoint | `DEFAULT`, `READY`, live version 2 |
| Protocol / network | HTTP / **PUBLIC** (smoke test only; VPC not enabled) |
| Image | `arie-decision-copilot:v0.1.1`, ARM64 (Graviton), 68.5 MB |
| Model | `amazon.nova-micro-v1:0` |
| Guardrail | `quwmqu430jgj` version **1** (immutable) |
| Source | `services/copilot-runtime/{app.py,Dockerfile}` |

**Everything deterministic stayed on Railway**: scoring, routing, evidence
acquisition, identity validation, tenant authorization, budget authorization,
the citation/evidence-membership gate, and the cost ledger. The Runtime holds no
database connection — asserted by a test that imports it in a clean subprocess
and fails if `psycopg`, `sqlalchemy` or `asyncpg` appear in its import graph.

The Runtime returns **raw text**, not a validated object. `arie.llm.structured`
states that validation happens exactly once, against the Pydantic model; only a
JSON Schema can cross a network boundary, and a schema is a weaker statement
than the class it came from. Railway validates.

## 3. Measured latency

Client in Australia, runtime in `us-east-1`. A bare TLS GET to the AgentCore
endpoint from the same client costs **1033 ms**, so a large share of every wall
figure below is geography rather than AgentCore.

| Measurement | Value |
|---|---|
| **Cold start** (after 9 minutes idle) | **2474 ms** |
| Warm p50 in that same session | 1882 ms |
| **Cold-start penalty** | **592 ms** |
| **Warm p50, keep-alive** (v0.1.1) | **1632 ms** (min 1581, max 6503) |
| Warm p50, keep-alive (v0.1.0) | 1695 ms |
| Warm p50, new connection per call | 2580 ms |
| In-container service time p50 | 777–832 ms |
| └─ Bedrock Converse, in-region | **295–310 ms** |
| └─ Guardrail screen, in-region | ~480–580 ms |
| AWS server-side `Latency` metric (23 invocations) | avg **1584 ms**, min 946, max 6165 |

**Direct Bedrock baseline — same client, same region, no AgentCore:**

| Measurement | Value |
|---|---|
| Guardrail screen + Converse, p50 | **1038 ms** |
| └─ Converse alone | 529 ms |
| └─ Guardrail screen alone | 509 ms |

**AgentCore adds roughly 600–840 ms, about +63%.**

Two details worth keeping. The container's own startup is sub-second — its logs
record `Started server process` and `Application startup complete` in the same
second — so the 592 ms cold-start penalty is AgentCore provisioning, not the
image. And the Converse leg is *faster* inside the Runtime (295–310 ms) than
directly from the test client (529 ms), because the container is co-located with
Bedrock; the added ingress hop costs more than that saving returns.

These numbers were taken from a client in Australia. A caller in a US region
would see a smaller delta. **That was not measured and is not claimed.**

## 4. Measured cost per request

| Request type | Model | Guardrail | Total |
|---|---|---|---|
| Safe | $0.000010955 | $0.00040 | **$0.000410955** |
| Blocked by guardrail | $0 | $0.00040 | **$0.00040** |

A blocked request pays for one guardrail assessment and no completion, because
the screen runs before the model.

Guardrail cost is 3 text units — topic $0.00015 + content $0.00015 + sensitive
information $0.00010 per 1,000-character unit (AWS Price List API, us-east-1,
effective 2026-09-01). **The guardrail costs roughly 37× the model call it
protects.** Nova Micro list price is $0.035/1M input and $0.14/1M output tokens.

AgentCore Runtime compute is additional and was not isolated per request; the
published rates are $0.0895/vCPU-hour and $0.00945/GB-hour.

## 5. Least-privilege execution role

`AmazonBedrockAgentCoreARIEDecisionCopilotRole`

Trust: `bedrock-agentcore.amazonaws.com`, with `aws:SourceAccount` and
`aws:SourceArn` conditions for confused-deputy protection.

| Permission | Scope |
|---|---|
| `bedrock:InvokeModel` | `amazon.nova-micro-v1:0` only |
| `bedrock:ApplyGuardrail` | `guardrail/quwmqu430jgj` only |
| `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents`, `Describe*` | `/aws/bedrock-agentcore/runtimes/*` only |
| `ecr:BatchGetImage`, `GetDownloadUrlForLayer`, `BatchCheckLayerAvailability` | `repository/arie-decision-copilot` only |
| `ecr:GetAuthorizationToken` | `*` (not resource-scopable) |
| `xray:PutTraceSegments`, `PutTelemetryRecords`, `cloudwatch:PutMetricData` | `*` (not resource-scopable) |

No Secrets Manager, no IAM, no S3, no database, no other model, no other
guardrail.

`logs:CreateLogGroup` is required. Without it the log pipeline fails **silently**
— successful invocations, zero streams, zero bytes, no error anywhere.

## 6. Cognito machine-to-machine authentication

| Item | Value |
|---|---|
| User pool | `us-east-1_9cW6xbaT5` |
| Resource server / scope | `arie-copilot` / `invoke` |
| App client | `2ean23u0o7t6vj6cv81ib638kk`, `client_credentials` only |
| Token | RS256, 3600 s TTL |

The Runtime's authorizer validates tokens against the pool's OIDC discovery URL
and accepts only that client id. Measured: **29 invocations, 29 inbound
authorization successes, 0 failures, 0 errors.** An unauthenticated call
returns **403**.

**End-user Supabase JWTs are deliberately not forwarded.** ARIE's budget gate
(`authorize_llm_call`) runs on Railway; if an end-user token were sufficient to
reach the Runtime, any authenticated customer could spend Bedrock budget without
passing it. Machine-to-machine credentials preserve the property that only
ARIE's server can invoke.

This does not eliminate secrets — it changes their kind. A Cognito client secret
is not an AWS access key, is scoped to one resource server, and is
independently rotatable and revocable.

## 7. Guardrail v1

Version **1** is immutable, cut from the DRAFT that the guardrail suite
validated: 2 topic policies (credential extraction, cross-tenant disclosure),
6 content filters, 10 PII entities, no regexes. The Runtime is configured with
`BEDROCK_GUARDRAIL_VERSION=1`, and every smoke-test response reports `v=1`.

The guardrail screens the **customer's question only**, via a standalone
`ApplyGuardrail` call — never Converse's inline `guardrailConfig`. The inline
form assesses the model's *response* as well, and this guardrail `ANONYMIZE`s
`NAME` and `EMAIL`: a verified probe asking "who is the contact?" returned
literally `{NAME}, {EMAIL}`. ARIE's evidence block carries contact details the
calling organization is already authorized to read, so masking them produces a
wrong answer rather than a safer one. Screening separately also halves guardrail
cost — one assessment pass (3 units) instead of two (6 units).

## 8. CloudWatch and KMS

| Item | Value |
|---|---|
| Log group | `/aws/bedrock-agentcore/runtimes/arie_decision_copilot-Z8puGdHz1m-DEFAULT` |
| Encryption | KMS, `alias/arie-copilot-runtime-logs` |
| Retention | 30 days |
| Metrics namespace | `AWS/Bedrock-AgentCore` |

The log group name is **endpoint-qualified** (`<runtime-id>-DEFAULT`). The
un-suffixed name receives nothing.

**No secrets or test payloads were detected across 1,468 inspected CloudWatch
log lines.** The scan looked for `Bearer`, `Authorization`, `AKIA`, `ASIA`,
`SessionToken`, JWT-shaped `eyJ`, the Cognito client secret, and the specific
question and evidence strings used in testing; none occurred. AgentCore does
not inject request/response payload logging into this container — its logs are
uvicorn access lines only.

This is a statement about what a scan of those lines found, not a guarantee
about all future log content: the workload was synthetic, and a different code
path could log something these patterns would not catch. KMS encryption and
finite retention remain as defence in depth for that reason.

## 9. Why AgentCore is not in ARIE's production request path

The Runtime hosts **one stateless Bedrock call**. It uses no agent loop, no
multi-turn sessions, no tools, no Memory, no Gateway, and no multi-agent
behaviour. Session isolation, the capability AgentCore Runtime principally
provides, is irrelevant to a single-turn stateless call. Observability was
already available through ARIE's existing OpenTelemetry tracing, and portfolio
traffic volumes do not require autoscaling.

Against that, adoption would add a container image, a registry, a Cognito pool,
an ARM64 build step, and a network hop measured at **+600–840 ms** — while still
leaving a long-lived secret in Railway, now a Cognito client secret rather than
an AWS key.

The same Bedrock guarantees — least-privilege model access, guardrail v1
enforcement, full cost accounting — are already available to ARIE by calling
Bedrock directly with an IAM principal, with no additional infrastructure and
without the extra hop.

> **AgentCore was validated as an AWS-native deployment path for ARIE's Bedrock
> copilot, but was not adopted as the production path because the current
> workload does not require agent sessions/tools/memory and the additional
> network hop adds complexity and latency.**

## 10. What this evaluation does *not* claim

- ARIE production traffic does **not** run through AgentCore.
- AgentCore did **not** change decision accuracy. It transports a model call;
  the model, prompts and guardrail are identical either way.
- AgentCore is **not** required for ARIE. Bedrock is reachable directly.
- Gateway and Memory were **not** implemented.
- VPC network mode is **not** enabled — the Runtime runs in PUBLIC mode, which
  is appropriate for a smoke test and not for production.
- US-region caller latency was **not** measured.

## 11. Related

- `services/copilot-runtime/app.py` — the Runtime, and why it returns raw text
- `src/arie/llm/bedrock_provider.py` — guardrail scoping and the model choice
- `src/arie/llm/agentcore_provider.py` — the client, behind the same `LLMProvider` seam
- `tests/unit/test_copilot_runtime.py` — container contract and the no-database assertion
- `tests/live/test_bedrock_guardrail_live.py` — opt-in live guardrail suite
