"""Bounded Docker Compose orchestration for the demo.

Starts only the services the Decision Receipt demo needs — `db`, `migrate`,
`api`, `worker` — never `n8n` (it's opt-in, profile-gated, and this demo
doesn't touch it). Every subprocess call has a wall-clock timeout; nothing
here can hang the demo indefinitely, and every failure carries the exact
command a user can run themselves to see what happened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SERVICES: tuple[str, ...] = ("db", "migrate", "api", "worker")
_COMPOSE_TIMEOUT_S = 180.0


class StackError(RuntimeError):
    """Docker Compose could not do what the demo needed. The message always
    ends with the exact command to run manually — fail loudly, not silently."""


def _run_compose(*args: str) -> None:
    command = ["docker", "compose", *args]
    rendered = " ".join(command)
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_COMPOSE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise StackError(
            "Docker was not found on PATH. Install Docker Desktop, make sure it's "
            f"running, then run:\n  {rendered}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise StackError(
            f"'{rendered}' did not finish within {_COMPOSE_TIMEOUT_S:.0f}s. Run it "
            f"yourself to see what's happening:\n  {rendered}"
        ) from exc
    if result.returncode != 0:
        raise StackError(
            f"'{rendered}' failed:\n{result.stderr.strip()}\n\n"
            f"Run it yourself to investigate:\n  {rendered}"
        )


def wipe() -> None:
    """`docker compose down -v` — destructive. Callers must warn the user
    themselves before calling this; nothing here prompts for confirmation."""
    _run_compose("down", "-v")


def ensure_running() -> None:
    """`docker compose up -d` for exactly `REQUIRED_SERVICES` — a no-op for
    any that are already up."""
    _run_compose("up", "-d", *REQUIRED_SERVICES)
