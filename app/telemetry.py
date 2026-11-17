"""OpenTelemetry tracing (exported via OTLP/HTTP to Jaeger) with W3C context propagated through Redis messages
and HTTP headers, so one trace spans api -> researcher -> gateway -> adjudicator (D5)."""
from __future__ import annotations
import os
from contextlib import contextmanager
from opentelemetry import trace, propagate
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_tracer = None


def setup_tracing(service: str):
    global _tracer
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if ep:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service)
    return _tracer


def tracer():
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("app")
    return _tracer


def inject() -> dict:
    carrier: dict = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def span_from(carrier: dict | None, name: str, **attrs):
    ctx = propagate.extract(carrier or {})
    with tracer().start_as_current_span(name, context=ctx) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp


@contextmanager
def span(name: str, **attrs):
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp


def current_trace_id() -> str:
    c = trace.get_current_span().get_span_context()
    return format(c.trace_id, "032x") if c and c.trace_id else ""
