"""Live-database tests for the human review workflow (M1 Step 11).

Mirrors `test_api_ingestion_integration.py`'s posture: exercise the actual HTTP
surface against the actual database, and treat the rollback / conflict tests
as the ones that matter most. `arie.approval.workflow.request_review` is not
called by anything in production yet (no `finalize_decision` handler exists —
see docs/06-m1-handoff.md), so every test here opens its own pending review
directly, the same way `arie.statemachine`'s own integration tests build a
bare lead row rather than going through the (not yet wired) worker.

Every test uses a freshly created lead, so it can never collide with another
test, another run, or real data in the database it points at.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup

from arie.approval.workflow import get_review, request_review
from arie.core.types import LeadStatus
from arie.statemachine.apply import OptimisticConcurrencyError

pytestmark = pytest.mark.integration


def _make_awaiting_human_lead(
    db_conn: psycopg.Connection,
    cleanup: IngestCleanup,
    *,
    original_decision: str | None = "auto_route",
    reviewer: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, int]:
    """A lead already escalated to AWAITING_HUMAN, with its pending review.

    Returns (lead_id, review_id, lead_version) — `lead_version` is read back
    after the escalation, i.e. the version a client would see from `GET
    /reviews/{review_id}` and would submit back as `expected_lead_version`.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, status) VALUES (%s, %s) RETURNING lead_id, version",
            ("review-test", str(LeadStatus.DECISION)),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    lead_id, version = row
    cleanup.lead_ids.append(lead_id)

    pending = request_review(
        db_conn,
        lead_id=lead_id,
        expected_version=version,
        original_decision=original_decision,
        reviewer=reviewer,
    )
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT version FROM leads WHERE lead_id = %s", (lead_id,))
        version_row = cur.fetchone()
    assert version_row is not None

    return lead_id, pending.review_id, version_row[0]


def _decision_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "approve",
        "reviewer": "reviewer@example.test",
        "expected_lead_version": 1,
    }
    payload.update(overrides)
    return payload


def _count(conn: psycopg.Connection, sql: str, params: tuple[Any, ...]) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ------------------------------------------------------------- GET /reviews --


