"""`scripts.demo.stack` — bounded, clearly-erroring Docker Compose calls.

Mocks `subprocess.run` so these run with no Docker installed and no real
process spawned.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from scripts.demo import stack


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_ensure_running_only_targets_the_required_services() -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return _completed(0)

    with patch("subprocess.run", side_effect=fake_run):
        stack.ensure_running()

    assert captured["command"] == [
        "docker",
        "compose",
        "up",
        "-d",
        "db",
        "migrate",
        "api",
        "worker",
    ]


def test_ensure_running_never_starts_n8n() -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return _completed(0)

    with patch("subprocess.run", side_effect=fake_run):
        stack.ensure_running()

    assert "n8n" not in captured["command"]


def test_wipe_runs_down_dash_v() -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return _completed(0)

    with patch("subprocess.run", side_effect=fake_run):
        stack.wipe()

    assert captured["command"] == ["docker", "compose", "down", "-v"]


def test_missing_docker_binary_raises_a_clear_stack_error() -> None:
    with (
        patch("subprocess.run", side_effect=FileNotFoundError("docker not found")),
        pytest.raises(stack.StackError, match="Docker was not found on PATH"),
    ):
        stack.ensure_running()


def test_a_failing_compose_command_raises_with_the_command_to_run_manually() -> None:
    with (
        patch("subprocess.run", return_value=_completed(1, stderr="db unhealthy")),
        pytest.raises(stack.StackError) as exc_info,
    ):
        stack.ensure_running()

    message = str(exc_info.value)
    assert "db unhealthy" in message
    assert "docker compose up -d db migrate api worker" in message


def test_a_hanging_compose_command_times_out_with_a_clear_message() -> None:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 0))

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(stack.StackError, match="did not finish within"),
    ):
        stack.ensure_running()
