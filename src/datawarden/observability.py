"""OpenTelemetry tracing. Exports via OTLP only when an endpoint is configured; otherwise spans are
created (so instrumentation paths are exercised) but not exported."""

from __future__ import annotations

from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from datawarden.config import get_settings

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "datawarden"}))
    endpoint = get_settings().otel_exporter_otlp_endpoint
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
        except ImportError:
            pass
    trace.set_tracer_provider(provider)
    _configured = True


def tracer():
    _configure()
    return trace.get_tracer("datawarden")


@contextmanager
def span(name: str, **attrs):
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, str | int | float | bool) else str(v))
        yield sp


def setup_tracing(app) -> None:
    _configure()

    @app.middleware("http")
    async def _trace(request, call_next):
        with tracer().start_as_current_span(f"{request.method} {request.url.path}") as sp:
            response = await call_next(request)
            sp.set_attribute("http.status_code", response.status_code)
            return response
