"""One smoke test proving `scripts.demo`'s `ArieClient` can drive a real, live
ARIE stack end to end — ingestion, polling, and a human review decision —
through a real HTTP server and a real database.

Runs the actual FastAPI app under `uvicorn` in a background thread, bound to
an ephemeral localhost port — real sockets, real HTTP, the same interpreter
`scripts/demo.ps1` runs against a Docker-hosted `api` service, just without
Docker. (`httpx.ASGITransport` was considered and rejected: it only
implements the *async* transport interface, and `ArieClient` is deliberately
sync — see its own module docstring for why.) A second background thread runs
`run_worker_cycle` in place of a real worker process — the same substitution
`test_pipeline_integration.py` makes, just driven through the demo's own
client instead of direct DB assertions.

Deliberately one test, not the full scenario suite: `test_demo_render.py`,
`test_demo_scenarios.py`, and `test_demo_client.py` already cover rendering,
orchestration, and HTTP-transport behavior in isolation with no database.
This is only "the wiring actually reaches a live stack."

**Cleanup is lead-only, not identity-only, on purpose.** This test uses the
same well-known corpus identities (`nadia.delacroix@lumen500.com`,
`nadia.haddad@cobalt500.com`) that `test_receipt_integration.py` also uses —
deliberately, since they're the documented, pre-verified demo fixtures. Both
files' `IngestCleanup.emails`/`.domains` teardown deletes the shared
`persons`/`companies` rows by identity, in the *same* transaction as everything
else that fixture cleans up; if one test's cleanup fails partway (e.g. a
timeout leaves a lead row it never registered), Postgres aborts the whole
transaction and *every* statement in it — including an already-executed
`DELETE FROM leads` for a completely different test's own row — rolls back
together, silently orphaning leads across the whole suite run. This test
sidesteps that shared surface entirely: it only ever deletes the lead rows it
itself created (queried back by this run's own external_ref pattern, not by
capturing return values that a mid-scenario timeout could leave unregistered),
and never touches the shared `persons`/`companies` rows — leaving them, like a
real demo run does, to be idempotently reused next time.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
import uvicorn
from psycopg_pool import ConnectionPool
from scripts.demo.client import ArieClient
from scripts.demo.corpus import select_demo_corpus
from scripts.demo.scenarios import run_scenario_a, run_scenario_b
from tests.integration.conftest import IngestCleanup, authorize_app

from arie.api.main import AppState, create_app
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import build_handlers
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle

pytestmark = pytest.mark.integration

_DECISION_TIMEOUT_S = 60.0
"""Generous relative to a real demo run's default (30s): this test shares a
process with a real uvicorn server and a worker-polling thread, competing for
the GIL under whatever load the rest of the suite is putting on the same
database — a tight timeout here produces exactly the kind of flake this
module's own docstring describes fixing the fallout of."""


def _drive_worker_until(
    stop: threading.Event,
    job_queue: PostgresJobQueue,
    pool: ConnectionPool,
    handlers: dict[str, JobHandler],
) -> None:
    while not stop.is_set():
        run_worker_cycle(
            job_queue,
            pool,
            handlers,
            worker_id="demo-smoke",
            batch_size=3,
            job_types=["compute_score"],
        )
        time.sleep(0.2)


@pytest.fixture
def live_server(app_state: AppState) -> Iterator[str]:
    """A real uvicorn server for the real app, on an ephemeral localhost port.

    Bound to port 0 so parallel test runs never collide; the actual port is
    read back off the running server's socket once it's listening.
    """
    app = create_app(state=app_state)
    authorize_app(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 10 seconds")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_demo_client_drives_autonomous_and_escalation_scenarios_against_a_live_stack(
    app_state: AppState,
    live_server: str,
    job_queue: PostgresJobQueue,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    leads: list[EvalLead],
) -> None:
    client = ArieClient(base_url=live_server, request_timeout_s=30.0)
    client.wait_for_health(timeout_s=15.0, poll_interval_s=0.1)

    corpus = select_demo_corpus(leads=leads)
    handlers = build_handlers(app_state.pool, runtime=corpus.runtime, provider_mode="simulated")
    run_id = f"smoke-{uuid.uuid4().hex[:8]}"

    stop = threading.Event()
    worker_thread = threading.Thread(
        target=_drive_worker_until, args=(stop, job_queue, app_state.pool, handlers), daemon=True
    )
    worker_thread.start()
    try:
        rendered_a, idempotency = run_scenario_a(
            client, corpus, run_id=run_id, decision_timeout_s=_DECISION_TIMEOUT_S
        )
        assert rendered_a.status == "decided"
        assert rendered_a.autonomous is True
        assert idempotency.is_idempotent is True

        before, after = run_scenario_b(
            client, corpus, run_id=run_id, decision_timeout_s=_DECISION_TIMEOUT_S
        )
        assert before.human_review is not None
        assert before.human_review.final_decision is None
        assert after.human_override is True
        assert after.final_status == "AUTO_ROUTED"
        assert after.recommended_action == before.recommended_action
    finally:
        stop.set()
        worker_thread.join(timeout=10)

        # Query back every lead this run created, by its own deterministic
        # external_ref pattern -- robust to a scenario raising partway
        # through, unlike capturing lead ids from return values that a
        # timeout would leave unassigned. Never touches persons/companies;
        # see the module docstring for why.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT lead_id FROM leads WHERE source = 'arie-demo' AND external_ref LIKE %s",
                (f"demo-{run_id}-%",),
            )
            our_lead_ids = [row[0] for row in cur.fetchall()]
        cleanup_ingest.lead_ids.extend(our_lead_ids)

        if our_lead_ids:
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT idempotency_key FROM provider_calls WHERE lead_id = ANY(%s)",
                    (our_lead_ids,),
                )
                cleanup_ingest.provider_call_keys.extend(row[0] for row in cur.fetchall())
                cur.execute(
                    "SELECT company_id, person_id FROM leads WHERE lead_id = ANY(%s)",
                    (our_lead_ids,),
                )
                for company_id, person_id in cur.fetchall():
                    cleanup_evidence.append(company_id)
                    cleanup_evidence.append(person_id)
