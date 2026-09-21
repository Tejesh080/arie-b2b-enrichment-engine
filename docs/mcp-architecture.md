# MCP engineering interface

A [Model Context Protocol](https://modelcontextprotocol.io) server
(`src/arie_mcp/`) that gives an AI agent eleven read-only tools for
inspecting this system's *runtime* state — job queue health, provider
errors, enrichment spend, routing decisions, schema drift — without shelling
out arbitrarily or being handed credentials wide enough to write anything.
Full design rationale lives in
[specs/mcp-engineering-interface.md](../specs/mcp-engineering-interface.md);
this document is the "what actually exists and how to use it" companion.

**Two transports, one tool registry.** `arie_mcp.server.build_server()`
registers all eleven tools exactly once; both entrypoints below call it —
neither redefines a tool or its DB access.

- **stdio** (`src/arie_mcp/server.py`, `python -m arie_mcp.server`) — local
  only, no auth (nothing to authenticate: it's a subprocess Claude Code
  itself launches and owns). Unchanged since V0.1.
- **Streamable HTTP + OAuth** (`src/arie_mcp/http_server.py`, `python -m
  arie_mcp.http_server`) — the remote transport, deployed as its own Railway
  service (`arie-mcp`, alongside `arie-api` and `arie-worker` in the same
  project — a third role on the same image, picked by Custom Start Command
  exactly as the worker already is). See
  [Remote transport](#remote-transport-streamable-http--oauth) below.

> **Status.** V0.1, read-only. Originally validated stdio-only against a
> local development database; the remote transport has since been added and
> verified end-to-end (local OAuth + Streamable HTTP round trip, and against
> the deployed Railway service with the production `arie_mcp_readonly` role)
> — see [Remote transport](#remote-transport-streamable-http--oauth). No
> state-changing tool exists in either transport — see
> [Known limitations](#known-limitations).

---

## Architecture

```mermaid
flowchart LR
    CC["Claude Code\n(local process)"] -- stdio --> MCP["arie_mcp server\nsrc/arie_mcp/"]
    MCP -- "SELECT-only\nmcp_diag schema" --> RO[("Postgres\narie_mcp_readonly role")]
    MCP -- "GET /openapi.json\n(local/dev API only)" --> API["arie.api.main\n(uvicorn, optional)"]
    MCP -- "append-only JSONL" --> AUDIT[("local audit log\nvar/mcp-audit/")]
```

- **Transport**: stdio only. Claude Code launches `python -m
  arie_mcp.server` as a subprocess it owns; there is no network listener, so
  there is nothing to firewall or expose by mistake.
- **Database access**: a dedicated Postgres role, `arie_mcp_readonly`
  (migrations `0039`, `0040`), that can `SELECT` from exactly the views in
  the `mcp_diag` schema — nothing else. `default_transaction_read_only = on`
  and a 5-second `statement_timeout` are set at the *role* level, so even a
  bug in this server's own SQL cannot write or hang past that ceiling; it
  isn't just a client-side promise.
- **API access**: `inspect_api_contract` is the one tool that talks to
  `arie.api.main` (a running `uvicorn` process, default
  `http://localhost:8000`) instead of the database, since its whole purpose
  is reading the *live* generated OpenAPI schema.
- **Audit log**: every tool call — success or failure — appends one JSONL
  line to `var/mcp-audit/arie-mcp-YYYY-MM-DD.jsonl` (gitignored, local-only,
  never routed through the DB role). See [Audit trail](#audit-trail).
- **Two outbound dependencies, both optional at startup.** No DB, no API —
  the server still starts and advertises all eleven tools; a tool that needs
  the missing dependency reports that cleanly per call. See [Failure
  behaviour](#failure-behaviour-verified).

## Tool catalogue (V0.1 — all read-only)

| Tool | Reads | Needs |
|---|---|---|
| `get_system_health` | queue depth, worker heartbeats, migration currency | DB |
| `inspect_api_contract` | the running API's live OpenAPI schema | API |
| `inspect_migrations` | applied vs. pending migrations, checksum status | DB |
| `inspect_job` | one job's full detail by `job_id` | DB |
| `list_failed_jobs` | paginated `failed`/`dead_letter` jobs | DB |
| `get_recent_errors` | aggregated job + provider-call failures in a window | DB |
| `list_providers` | the live-provider registry and acquisition order | — (process-static) |
| `inspect_provider_health` | quota cooldown, recent error rate, for one provider | DB |
| `get_enrichment_costs` | cost/credit rollup, by provider or by day | DB |
| `inspect_routing_decision` | the VoI acquisition trail for one lead | DB |
| `inspect_configuration` | env-var *presence* (never values) + org entitlements | DB (org part only) |

Every tool returns the same envelope (`ok`, `data`, `error_code`, `message`,
`truncated`, `row_count`, `correlation_id`) and is capped in output size,
timed out, and audited — see specs § 9–12 for the exact shape.

## Claude Code configuration

Project-scoped `.mcp.json` at the repo root (this file is gitignored in this
repo — see the comment above its entry in `.gitignore` — because a previous
version leaked a Supabase project ref in a URL; keep it that way and don't
re-track it):

```jsonc
{
  "mcpServers": {
    "arie-mcp": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "arie_mcp.server"],
      "cwd": "/absolute/path/to/adaptive-revenue-intelligence-engine",
      "env": {
        // Claude Code expands ${VAR} / ${VAR:-default} from the launching
        // shell's own environment — the literal secret is never written
        // into this file. See "Supplying MCP_READONLY_DATABASE_URL" below.
        "MCP_READONLY_DATABASE_URL": "${MCP_READONLY_DATABASE_URL}",
        "ARIE_MCP_API_BASE_URL": "${ARIE_MCP_API_BASE_URL:-http://localhost:8000}",
        "ARIE_MCP_AUDIT_LOG_PATH": "${ARIE_MCP_AUDIT_LOG_PATH:-var/mcp-audit}"
      }
    }
  }
}
```

`command: "python"` resolves through whatever shell launched `claude`, so
that shell needs the project's `mcp` extra installed first:

```bash
pip install -e ".[dev,service,mcp]"
```

### Supplying `MCP_READONLY_DATABASE_URL` safely

Never place a real connection string in `.mcp.json`, even though it's
gitignored here — the file is tooling, not a secrets store, and `${VAR}`
interpolation exists precisely so it never has to hold one. Instead:

1. **Local development** (recommended day-to-day target — never production):
   ```bash
   docker compose up -d db
   export TEST_DATABASE_URL=postgresql://arie:arie_local_dev@localhost:5432/arie_test
   export ARIE_ALLOW_INTEGRATION_TEST_DB=1
   python scripts/test_db.py designate   # first time only
   python scripts/migrate.py --target test --apply
   ```
   Then set a password on the (shipped-inert, per migration `0039`'s own
   docstring) `arie_mcp_readonly` role once, using the *admin* local
   connection — never `DATABASE_URL` — and export the resulting readonly
   URL in the shell that will launch `claude`:
   ```bash
   psql "$TEST_DATABASE_URL" -c "ALTER ROLE arie_mcp_readonly PASSWORD '<generate one>';"
   export MCP_READONLY_DATABASE_URL=postgresql://arie_mcp_readonly:<that password>@localhost:5432/arie_test
   ```
2. **Never** derive it from `DATABASE_URL`/`DATABASE_DIRECT_URL`. Those are
   this project's production Supabase credentials (`scripts/migrate.py`'s
   own docstring documents the incident that makes this a hard rule, not a
   style preference) — `arie_mcp.settings.Settings` deliberately never falls
   back to them either.
3. A CI/test run doesn't need any of this by hand:
   `tests/mcp/conftest.py`'s `mcp_readonly_password` fixture generates and
   sets a random, session-scoped password on the disposable test database
   itself, every run.

## Remote transport (Streamable HTTP + OAuth)

The point of the remote transport is to reach these same eleven tools from
an ordinary Claude conversation — Claude.ai, Desktop, mobile, or Cowork —
added once as a **Custom Connector**, with no local process, no PowerShell,
and no environment variable on the caller's own machine. Everything below
is additional to stdio, not a replacement for it — `python -m
arie_mcp.server` is unchanged and still the right choice for local
Claude Code development.

**Deployment.** A third Railway service, `arie-mcp`, in the same project as
`arie-api` and `arie-worker` — same repo, same Dockerfile (now installing
the `mcp` extra alongside `service` so one image serves all three roles),
Custom Start Command `python -m arie_mcp.http_server`, Healthcheck Path
`/healthz` (a bare liveness probe with no DB dependency and no auth — never
`get_system_health`, which is an authenticated tool that queries the
database and would take the whole service out of rotation over a transient
DB hiccup). Neither `arie-api` nor `arie-worker` was touched to add this.

**Database.** `MCP_READONLY_DATABASE_URL` on this service only, pointed at
production's `arie_mcp_readonly` role (same role, same `mcp_diag`-only
grants, same `default_transaction_read_only`/5s statement timeout as the
local stdio setup above) — never `DATABASE_URL`. `arie_mcp.server
.build_server()` is the single call site both transports use to open this
connection; the remote entrypoint never opens a second one.

**Authentication.** OAuth 2.1 with Dynamic Client Registration (RFC 7591)
and PKCE (S256) — the mechanism Claude's own connector documentation
recommends for a server with no existing multi-tenant client population,
using the MCP Python SDK's own `mcp.server.auth` machinery (the real
`/authorize`/`/token`/`/register`/`/revoke` routes and discovery metadata,
not a hand-rolled protocol) rather than a bearer token or API key. One
service, acting as both the OAuth authorization server and the resource
server — the same "legacy combined AS+RS" shape the SDK's own bundled
`examples/servers/simple-auth` reference uses, appropriate here because
there is exactly one legitimate caller population (the server's owner, via
Claude) rather than a multi-tenant client base that would justify splitting
the two. `src/arie_mcp/auth.py` (`ArieOAuthProvider`) is the whole
implementation:

- **One password, no username** (`MCP_OWNER_PASSWORD`, no default — the
  server refuses to start without it, loudly, rather than binding a public
  port with nothing protecting it). Compared with `secrets.compare_digest`;
  gated further by a per-source-IP rate limit (10 attempts/hour) on the
  login endpoint.
- **In-memory client/code/token storage.** Correct for one replica (this
  service holds no product traffic and never scales horizontally) and
  simplest to audit. A redeploy invalidates every session — Claude re-runs
  DCR and the owner signs in again once; an inconvenience, not a
  correctness gap.
- **Refresh tokens, rotated.** Access tokens live one hour; refresh tokens
  live 90 days and are rotated (old one invalidated, new one issued) on
  every use, per OAuth 2.1's token-theft mitigation for public clients —
  every DCR-registered client is one. Claude refreshes proactively (five
  minutes before expiry) and reactively (on a 401), so a connected session
  stays usable indefinitely without the owner repeating the consent flow.
- **No write path, same as stdio.** The provider only ever issues tokens
  scoped to reading the eleven existing tools; there is no code path here
  that could authorize anything else, because nothing else exists to
  authorize.

**Endpoint:** `https://arie-mcp-production.up.railway.app/mcp` (Streamable
HTTP). Login page at `/login`, OAuth discovery at
`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource`, both served automatically by the
SDK's auth routes.

## Security boundary

- The `arie_mcp_readonly` role cannot `SELECT` from any `public.*` base
  table — only from explicitly `GRANT`ed views in `mcp_diag`. Verified
  live in this session: querying `leads` directly through that role raises
  `InsufficientPrivilege`, not merely "not exposed by a tool".
- No view in `mcp_diag` selects `persons`, `companies`,
  `evidence.value`/`raw_ref`, `provider_calls.raw_response_ref`,
  `organization_provider_configs.vault_secret_id`,
  `organization_api_keys` (hashes), Stripe customer/subscription IDs, or
  anything in `vault.*` — structurally unreachable at the schema layer, not
  filtered after the fact in Python.
- `inspect_configuration` reports env-var *presence* only
  (`{"stripe_configured": true, ...}`), never a value, computed from
  `os.environ` key presence — no value ever leaves the process.
- `jobs.last_error` is redacted (`src/arie_mcp/redaction.py`) for email
  addresses, DB connection URLs, bearer tokens, and API-key-shaped strings
  *before* truncation, so a secret can't survive half-cut in the truncated
  remainder. This is a narrow, honest pattern set — not fuzzy PII detection
  — and a bare human name with no accompanying secret shape is documented,
  not silently assumed away (`tests/mcp/integration/test_pii.py`).
- Every DB statement is capped at 5 seconds at the *role* level; every tool
  call is capped at 10 seconds wall-clock regardless of what it does
  internally; every list-shaped result caps at 100 rows server-side.

## A real debugging workflow

This is an actual sequence run against this repository's local development
database (`docker compose`'s Postgres, `arie_test` — never production) in
the validation session that produced this document, through a real stdio
MCP session (the same `mcp` Python SDK client Claude Code itself is built
on). Nothing below was fabricated to make a good demo; the local dev
database already had this genuine finding sitting in it from earlier real
work.

**1. `get_system_health`** — a live look, no DB probing by hand:

```json
{
  "database_reachable": true,
  "schema_up_to_date": true,
  "pending_migrations": [],
  "queue": { "pending": 0, "processing": 0, "failed": 0, "dead_letter": 5, "done": 111 },
  "worker_fleet": { "active_workers": 0, "most_recent_heartbeat_at": null, "stale": true }
}
```

Five dead-lettered jobs, no active workers — worth a closer look.

**2. `list_failed_jobs {"status": "dead_letter"}`** — all five are
`compute_score` jobs, all failed after 4 attempts, all with the same shape
of `last_error`:

```
compute_score expects a NEW lead; lead 3940c65a-b967-4e09-8b1f-ca1872c1cc35 is AWAITING_HUMAN
```

**3. `inspect_job`** on one of them adds the piece `list_failed_jobs` alone
didn't have — who was holding the lease when it finally died:

```json
{
  "job_id": "2344a443-8736-4563-956c-3dbd767cb8a4",
  "status": "dead_letter",
  "attempt_count": 4,
  "locked_by": "demo-smoke",
  "lead_status": "AWAITING_HUMAN",
  "lead_is_shadow": false
}
```

**4. The finding.** `compute_score` is the job ingestion enqueues for every
*new* lead (`src/arie/jobs/handlers.py:901-908`), and it refuses to run
unless the lead is still `LeadStatus.NEW` — a real invariant, not a bug in
the guard itself. `locked_by: "demo-smoke"` traces straight to
`tests/integration/test_demo_smoke_integration.py` (confirmed by grep — that
literal string is a test-suite worker identity, not a production caller).
The lead in question had already walked the *entire* real pipeline —
`NEW → SCORING → FETCHING_EVIDENCE → INTEGRATING → DECISION → AWAITING_HUMAN`
(`src/arie/statemachine/transitions.py:44-58`) — by the time this
particular `compute_score` job finally got a worker. That only happens if a
second, stale `compute_score` job existed for a lead whose *real* run had
already finished and moved it on. **Next place to look**: whether
`tests/integration/test_demo_smoke_integration.py` (and
`test_icp_profiles_integration.py`, same `locked_by` pattern) can enqueue a
duplicate `compute_score` job for a lead under retry/crash conditions, or
leaves one behind after driving the lead through a separate fast path. This
reads as a test-isolation artifact from local runs, not a live production
defect — but MCP is what made the *shape* of the problem (a orphaned
duplicate job racing the real pipeline, not five unrelated failures)
visible in three tool calls instead of a manual SQL session.

**Did MCP reduce the need for direct DB/shell inspection?** Yes, materially,
for the diagnosis itself: three typed, capped, audited tool calls
(`get_system_health` → `list_failed_jobs` → `inspect_job`) produced the same
facts a hand-written `SELECT ... FROM jobs WHERE status = 'dead_letter'`
session would have, with schema/PII/timeout guarantees a raw psql session
doesn't have by default. Locating *why* — the actual source files and the
`locked_by` → test-file trace — still took `Grep` over the checked-out repo,
which is correct: MCP answers "what is the running system doing," not "what
does the code that produced this say," and was never designed to replace
the second half.

## Audit trail

Confirmed from this session's real `var/mcp-audit/arie-mcp-*.jsonl` (paths
and IDs below are as actually produced; redacted only where a real value
would be sensitive, and nothing here was):

```json
{"ts":"2026-09-20T17:17:33.228337Z","correlation_id":"856c89a5617e4719a2b700add2644c6d","tool":"get_system_health","input":{},"outcome":"ok","error_code":null,"duration_ms":507,"row_count":null,"truncated":false,"caller_pid":4000,"mcp_server_version":"0.1.0"}
{"ts":"2026-09-20T17:17:34.070824Z","correlation_id":"42736b52b3e14c1c9a864236946820d1","tool":"list_failed_jobs","input":{"status":"dead_letter","job_type":null,"since":null,"limit":20},"outcome":"ok","error_code":null,"duration_ms":3,"row_count":5,"truncated":false,"caller_pid":4000,"mcp_server_version":"0.1.0"}
{"ts":"2026-09-20T17:17:34.149376Z","correlation_id":"dffc54b1cc4b4cbd9ea7e035ae6a7329","tool":"inspect_routing_decision","input":{"lead_id":"3940c65a-b967-4e09-8b1f-ca1872c1cc35"},"outcome":"error","error_code":"NOT_FOUND","duration_ms":4,"row_count":null,"truncated":false,"caller_pid":4000,"mcp_server_version":"0.1.0"}
```

Every line in the real file got its own `correlation_id`, independent of the
others — confirmed by inspecting all 14 lines the session produced, not
just these three. No line, across either this run or a second run that
exercised the DB-unavailable and API-unavailable paths, contains a
connection string, password, or any `mcp_diag`-excluded field.

## Failure behaviour (verified)

Both checked with a real stdio session, not read from the spec:

- **`MCP_READONLY_DATABASE_URL` unset**: the server still starts and still
  advertises all eleven tools. `get_system_health` degrades gracefully —
  it's designed to report `database_reachable: false` as *data*, not an
  error, since that's exactly the signal it exists to surface. Every other
  DB-backed tool returns a clean typed error instead:
  ```json
  {"ok": false, "data": null, "error_code": "DB_UNAVAILABLE",
   "message": "MCP_READONLY_DATABASE_URL is not configured", ...}
  ```
- **API process unreachable** (`inspect_api_contract` pointed at a dead
  port): also a clean typed error, no hang, no crash:
  ```json
  {"ok": false, "data": null, "error_code": "UPSTREAM_UNAVAILABLE",
   "message": "could not reach the API's OpenAPI schema at http://127.0.0.1:1", ...}
  ```
  Neither case raised an exception through the MCP protocol layer or ended
  the session; both are ordinary tool results a caller reads and moves on
  from.

## Known limitations

- **No write path exists.** `replay_failed_job`, database writes, admin
  endpoints, arbitrary SQL, arbitrary shell, and control over Railway/GitHub
  Actions/Vercel are all explicitly out of scope for V0.1 (spec § 3, § 15) —
  not implemented, not partially wired, not planned for this slice.
- **`inspect_routing_decision` has nothing to show on a database with no
  `voi_decisions` rows** (true of the local dev database at the time this
  document was written — simulated-mode local runs don't always populate
  it) — it correctly returns `NOT_FOUND` rather than fabricating a result,
  but that means the tool is effectively unexercised until a lead with a
  real acquisition trail exists locally.
- **`inspect_api_contract` requires a separately-running API process.**
  MCP does not start one; if `arie.api.main` isn't already up at
  `ARIE_MCP_API_BASE_URL`, the tool degrades cleanly (above) rather than
  doing anything about it.
- **Audit log is local-only and unauthenticated.** It's a plain file an
  operator with filesystem access can read or delete; there's no tamper
  protection and no remote shipping in V0.1.
- **Single-tenant assumption in a few tools.** `inspect_provider_health` and
  `get_enrichment_costs` report global aggregates across all organizations
  when `organization_id` is omitted (labelled `"scope": "global"` precisely
  so a caller can't mistake it for one tenant's view) — there's no per-call
  authorization boundary between organizations, appropriate for this
  project's current single-operator usage but worth flagging before any
  multi-tenant-facing use.
- **This document's "real debugging workflow" data is a local development
  database**, not production — five dead-lettered jobs and a ~50% Hunter
  provider error rate in local test data are not claims about the live
  system's health.
