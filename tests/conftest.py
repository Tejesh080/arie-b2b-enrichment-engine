"""Shared fixtures.

The canonical dataset is generated once per session. It is deterministic, so
every test sees byte-identical data — which is also what makes the validity gate
meaningful: it runs against exactly the dataset the benchmark will use, not a
smaller stand-in.
"""

from __future__ import annotations

import pytest

from arie.evalgen.generator import generate_dataset
from arie.evalgen.schema import DatasetManifest, EvalLead

CANONICAL_SEED = 42


@pytest.fixture(scope="session")
def dataset() -> tuple[list[EvalLead], DatasetManifest]:
    return generate_dataset(seed=CANONICAL_SEED)


@pytest.fixture(scope="session")
def leads(dataset: tuple[list[EvalLead], DatasetManifest]) -> list[EvalLead]:
    return dataset[0]


@pytest.fixture(scope="session")
def manifest(dataset: tuple[list[EvalLead], DatasetManifest]) -> DatasetManifest:
    return dataset[1]


@pytest.fixture(scope="session")
def test_split(leads: list[EvalLead]) -> list[EvalLead]:
    return [lead for lead in leads if lead.split == "test"]


@pytest.fixture(scope="session")
def calibration_split(leads: list[EvalLead]) -> list[EvalLead]:
    return [lead for lead in leads if lead.split == "calibration"]
