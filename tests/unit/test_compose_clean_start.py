"""The Compose clean-start ordering contract, pinned as data.

A fresh `docker compose up` used to start workers before the schema existed;
they crashed with 'relation "jobs" does not exist' until scripts/migrate.py
was run by hand. The fix is ordering — a one-shot `migrate` service that api
and worker gate on having *completed* — and ordering expressed in YAML is
exactly the kind of thing that silently regresses in an unrelated edit. These
tests parse the committed file and assert the contract; they never run Docker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    with _COMPOSE_PATH.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
    return loaded


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = compose["services"]
    return result


def test_migrate_service_uses_the_canonical_runner(services: dict[str, Any]) -> None:
    """One migration system, not two — the service must invoke scripts/migrate.py,
    the checksum-guarded runner that is the only path allowed to touch schema."""
    migrate = services["migrate"]
    assert "scripts/migrate.py" in migrate["command"]
    assert "DATABASE_DIRECT_URL" in migrate["environment"], (
        "scripts/migrate.py connects via DATABASE_DIRECT_URL and exits 1 without it"
    )


def test_migrate_waits_for_a_healthy_database(services: dict[str, Any]) -> None:
    assert services["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"


@pytest.mark.parametrize("service_name", ["api", "worker"])
def test_processing_services_wait_for_migrations_to_complete(
    services: dict[str, Any], service_name: str
) -> None:
    """`service_completed_successfully`, not `service_started`: a migrate that
    is merely running hasn't created the schema yet, which is the exact race
    this ordering exists to close."""
    depends_on = services[service_name]["depends_on"]
    assert depends_on["migrate"]["condition"] == "service_completed_successfully"


def test_migrate_is_one_shot(services: dict[str, Any]) -> None:
    """A restart policy on migrate would re-run it in a loop; api/worker's
    completed-successfully gate only means something for a service that runs
    once and exits."""
    assert "restart" not in services["migrate"]


def test_n8n_stays_behind_its_profile(services: dict[str, Any]) -> None:
    """Step 12's opt-in contract: plain `docker compose up` must not start n8n."""
    assert services["n8n"]["profiles"] == ["n8n"]


@pytest.mark.parametrize("service_name", ["db", "api", "worker"])
def test_long_running_services_restart_unless_stopped(
    services: dict[str, Any], service_name: str
) -> None:
    """A crashed api/worker/db should come back on its own; a one-shot
    `migrate` restarting would re-run it in a loop (see
    test_migrate_is_one_shot) — this is deliberately the complementary
    contract, not the same one."""
    assert services[service_name]["restart"] == "unless-stopped"
    assert "restart" not in services["migrate"]


def test_worker_disables_the_image_healthcheck(services: dict[str, Any]) -> None:
    """The image's HEALTHCHECK (Dockerfile) probes :8000/healthz — the api
    role's port. The worker command serves no HTTP port, so that check would
    fail forever unless explicitly turned off for this service."""
    assert services["worker"]["healthcheck"] == {"disable": True}


def test_api_has_no_service_level_healthcheck_override(services: dict[str, Any]) -> None:
    """No `healthcheck:` block here means it inherits the image's own
    (Dockerfile) — redeclaring it would be a second place for the same probe
    to drift out of sync with."""
    assert "healthcheck" not in services["api"]


def test_n8n_waits_for_the_api_to_be_actually_healthy(services: dict[str, Any]) -> None:
    """`service_started`, not `service_healthy`, would let n8n race a
    database-reachable-but-not-yet-migrated API — the same class of race
    `migrate`'s `service_completed_successfully` gate exists to close
    elsewhere in this file."""
    assert services["n8n"]["depends_on"]["api"]["condition"] == "service_healthy"
