"""Wiring the official OpenTelemetry SDK to Zharp: exporters, resource, propagators, the
``logging`` bridge, the context processors and the instrumentations. No tracer of our own."""

import importlib
import importlib.util
import logging
import logging.config
import os
import socket
import threading
from typing import Callable, List, Optional, Tuple

from opentelemetry import propagate, trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from . import _errors
from ._config import Options, announce
from ._context import ContextLogProcessor, ContextSpanProcessor
from ._version import __version__

_log = logging.getLogger("zharp")


class State:
    def __init__(self, options: Options, tracer_provider, logger_provider, handler: Optional[logging.Handler]):
        self.options = options
        self.tracer_provider = tracer_provider
        self.logger_provider = logger_provider
        self.handler = handler
        # Logs only: no provider, and zharp.start_span() runs its work without a span.
        self.tracer = (
            tracer_provider.get_tracer("zharp", __version__) if tracer_provider is not None else trace.NoOpTracer()
        )
        self.logger = logger_provider.get_logger("zharp", __version__)
        self.instrumented: List[str] = []


def resource_for(options: Options) -> Resource:
    attributes = {
        "service.name": options.service,
        "telemetry.sdk.name": "zharp",
        "telemetry.sdk.language": "python",
        "telemetry.sdk.version": __version__,
        "process.pid": os.getpid(),
    }
    if options.release:
        attributes["service.version"] = options.release
    if options.environment:
        attributes["deployment.environment.name"] = options.environment
    try:
        attributes["host.name"] = socket.gethostname()
    except Exception:
        pass
    # Resource.create adds OTEL_RESOURCE_ATTRIBUTES; ours win.
    return Resource.create(attributes)


def default_exporters(options: Options):
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {"Authorization": "Bearer " + options.key}
    return (
        OTLPSpanExporter(endpoint=options.endpoint + "/v1/traces", headers=headers, compression=Compression.Gzip),
        OTLPLogExporter(endpoint=options.endpoint + "/v1/logs", headers=headers, compression=Compression.Gzip),
    )


def start(options: Options, exporters: Optional[Tuple] = None, batch: bool = True) -> State:
    span_exporter, log_exporter = exporters or default_exporters(options)
    resource = resource_for(options)

    tracer_provider = None
    if options.tracing:
        tracer_provider = _tracer_provider(resource, options)
        tracer_provider.add_span_processor(ContextSpanProcessor())
        tracer_provider.add_span_processor(_span_processor(span_exporter, batch))

    logger_provider = _logger_provider(resource)
    logger_provider.add_log_record_processor(ContextLogProcessor())
    logger_provider.add_log_record_processor(_log_processor(log_exporter, batch))

    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))
    if options.tracing:
        _errors.patch_record_exception()
    _quiet_repeated_export_errors()

    handler = None
    if options.logging_level is not None:
        handler = _bridge_logging(logger_provider, options.logging_level)

    state = State(options, tracer_provider, logger_provider, handler)
    if options.instrument:
        state.instrumented = instrument_installed() if options.tracing else instrument_logs_only()
    return state


def _tracer_provider(resource: Resource, options: Options):
    """The global SDK provider: ours, or one the app already set (we add our processors to it)."""
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        return existing
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(options.sample_rate)))
    trace.set_tracer_provider(provider)
    return provider


def _logger_provider(resource: Resource):
    existing = get_logger_provider()
    if isinstance(existing, LoggerProvider):
        return existing
    provider = LoggerProvider(resource=resource)
    set_logger_provider(provider)
    return provider


def _span_processor(exporter, batch: bool):
    if batch:
        return BatchSpanProcessor(exporter, schedule_delay_millis=5000, max_export_batch_size=512)
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return SimpleSpanProcessor(exporter)


def _log_processor(exporter, batch: bool):
    if batch:
        return BatchLogRecordProcessor(exporter, schedule_delay_millis=5000, max_export_batch_size=512)
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor

    return SimpleLogRecordProcessor(exporter)


# ─── logging ────────────────────────────────────────────────────────────────────


def _handler_class():
    try:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler
    except ImportError:  # older releases kept it in the SDK
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from opentelemetry.sdk._logs import LoggingHandler
    return LoggingHandler