def test_get_review_returns_pending_review_with_live_lead_context(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    response = api_client.get(f"/reviews/{review_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["review_id"] == str(review_id)
    assert body["lead_id"] == str(lead_id)
    assert body["is_pending"] is True
    assert body["original_decision"] == "auto_route"
    assert body["final_decision"] is None
    assert body["responded_at"] is None
    assert body["lead_status"] == LeadStatus.AWAITING_HUMAN
    assert body["lead_version"] == version


def test_get_unknown_review_is_404(api_client: TestClient) -> None:
    response = api_client.get(f"/reviews/{uuid.uuid4()}")
    assert response.status_code == 404


# --------------------------------------------------------------- decisions --


def test_approve_routes_the_lead_to_auto_routed(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    response = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(action="approve", expected_lead_version=version),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["final_decision"] == "auto_route"
    assert body["lead_status"] == LeadStatus.AUTO_ROUTED
    assert body["lead_version"] == version + 1
    assert body["already_applied"] is False

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT final_decision, responded_at IS NOT NULL FROM human_reviews "
            "WHERE review_id = %s",
            (review_id,),
        )
        review_row = cur.fetchone()
    assert lead_row is not None and lead_row == (LeadStatus.AUTO_ROUTED, version + 1)
    assert review_row is not None and review_row == ("auto_route", True)


def test_reject_routes_the_lead_to_synced(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    response = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(action="reject", expected_lead_version=version),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["final_decision"] == "reject"
    assert body["lead_status"] == LeadStatus.SYNCED

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
    assert lead_row is not None and lead_row[0] == LeadStatus.SYNCED


def test_edit_override_routes_the_lead_to_manual_review_and_keeps_notes(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    response = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(
            action="edit",
            expected_lead_version=version,
            notes="Score bounds missed a disqualifying signal in the free-text notes.",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["final_decision"] == "manual_review"
    assert body["lead_status"] == LeadStatus.MANUAL_REVIEW
    assert "disqualifying signal" in body["notes"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
    assert lead_row is not None and lead_row[0] == LeadStatus.MANUAL_REVIEW


def test_edit_without_notes_is_rejected_before_anything_is_written(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    response = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(action="edit", expected_lead_version=version, notes=None),
    )

    assert response.status_code == 422
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute("SELECT responded_at FROM human_reviews WHERE review_id = %s", (review_id,))
        review_row = cur.fetchone()
    assert lead_row is not None and lead_row[0] == LeadStatus.AWAITING_HUMAN
    assert review_row is not None and review_row[0] is None


def test_review_not_found_is_404(api_client: TestClient) -> None:
    response = api_client.post(
        f"/reviews/{uuid.uuid4()}/decision", json=_decision_payload(expected_lead_version=1)
    )
    assert response.status_code == 404


# --------------------------------------------------------------- idempotency --


def test_duplicate_submission_is_idempotent(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """A client retry (timeout, redelivery) resending the identical decision
    must be a no-op that reports what already happened, not a second effect."""
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)
    payload = _decision_payload(action="approve", expected_lead_version=version)

    first = api_client.post(f"/reviews/{review_id}/decision", json=payload)
    second = api_client.post(f"/reviews/{review_id}/decision", json=payload)

    assert first.status_code == 200
    assert first.json()["already_applied"] is False
    assert second.status_code == 200
    assert second.json()["already_applied"] is True
    assert second.json()["lead_status"] == first.json()["lead_status"]
    assert second.json()["lead_version"] == first.json()["lead_version"]
    assert second.json()["responded_at"] == first.json()["responded_at"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
    assert lead_row is not None
    assert lead_row == (LeadStatus.AUTO_ROUTED, version + 1), (
        "the retry must not apply the transition a second time"
    )
    assert (
        _count(
            db_conn,
            "SELECT count(*) FROM lead_events WHERE lead_id = %s AND event_type = %s",
            (lead_id, "human_review:decided"),
        )
        == 1
    )


def test_conflicting_resubmission_after_completion_fails_safely(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Two different verdicts for one review — a second reviewer, or the first
    changing their mind after their call already landed. The loser must not
    silently overwrite the recorded decision or move the lead a second time."""
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    first = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(
            action="approve", reviewer="first@example.test", expected_lead_version=version
        ),
    )
    second = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(
            action="reject", reviewer="second@example.test", expected_lead_version=version
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 409

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute(
            "SELECT final_decision, reviewer FROM human_reviews WHERE review_id = %s",
            (review_id,),
        )
        review_row = cur.fetchone()
    assert lead_row is not None
    assert lead_row == (LeadStatus.AUTO_ROUTED, version + 1), (
        "only the winner's transition may apply"
    )
    assert review_row is not None
    assert review_row == ("auto_route", "first@example.test")


def test_stale_lead_version_is_rejected_and_leaves_the_review_pending(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Optimistic-concurrency failure and transaction rollback, together: a
    reviewer submits against a lead version that has since moved for reasons
    unrelated to this review. `apply_transition` must reject it, and rolling
    back must undo the review's own compare-and-swap too -- otherwise the
    review would be silently stuck (marked answered internally, lead never
    moved) with no way to retry.
    """
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    # Simulate a concurrent writer touching the lead for an unrelated reason
    # (never through arie.statemachine.apply, on purpose -- the point is that
    # *something* moved the version out from under this review).
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE leads SET version = version + 1, updated_at = now() WHERE lead_id = %s",
            (lead_id,),
        )
    db_conn.commit()

    response = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(action="approve", expected_lead_version=version),  # now stale
    )

    assert response.status_code == 409

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
        cur.execute("SELECT responded_at FROM human_reviews WHERE review_id = %s", (review_id,))
        review_row = cur.fetchone()
    assert lead_row is not None
    assert lead_row == (LeadStatus.AWAITING_HUMAN, version + 1), (
        "the lead must be untouched by the failed submission -- only the "
        "earlier, unrelated bump is reflected"
    )
    assert review_row is not None and review_row[0] is None, (
        "the review's own compare-and-swap must have rolled back too, or a "
        "retry with the correct version could never complete it"
    )


def test_retry_after_a_stale_version_conflict_succeeds(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The other half of the rollback guarantee: after re-reading the lead's
    current version, the same review can still be completed normally."""
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE leads SET version = version + 1, updated_at = now() WHERE lead_id = %s",
            (lead_id,),
        )
    db_conn.commit()

    failed = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(action="approve", expected_lead_version=version),
    )
    assert failed.status_code == 409

    current = api_client.get(f"/reviews/{review_id}")
    retry = api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(
            action="approve", expected_lead_version=current.json()["lead_version"]
        ),
    )

    assert retry.status_code == 200
    assert retry.json()["already_applied"] is False
    assert retry.json()["lead_status"] == LeadStatus.AUTO_ROUTED

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM leads WHERE lead_id = %s", (lead_id,))
        lead_row = cur.fetchone()
    assert lead_row is not None and lead_row[0] == LeadStatus.AUTO_ROUTED


# -------------------------------------------------------------------- audit --


def test_escalation_and_decision_are_both_recorded_as_lead_events(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The append-only audit trail: opening a review and deciding it are two
    distinct, individually recorded events, not one row overwritten in place."""
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    api_client.post(
        f"/reviews/{review_id}/decision",
        json=_decision_payload(
            action="edit",
            reviewer="auditor@example.test",
            expected_lead_version=version,
            notes="Overriding after a manual check of the source evidence.",
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM lead_events WHERE lead_id = %s ORDER BY event_id",
            (lead_id,),
        )
        events = cur.fetchall()

    assert [e[0] for e in events] == ["lead:escalated", "human_review:decided"]
    assert events[0][1] == {"original_decision": "auto_route"}
    decided_payload = events[1][1]
    assert decided_payload["review_id"] == str(review_id)
    assert decided_payload["action"] == "edit"
    assert decided_payload["reviewer"] == "auditor@example.test"


# --------------------------------------------------- request_review itself --


def test_a_second_escalation_with_a_stale_version_fails_safely(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Defense in depth for the (currently uncalled) escalation entry point.

    A caller retrying `request_review` with the *same* (now stale)
    `expected_version` it started with -- the ordinary shape of a retry --
    must fail via the same optimistic-concurrency guard every other
    transition in this codebase uses, and must not leave a second pending
    review behind (0006's partial unique index is the backstop if it ever
    did). This does not claim full idempotency for `request_review` -- a
    retry that first re-reads the lead's now-current version would legally
    re-open a review, since nothing calls this function yet to make that
    distinction load-bearing. See the module docstring.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, status) VALUES (%s, %s) RETURNING lead_id, version",
            ("review-test", str(LeadStatus.DECISION)),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    lead_id, version = row
    cleanup_ingest.lead_ids.append(lead_id)

    first = request_review(
        db_conn, lead_id=lead_id, expected_version=version, original_decision="auto_route"
    )
    db_conn.commit()

    with pytest.raises(OptimisticConcurrencyError):
        request_review(
            db_conn,
            lead_id=lead_id,
            expected_version=version,  # stale: the call above already advanced it once
            original_decision="auto_route",
        )
    db_conn.rollback()

    assert _count(db_conn, "SELECT count(*) FROM human_reviews WHERE lead_id = %s", (lead_id,)) == 1
    assert first.created is True


def test_get_review_reflects_current_lead_state_not_a_snapshot(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """`get_review` joins the lead live -- a stale version here would defeat
    the whole point of returning it for the client to submit back."""
    lead_id, review_id, version = _make_awaiting_human_lead(db_conn, cleanup_ingest)

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE leads SET version = version + 1, updated_at = now() WHERE lead_id = %s",
            (lead_id,),
        )
    db_conn.commit()

    record = get_review(db_conn, review_id)

    assert record is not None
    assert record.lead_version == version + 1
    assert record.is_pending is True


def test_get_review_returns_none_for_unknown_id(db_conn: psycopg.Connection) -> None:
    """`get_review` itself returns `None` for a miss -- `ReviewNotFoundError`
    (see the 404 tests above) is `submit_decision`'s vocabulary, raised only
    when a decision is submitted against an id that doesn't resolve."""
    assert get_review(db_conn, uuid.uuid4()) is None
