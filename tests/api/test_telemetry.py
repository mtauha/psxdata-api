"""Telemetry line format, stdout exporters and provider setup (api/telemetry.py)."""
from __future__ import annotations

import io
import json

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry._logs import SeverityNumber

from api import telemetry


def _recorded_spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("parent", kind=trace.SpanKind.SERVER) as parent:
        parent.set_attribute("http.route", "/stocks/{symbol}/historical")
        with tracer.start_as_current_span("child") as child:
            child.set_attribute("cache.status", "HIT")
    return {s.name: s for s in exporter.get_finished_spans()}


def _recorded_log(body="boom", attributes=None):
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    provider.get_logger("test").emit(
        body=body,
        severity_text="ERROR",
        severity_number=SeverityNumber.ERROR,
        attributes=attributes or {},
    )
    return exporter.get_finished_logs()[0]


def test_span_line_shape_and_key_order():
    spans = _recorded_spans()
    line = telemetry.span_line(spans["child"])
    assert "\n" not in line
    data = json.loads(line)
    assert list(data) == [
        "otel", "v", "trace_id", "span_id", "parent_span_id", "name", "kind",
        "start_ns", "end_ns", "status", "attributes",
    ]
    assert line.startswith('{"otel":"span","v":1,')
    assert len(data["trace_id"]) == 32 and len(data["span_id"]) == 16
    assert data["parent_span_id"] == format(spans["parent"].context.span_id, "016x")
    assert data["trace_id"] == format(spans["parent"].context.trace_id, "032x")
    assert data["kind"] == "INTERNAL" and data["status"] == "UNSET"
    assert data["end_ns"] >= data["start_ns"]
    assert data["attributes"] == {"cache.status": "HIT"}


def test_root_span_has_null_parent():
    data = json.loads(telemetry.span_line(_recorded_spans()["parent"]))
    assert data["parent_span_id"] is None
    assert data["kind"] == "SERVER"


def test_log_line_shape_and_key_order():
    line = telemetry.log_line(_recorded_log())
    data = json.loads(line)
    assert list(data) == [
        "otel", "v", "timestamp_ns", "severity_text", "severity_number", "body",
        "trace_id", "span_id", "attributes",
    ]
    assert line.startswith('{"otel":"log","v":1,')
    assert data["severity_text"] == "ERROR" and data["severity_number"] == 17
    assert data["body"] == "boom"
    assert data["trace_id"] is None and data["span_id"] is None  # emitted outside a span


def test_log_line_truncates_long_stacktrace():
    stack = "x" * (telemetry.MAX_STACKTRACE + 500)
    data = json.loads(telemetry.log_line(_recorded_log(attributes={"exception.stacktrace": stack})))
    out = data["attributes"]["exception.stacktrace"]
    assert out.endswith("…[truncated]")
    assert len(out) == telemetry.MAX_STACKTRACE + len("…[truncated]")


def test_exporters_write_one_line_per_item():
    stream = io.StringIO()
    spans = list(_recorded_spans().values())
    assert telemetry.JsonLineSpanExporter(stream).export(spans) is SpanExportResult.SUCCESS
    log = _recorded_log()
    assert telemetry.JsonLineLogExporter(stream).export([log]) is LogRecordExportResult.SUCCESS
    out = stream.getvalue().splitlines()
    assert len(out) == 3
    assert [json.loads(line)["otel"] for line in out] == ["span", "span", "log"]


def test_exporter_survives_closed_stream():
    stream = io.StringIO()
    stream.close()
    spans = list(_recorded_spans().values())
    assert telemetry.JsonLineSpanExporter(stream).export(spans) is SpanExportResult.FAILURE
    assert telemetry.JsonLineLogExporter(stream).export([_recorded_log()]) is (
        LogRecordExportResult.FAILURE
    )


def test_build_telemetry_config_when_enabled():
    t = telemetry.build_telemetry({})
    assert t.enabled is True
    cfg = t.config
    assert cfg["tracer_provider"] is t.tracer_provider
    assert cfg["logger_provider"] is t.logger_provider
    assert cfg["metrics"] is False
    assert cfg["operation_spans"] is False
    assert cfg["auto_configure"] is False
    assert cfg["exclude"]({"path": "/health"}) is True
    assert cfg["exclude"]({"path": "/stocks"}) is False
    resource = t.tracer_provider.resource.attributes
    assert resource["service.name"] == "psxdata-api"
    assert resource["service.version"] == telemetry.SERVICE_VERSION


def test_build_telemetry_kill_switch():
    t = telemetry.build_telemetry({"PSX_TELEMETRY": " OFF "})
    assert t.enabled is False
    assert t.config == {"tracing": False, "logs": False, "metrics": False}
    assert isinstance(t.tracer, trace.NoOpTracer)
