"""OpenTelemetry for psxdata-api, written to stdout as one JSON object per line.

FastAPI Cloud keeps the app's stdout as its logs, and psx-log-exporter copies those into
MotherDuck, so telemetry travels the same path as every other log line. The line format is
the contract with psx-log-exporter's views: change a key and bump LINE_VERSION.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, TextIO

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

SERVICE_NAME = "psxdata-api"
LINE_VERSION = 1
MAX_STACKTRACE = 8192
KILL_SWITCH_ENV = "PSX_TELEMETRY"
_TRUNCATED = "…[truncated]"

try:
    SERVICE_VERSION = version("psxdata-api")
except PackageNotFoundError:  # running from a source tree without an install (Docker image)
    SERVICE_VERSION = "unknown"


def _hex(value: int, width: int) -> str | None:
    return format(value, f"0{width}x") if value else None


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def span_line(span: ReadableSpan) -> str:
    context = span.context
    return _dumps({
        "otel": "span",
        "v": LINE_VERSION,
        "trace_id": _hex(context.trace_id, 32),
        "span_id": _hex(context.span_id, 16),
        "parent_span_id": _hex(span.parent.span_id, 16) if span.parent else None,
        "name": span.name,
        "kind": span.kind.name,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
    })


def _log_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(attributes or {})
    stack = out.get("exception.stacktrace")
    if isinstance(stack, str) and len(stack) > MAX_STACKTRACE:
        out["exception.stacktrace"] = stack[:MAX_STACKTRACE] + _TRUNCATED
    return out


def log_line(item: ReadableLogRecord) -> str:
    record = item.log_record
    severity = record.severity_number
    return _dumps({
        "otel": "log",
        "v": LINE_VERSION,
        "timestamp_ns": record.timestamp or record.observed_timestamp,
        "severity_text": record.severity_text,
        "severity_number": severity.value if severity is not None else None,
        "body": None if record.body is None else str(record.body),
        "trace_id": _hex(record.trace_id or 0, 32),
        "span_id": _hex(record.span_id or 0, 16),
        "attributes": _log_attributes(record.attributes),
    })


class _LineWriter:
    """Writes whole lines under a lock; ``stream=None`` means sys.stdout at write time."""

    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, lines: list[str]) -> bool:
        stream = self._stream or sys.stdout
        try:
            with self._lock:
                stream.write("".join(line + "\n" for line in lines))
                stream.flush()
        except (OSError, ValueError):  # closed or broken stream: drop, never break a request
            return False
        return True


class JsonLineSpanExporter(SpanExporter):
    def __init__(self, stream: TextIO | None = None) -> None:
        self._writer = _LineWriter(stream)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        ok = self._writer.write([span_line(s) for s in spans])
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE


class JsonLineLogExporter(LogRecordExporter):
    def __init__(self, stream: TextIO | None = None) -> None:
        self._writer = _LineWriter(stream)

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        ok = self._writer.write([log_line(item) for item in batch])
        return LogRecordExportResult.SUCCESS if ok else LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


@dataclass
class Telemetry:
    enabled: bool
    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    config: dict[str, Any]
    tracer: trace.Tracer

    def flush(self) -> None:
        self.tracer_provider.force_flush()
        self.logger_provider.force_flush()


def _is_health(scope: Mapping[str, Any]) -> bool:
    return scope.get("path") == "/health"


def build_telemetry(
    env: Mapping[str, str],
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
) -> Telemetry:
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": SERVICE_VERSION})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(span_exporter or JsonLineSpanExporter())
    )
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter or JsonLineLogExporter())
    )
    enabled = env.get(KILL_SWITCH_ENV, "").strip().lower() != "off"
    if not enabled:
        return Telemetry(
            False,
            tracer_provider,
            logger_provider,
            {"tracing": False, "logs": False, "metrics": False},
            trace.NoOpTracer(),
        )
    config: dict[str, Any] = {
        "tracer_provider": tracer_provider,
        "logger_provider": logger_provider,
        "metrics": False,  # request metrics are derived from spans in SQL
        "operation_spans": False,  # custom spans cover the parts worth timing
        "auto_configure": False,  # stray OTEL_* env vars must not add a second exporter
        "exclude": _is_health,
    }
    return Telemetry(
        True, tracer_provider, logger_provider, config, tracer_provider.get_tracer(SERVICE_NAME)
    )


TELEMETRY = build_telemetry(os.environ)


def get_tracer() -> trace.Tracer:
    return TELEMETRY.tracer
