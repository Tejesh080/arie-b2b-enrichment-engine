"""OpenTelemetry wiring — one trace from HTTP request through to worker processing.

The thing worth understanding here is not the SDK setup (that part is boilerplate)
but **where the trace has to survive a process boundary**.

``POST /leads`` runs in the API process and returns in milliseconds. The work it
schedules runs in a *different* process, possibly on a different machine,
possibly minutes later after a retry backoff. Nothing in memory connects the
two — no call stack, no thread local, no shared context. The only artefact that
crosses that boundary is the ``jobs`` row itself, so that is where the trace
context is written down: ``jobs.trace_context`` (added in
``migrations/0005_ingestion_ledger_tracing.sql``) holds the W3C carrier map the
propagator emits, and the worker feeds it back in when it claims the job.

**Child span, not span link.** The worker's ``job.process`` span is created as a
child of the enqueue context, so the whole lifecycle — request, identity
resolution, enqueue, and every processing attempt — lands in one trace. The
messaging semantic conventions would also permit making it a new root with a
*link* back to the producer, which is the better choice when a single message
fans out to thousands of consumers and one giant trace becomes unreadable. At
this system's scale (one job per lead, a handful of attempts at most) a single
connected trace is far more useful to look at, and "why did this lead take four
minutes" is answerable in one view. If job volume per trace ever grows, that
trade flips and this is the line to change.

**Off by default, and genuinely off.** With no ``OTEL_EXPORTER_OTLP_ENDPOINT``
configured, ``configure_tracing`` installs nothing and every ``get_tracer`` call
returns OTel's no-op implementation — spans cost close to nothing rather than
being built and discarded. That keeps the M0 guarantee intact: the benchmark
imports none of this, needs no collector, and runs offline exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter

_PROPAGATOR = TraceContextTextMapPropagator()

# Set once, by configure_tracing. Tracked here rather than inferred from
# trace.get_tracer_provider() because OTel's global provider cannot be replaced
# once set — a second set_tracer_provider() logs a warning and is ignored, which
# would make a test that reconfigures tracing silently assert against the first
# provider's exporter and pass for the wrong reason.
_provider: TracerProvider | None = None


def get_tracer(name: str) -> Tracer:
    """A tracer for `name`.

    Safe to call at import time, before any provider is configured: OTel returns
    a ``ProxyTracer`` that resolves the global provider lazily at span-creation
    time, so module-level tracer handles pick up a provider installed later.
    """
    return trace.get_tracer(name)


def configure_tracing(
    *,
    service_name: str | None = None,
    endpoint: str | None = None,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install a tracer provider. Returns it, or ``None`` if tracing stays off.

    Called once per process — by the API's lifespan and by the worker's
    ``main()``. Calling it again returns the provider already installed.

    With no `exporter` and no `endpoint` (resolved from
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` when not passed), this installs nothing and
    returns ``None``: tracing is off, not "on and pointed nowhere". An explicit
    `exporter` wins over `endpoint` and is how tests capture spans in memory.

    **There is deliberately no way to replace an installed provider.** OTel's
    global provider is set-once by design: a second ``set_tracer_provider`` logs
    a warning and is ignored, so a "force" option would build a new provider,
    fail to install it, and hand back a provider that no tracer in the process
    actually writes to — passing tests that assert on an exporter nothing feeds.
    A process that needs a specific exporter must configure it before anything
    else does; ``tests/integration/conftest.py`` does this from a session-scoped
    fixture for exactly that reason.
    """
    global _provider

    # Imported lazily: these live in the `service` extra, and nothing in M0
    # should acquire a hard dependency on the SDK just by importing this module.
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    from arie.config import OBSERVABILITY

    if _provider is not None:
        return _provider

    resolved_endpoint = endpoint if endpoint is not None else OBSERVABILITY.otlp_endpoint
    if exporter is None and not resolved_endpoint:
        return None

    resolved_name = service_name or OBSERVABILITY.service_name
    provider = SdkTracerProvider(resource=Resource.create({SERVICE_NAME: resolved_name}))

    if exporter is not None:
        # Simple (synchronous) processor: a test asserting on spans must not
        # have to wait for a batch timer to flush.
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=resolved_endpoint))
        )

    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    """Flush pending spans and release the provider. Idempotent.

    For process shutdown — it exists so a worker stopping on Ctrl-C or an API
    completing its lifespan does not drop whatever the batch processor still
    holds. It is *not* a way to reset tracing: OTel's global provider stays set
    (see ``configure_tracing``), so a later ``configure_tracing`` in the same
    process would build a provider it cannot install.
    """
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def inject_trace_context() -> dict[str, str] | None:
    """The current span's W3C carrier, or ``None`` if there is no live trace.

    ``None`` rather than an empty dict is deliberate: it is what gets stored in
    ``jobs.trace_context``, and a NULL there reads unambiguously as "this job was
    enqueued outside a trace" instead of as an empty-but-present context.
    """
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier or None


def extract_trace_context(carrier: Mapping[str, str] | None) -> Context | None:
    """Rebuild the enqueue-time context from a stored carrier.

    Returns ``None`` for a missing, empty, or unparseable carrier — all three
    mean the same thing operationally ("no parent to attach to"), and a job is
    never worth failing over a bad trace header. A malformed ``traceparent``
    yields an invalid span context, which is detected here rather than being
    passed on to produce a span parented to nothing.
    """
    if not carrier:
        return None
    context = _PROPAGATOR.extract(dict(carrier))
    span_context = trace.get_current_span(context).get_span_context()
    return context if span_context.is_valid else None


@contextmanager
def traced(
    tracer: Tracer,
    name: str,
    *,
    context: Context | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Start a span, and mark it ERROR with the exception attached if the body raises.

    The default ``start_as_current_span(record_exception=True)`` records the
    event but leaves span *status* unset, so a failed job would show up as a
    successful span carrying an exception event — technically complete, and
    invisible on any "show me the errors" query. This sets both.
    """
    with tracer.start_as_current_span(name, context=context) as span:
        if attributes:
            set_attributes(span, attributes)
        try:
            yield span
        except Exception as exc:
            record_error(span, exc)
            raise


def record_error(span: Span, exc: BaseException | str) -> None:
    """Mark a span failed, for a caller that handles the error instead of raising.

    The worker is the case that needs this: a handler raising is a *normal*
    outcome there — the job is rescheduled with backoff and the exception never
    propagates — so ``traced`` can't be used, but the span must still come out
    ERROR or a failing job would be invisible on any error query.
    """
    if isinstance(exc, str):
        span.set_status(Status(StatusCode.ERROR, exc))
        return
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


def set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    """Set attributes, skipping ``None`` values and stringifying non-primitives.

    OTel rejects ``None`` and arbitrary objects as attribute values; UUIDs and
    enums are the two this codebase passes constantly, so they are converted
    here rather than at every call site.
    """
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(
            key, value if isinstance(value, bool | str | int | float) else str(value)
        )