def _bridge_logging(logger_provider, level: int) -> logging.Handler:
    """OTel's logging handler on the root logger, kept there when the app reconfigures logging."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        handler = _handler_class()(level=level, logger_provider=logger_provider)
    handler.addFilter(_errors.ExceptionFilter())
    handler._zharp = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_zharp", False):
            root.removeHandler(existing)
    root.addHandler(handler)
    # The root logger stops at WARNING by default; let the handler see what it asked for.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    def keep_handler(configure: Callable) -> Callable:
        if getattr(configure, "_zharp", False):
            return configure

        def wrapper(*args, **kwargs):
            try:
                return configure(*args, **kwargs)
            finally:
                current = logging.getLogger()
                if handler not in current.handlers:
                    current.addHandler(handler)
                if current.level == logging.NOTSET or current.level > level:
                    current.setLevel(level)

        wrapper._zharp = True  # type: ignore[attr-defined]
        return wrapper

    logging.config.dictConfig = keep_handler(logging.config.dictConfig)
    logging.config.fileConfig = keep_handler(logging.config.fileConfig)
    logging.basicConfig = keep_handler(logging.basicConfig)
    return handler


class _OncePerMessage(logging.Filter):
    """The exporter logs every failed batch; a wrong key would print one line every 5 seconds."""

    def __init__(self) -> None:
        super().__init__()
        self._seen = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, str(record.msg)[:200])
        with self._lock:
            if key in self._seen:
                return False
            if len(self._seen) > 100:
                self._seen.clear()
            self._seen.add(key)
        return True


def _quiet_repeated_export_errors() -> None:
    once = _OncePerMessage()
    for name in (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http._log_exporter",
        "opentelemetry.exporter.otlp.proto.http._common",
        "opentelemetry.sdk._shared_internal",
    ):
        logging.getLogger(name).addFilter(once)


# ─── instrumentations ───────────────────────────────────────────────────────────

#: (name, library that must be importable, instrumentor module, class)
INSTRUMENTATIONS = [
    ("django", "django", "opentelemetry.instrumentation.django", "DjangoInstrumentor"),
    ("fastapi", "fastapi", "opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
    ("flask", "flask", "opentelemetry.instrumentation.flask", "FlaskInstrumentor"),
    ("requests", "requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    ("httpx", "httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    ("psycopg", "psycopg", "opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
    ("psycopg2", "psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor"),
    ("sqlalchemy", "sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
]


#: ASGI's per-message "http send" / "http receive" spans say nothing the request span doesn't.
_INSTRUMENT_OPTIONS = {"fastapi": {"exclude_spans": ["receive", "send"]}}


def instrument_installed() -> List[str]:
    """Instrument every supported library that is installed. Returns their names."""
    done = []
    for name, library, module, cls in INSTRUMENTATIONS:
        try:
            if importlib.util.find_spec(library) is None:
                continue
            if name == "django" and not _django_settings_ready():
                _instrument_django_at_setup(module, cls)
                done.append(name)
                continue
            instrumentor = getattr(importlib.import_module(module), cls)()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(**_INSTRUMENT_OPTIONS.get(name, {}))
            done.append(name)
        except Exception as error:  # a missing instrumentation package or a version it rejects
            _log.debug("Zharp: not instrumenting %s: %s", name, error)
    return done


#: Logs only: the web frameworks, with a no-op tracer. They still read the incoming ``traceparent``
#: and ``baggage`` (the request's trace id, user and session go on its logs), but no span is
#: recorded, and HTTP clients and databases are left alone, so nothing is injected downstream.
_LOGS_ONLY = ("django", "fastapi", "flask")


def instrument_logs_only() -> List[str]:
    done = []
    noop = trace.NoOpTracerProvider()
    for name, library, module, cls in INSTRUMENTATIONS:
        if name not in _LOGS_ONLY:
            continue
        try:
            if importlib.util.find_spec(library) is None:
                continue
            kwargs = dict(_INSTRUMENT_OPTIONS.get(name, {}), tracer_provider=noop)
            if name == "django" and not _django_settings_ready():
                _instrument_django_at_setup(module, cls, kwargs)
                done.append(name)
                continue
            instrumentor = getattr(importlib.import_module(module), cls)()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(**kwargs)
            done.append(name)
        except Exception as error:
            _log.debug("Zharp: not instrumenting %s: %s", name, error)
    return done


def _django_settings_ready() -> bool:
    if os.environ.get("DJANGO_SETTINGS_MODULE"):
        return True
    try:
        from django.conf import settings

        return bool(settings.configured)
    except Exception:
        return False


def _instrument_django_at_setup(module: str, cls: str, kwargs: Optional[dict] = None) -> None:
    """``zharp.init()`` at the top of manage.py runs before DJANGO_SETTINGS_MODULE is set, and the
    Django instrumentor would configure empty settings. Instrument as django.setup() starts."""
    import django

    original = django.setup
    if getattr(original, "_zharp", False):
        return

    def setup(*args, **kwargs):
        django.setup = original
        try:
            instrumentor = getattr(importlib.import_module(module), cls)()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(**(kwargs or {}))
        except Exception as error:
            _log.debug("Zharp: not instrumenting django: %s", error)
        return original(*args, **kwargs)

    setup._zharp = True  # type: ignore[attr-defined]
    django.setup = setup


def describe(options: Options, instrumented: List[str]) -> str:
    env = 'environment "{}"'.format(options.environment) if options.environment else "no environment set"
    what = ", ".join(instrumented) if instrumented else "no supported libraries found"
    mode = "Application Monitoring active" if options.tracing else "Application Monitoring active, logs only"
    return '{} — service "{}", {} ({})'.format(mode, options.service, env, what)


def announce_started(state: State) -> None:
    announce(describe(state.options, state.instrumented))
