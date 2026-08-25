"""Structural checks on `workflows/n8n/*.json` — never run against a live n8n.

Before this file, nothing read these JSON files at all: "hand-authored
against n8n's current node schemas... verified for real" (see
`docs/architecture.md`'s Step 12 section) meant a one-time manual import,
not anything CI would notice regress. Two things worth catching automatically
from here on:

1. **Referential integrity.** Every `connections` entry names a real node;
   node ids and names are unique. A typo'd node name in a hand-edited JSON
   file fails to import in n8n with no help from mypy or ruff — this is the
   only thing that would catch it before someone tries to import it.

2. **Status-vocabulary drift.** `outcome-sync.json` restates ARIE's own
   QUALIFIED/REJECTED/FAILURE status groups as JS array literals inside
   node expressions (n8n's own format — no way to import a Python constant
   into a node condition). `arie.statemachine.transitions` is the one place
   that vocabulary is defined; these tests read the literal lists back out
   of the committed JSON and assert they match it, so a status rename or a
   qualified-set fix on the Python side can't silently leave the n8n JSON
   holding the old vocabulary — exactly the "three inconsistent definitions
   of finalized" the M1 audit found (this module, the n8n gate, and CI, all
   disagreeing with each other and with themselves).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from arie.statemachine.transitions import FAILURE, FINALIZED, QUALIFIED

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "n8n"
_WORKFLOW_FILES = sorted(_WORKFLOWS_DIR.glob("*.json"))


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    return data


def _status_list_in(expression: str) -> set[str]:
    """The string literals inside a `[...].includes(...)` JS array literal.

    n8n node expressions are plain strings, not structured data — this reads
    the same shape `['AUTO_ROUTED', 'ROUTED', ...].includes($json...)` every
    status-list condition in these workflows uses.
    """
    match = re.search(r"\[([^\]]*)\]\.includes", expression)
    assert match is not None, f"no [...].includes(...) array literal found in: {expression!r}"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _node(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    for node in workflow["nodes"]:
        assert isinstance(node, dict)
        if node["name"] == name:
            return node
    raise AssertionError(f"no node named {name!r}")


@pytest.mark.parametrize("path", _WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_json_parses(path: Path) -> None:
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _WORKFLOW_FILES, ids=lambda p: p.name)
def test_node_ids_and_names_are_unique(path: Path) -> None:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    names = [n["name"] for n in workflow["nodes"]]
    ids = [n["id"] for n in workflow["nodes"]]
    assert len(names) == len(set(names)), f"duplicate node name in {path.name}"
    assert len(ids) == len(set(ids)), f"duplicate node id in {path.name}"


@pytest.mark.parametrize("path", _WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_connection_targets_a_real_node(path: Path) -> None:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    names = {n["name"] for n in workflow["nodes"]}
    for source, connection in workflow["connections"].items():
        assert source in names, f"{path.name}: connection source {source!r} isn't a real node"
        for group in connection["main"]:
            for edge in group:
                assert edge["node"] in names, (
                    f"{path.name}: connection target {edge['node']!r} isn't a real node"
                )


# --- outcome-sync.json: status vocabulary must match arie.statemachine.transitions --


def test_is_finalized_gate_matches_finalized() -> None:
    workflow = _load("outcome-sync.json")
    node = _node(workflow, "Is Finalized?")
    expression = node["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert _status_list_in(expression) == {str(s) for s in FINALIZED}


def test_is_permanently_failed_gate_matches_failure() -> None:
    workflow = _load("outcome-sync.json")
    node = _node(workflow, "Is Permanently Failed?")
    expression = node["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert _status_list_in(expression) == {str(s) for s in FAILURE}


def test_qualified_field_matches_qualified() -> None:
    workflow = _load("outcome-sync.json")
    node = _node(workflow, "Map to CRM Payload")
    assignments = node["parameters"]["assignments"]["assignments"]
    qualified_assignment = next(a for a in assignments if a["name"] == "qualified")
    assert _status_list_in(qualified_assignment["value"]) == {str(s) for s in QUALIFIED}


def test_finalized_and_failure_gates_are_disjoint() -> None:
    """FAILED/DEAD_LETTER must not also satisfy "Is Finalized?" -- a lead
    that failed permanently reaches "Is Permanently Failed?" only because
    "Is Finalized?" said no first (see the connections: Is Finalized?'s
    false branch feeds Is Permanently Failed?)."""
    assert FINALIZED.isdisjoint(FAILURE)


# --- outcome-sync.json: the audit-fixed false-success bug -------------------


def test_the_crm_sink_response_is_branched_on_status_before_responding() -> None:
    """The audit-fixed bug: `POST Mock CRM Sink` sets `neverError: true` (so
    a non-2xx doesn't abort the workflow) and used to connect straight to a
    node that hardcoded `synced: true` with no status check at all -- a
    failed CRM write reported success. It must now connect to a branching
    node first."""
    workflow = _load("outcome-sync.json")
    targets = [
        edge["node"]
        for group in workflow["connections"]["POST Mock CRM Sink"]["main"]
        for edge in group
    ]
    assert targets == ["Sink Succeeded?"]

    gate = _node(workflow, "Sink Succeeded?")
    assert gate["type"] == "n8n-nodes-base.if"


def test_a_failed_sink_write_does_not_respond_with_synced_true() -> None:
    workflow = _load("outcome-sync.json")
    fail_branch = workflow["connections"]["Sink Succeeded?"]["main"][1]
    assert [edge["node"] for edge in fail_branch] == ["Respond (Sink Failed)"]

    node = _node(workflow, "Respond (Sink Failed)")
    assert "synced: true" not in node["parameters"]["responseBody"]
    assert node["parameters"]["options"]["responseCode"] != 200


def test_a_permanent_failure_gets_an_explicit_terminal_response_not_an_infinite_wait() -> None:
    """The audit-fixed bug: FAILED/DEAD_LETTER leads used to fall into "Is
    Finalized?"'s false branch and respond `{synced: false, reason: 'lead
    not finalized'}` forever -- indistinguishable from a lead still
    genuinely in progress, with no signal telling a caller to stop polling."""
    workflow = _load("outcome-sync.json")
    node = _node(workflow, "Respond (Terminal Failure)")
    assert "terminal: true" in node["parameters"]["responseBody"]
    assert "synced: false" in node["parameters"]["responseBody"]

    not_finalized = _node(workflow, "Respond (Not Finalized Yet)")
    assert "terminal: false" in not_finalized["parameters"]["responseBody"]
