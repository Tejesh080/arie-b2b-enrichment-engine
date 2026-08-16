"""Trace-context plumbing, without a database.

The propagation helpers are the load-bearing part of Step 9's observability:
they are what carries a trace across the API-to-worker process boundary through
a `jobs` row. These tests pin their contract in isolation —
``tests/integration/test_trace_propagation_integration.py`` then checks the
whole chain end to end.
"""

from __future__ import annotations

import uuid

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from arie.core.types import LeadStatus
from arie.observability.tracing import (
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    record_error,
    set_attributes,
    traced,
)

_TRACER = get_tracer("arie.tests.tracing")


def test_inject_returns_none_outside_a_trace(spans: InMemorySpanExporter) -> None:
    """NULL in `jobs.trace_context` must mean "enqueued outside a trace".

    An empty-but-present carrier would be ambiguous with a broken one.
    """
    assert inject_trace_context() is None


def test_inject_captures_the_current_span(spans: InMemorySpanExporter) -> None:
    with _TRACER.start_as_current_span("outer"):
        carrier = inject_trace_context()

    assert carrier is not None
    assert "traceparent" in carrier


def test_context_round_trips_through_a_carrier(spans: InMemorySpanExporter) -> None:
    """The property the whole design rests on: a span started from an extracted
    carrier lands in the same trace as the span that injected it.

    Asserted against the *exported* spans rather than the live handles — the
    exporter yields ``ReadableSpan``, which is where ``.parent`` lives; the API's
    ``Span`` protocol deliberately doesn't expose it.
    """
    with _TRACER.start_as_current_span("producer"):
        carrier = inject_trace_context()

    parent = extract_trace_context(carrier)
    with _TRACER.start_as_current_span("consumer", context=parent):
        pass

    finished = {span.name: span for span in spans.get_finished_spans()}
    producer, consumer = finished["producer"], finished["consumer"]
    assert producer.context is not None
    assert consumer.context is not None
    assert consumer.context.trace_id == producer.context.trace_id
    assert consumer.parent is not None
    assert consumer.parent.span_id == producer.context.span_id


def test_extract_treats_missing_and_broken_carriers_alike(spans: InMemorySpanExporter) -> None:
    """A bad trace header is never a reason to drop work.

    All three of these mean "no parent to attach to", and the worker responds
    to each by starting its own trace rather than failing the job.
    """
    assert extract_trace_context(None) is None
    assert extract_trace_context({}) is None
    assert extract_trace_context({"traceparent": "not-a-traceparent"}) is None
    # Well-formed shape, all-zero (invalid) trace id — the spec's own "this is
    # not a real context" encoding, which parses but must not be attached to.
    assert (
        extract_trace_context(
            {"traceparent": "00-00000000000000000000000000000000-0000000000000000-01"}
        )
        is None
    )


def test_traced_marks_the_span_error_and_reraises(spans: InMemorySpanExporter) -> None:
    """`start_as_current_span` records the exception but leaves status unset.

    A span carrying an exception event but reporting OK is invisible to every
    "show me the errors" query, which is the only reason `traced` exists.
    """
    with pytest.raises(RuntimeError, match="boom"), traced(_TRACER, "failing"):
        raise RuntimeError("boom")

    (span,) = spans.get_finished_spans()
    assert span.name == "failing"
    assert span.status.status_code is StatusCode.ERROR
    assert span.events, "the exception should still be recorded as an event"


def test_traced_leaves_a_successful_span_unset(spans: InMemorySpanExporter) -> None:
    with traced(_TRACER, "fine"):
        pass

    (span,) = spans.get_finished_spans()
    assert span.status.status_code is not StatusCode.ERROR


def test_record_error_marks_a_span_the_caller_handled(spans: InMemorySpanExporter) -> None:
    """The worker's case: a raising handler is a normal outcome there, so the
    exception never propagates — but the span still has to come out ERROR."""
    with _TRACER.start_as_current_span("handled") as span:
        record_error(span, ValueError("nope"))

    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.events


def test_record_error_accepts_a_bare_message(spans: InMemorySpanExporter) -> None:
    """ "no handler registered" is a failure with no exception object behind it."""
    with _TRACER.start_as_current_span("handled") as span:
        record_error(span, "no handler registered")

    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.status.description == "no handler registered"


def test_set_attributes_skips_none_and_stringifies_rich_types(
    spans: InMemorySpanExporter,
) -> None:
    """OTel rejects None and arbitrary objects; UUIDs and enums are what this
    codebase passes constantly, so they are converted centrally."""
    lead_id = uuid.uuid4()
    with _TRACER.start_as_current_span("attrs") as span:
        set_attributes(
            span,
            {
                "arie.lead_id": lead_id,
                "arie.status": LeadStatus.NEW,
                "arie.attempt": 2,
                "arie.cache_hit": False,
                "arie.absent": None,
            },
        )

    (finished,) = spans.get_finished_spans()
    attributes = dict(finished.attributes or {})
    assert attributes["arie.lead_id"] == str(lead_id)
    assert attributes["arie.status"] == "NEW"
    assert attributes["arie.attempt"] == 2
    assert attributes["arie.cache_hit"] is False
    assert "arie.absent" not in attributes


def test_booleans_are_not_stringified(spans: InMemorySpanExporter) -> None:
    """bool is a subclass of int; a careless isinstance check turns False into
    the string "False", which is truthy in every dashboard filter."""
    with _TRACER.start_as_current_span("bools") as span:
        set_attributes(span, {"a": True, "b": False})

    (finished,) = spans.get_finished_spans()
    attributes = dict(finished.attributes or {})
    assert attributes["a"] is True
    assert attributes["b"] is False
