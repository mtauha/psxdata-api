"""Telemetry line format, stdout exporters and provider setup (api/telemetry.py)."""
from __future__ import annotations

import io
import json
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from api import telemetry
from api.main import app


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


def test_request_produces_one_tagged_server_span(otel):
    with patch("psxdata.tickers", return_value=["HBL"]):
        resp = TestClient(app).get("/stocks")
    assert resp.status_code == 200
    (span,) = otel.server_spans()
    attrs = dict(span.attributes)
    assert span.name == "GET /stocks"
    assert attrs["http.route"] == "/stocks"
    assert attrs["http.request.method"] == "GET"
    assert attrs["http.response.status_code"] == 200
    assert attrs["client.address"] == "testclient"  # scope client, same source as uvicorn's log
    assert attrs["service.version"] == telemetry.SERVICE_VERSION


def test_health_is_not_traced(otel):
    assert TestClient(app).get("/health").status_code == 200
    assert otel.server_spans() == []


def test_unhandled_exception_logs_linked_error(otel):
    with patch("psxdata.tickers", side_effect=RuntimeError("kaboom")):
        resp = TestClient(app, raise_server_exceptions=False).get("/stocks")
    assert resp.status_code == 500
    (span,) = otel.server_spans()
    (log,) = otel.logs.get_finished_logs()
    assert log.log_record.severity_text == "ERROR"
    assert log.log_record.trace_id == span.context.trace_id
    assert log.log_record.attributes["exception.type"] == "RuntimeError"


def test_handled_4xx_produces_no_error_log(otel):
    # _parse_date raises HTTPException(422): a handled error, so no telemetry log record
    assert TestClient(app).get("/stocks/HBL/historical?start=nope").status_code == 422
    assert otel.logs.get_finished_logs() == ()
    (span,) = otel.server_spans()
    assert span.attributes["http.response.status_code"] == 422


def test_kill_switch_app_still_serves():
    off = telemetry.build_telemetry({"PSX_TELEMETRY": "off"})
    tiny = FastAPI(telemetry=off.config)
    tiny.add_middleware(telemetry.ServerSpanTags)

    @tiny.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    assert TestClient(tiny).get("/ping").json() == {"ok": True}


def test_unsampled_client_traceparent_is_still_recorded(otel):
    headers = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"}
    with patch("psxdata.tickers", return_value=["HBL"]):
        resp = TestClient(app).get("/stocks", headers=headers)
    assert resp.status_code == 200
    (span,) = otel.server_spans()
    assert span.context.trace_flags.sampled


def test_service_version_comes_from_the_package():
    import api

    assert telemetry.SERVICE_VERSION == api.__version__


def test_log_line_truncates_long_exception_message():
    message = "m" * (telemetry.MAX_STACKTRACE + 500)
    data = json.loads(telemetry.log_line(_recorded_log(attributes={"exception.message": message})))
    out = data["attributes"]["exception.message"]
    assert out.endswith("…[truncated]")
    assert len(out) == telemetry.MAX_STACKTRACE + len("…[truncated]")
