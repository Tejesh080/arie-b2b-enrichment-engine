"""Shared fixtures.

The canonical dataset is generated once per session. It is deterministic, so
every test sees byte-identical data — which is also what makes the validity gate
meaningful: it runs against exactly the dataset the benchmark will use, not a
smaller stand-in.

The tracing fixtures live here rather than in ``tests/integration/`` for a
reason that only shows up when both packages run in one session (``make
test-all``): OTel's global tracer provider is **set-once**, so whichever test
package installed one first would win and the other's exporter would silently
never receive a span. One session-scoped provider, shared by both, removes the
ordering dependency entirely.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from arie.evalgen.generator import generate_dataset
from arie.evalgen.schema import DatasetManifest, EvalLead

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    """Install an in-memory tracer provider once, for the whole session.

    Imports are deferred into the body so that collecting the M0 test modules
    — which have nothing to do with tracing — doesn't pull the OpenTelemetry
    SDK in. It lives in the `service` extra, and M0's "runs with nothing
    installed but the core deps" property is worth not eroding by accident.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from arie.observability.tracing import configure_tracing

    exporter = InMemorySpanExporter()
    configure_tracing(service_name="arie-tests", exporter=exporter)
    return exporter


@pytest.fixture
def spans(span_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """The session exporter, emptied before and after each test that uses it."""
    span_exporter.clear()
    yield span_exporter
    span_exporter.clear()
