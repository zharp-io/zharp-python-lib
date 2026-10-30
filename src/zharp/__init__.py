"""Zharp Application Monitoring for Python.

.. code-block:: python

    import zharp

    zharp.init(key="zpk_…", service="checkout-api", environment="production")

That's all a Django, FastAPI or Flask app needs: requests, outgoing HTTP calls (requests, httpx),
database queries (psycopg, psycopg2, SQLAlchemy) and ``logging`` records are sent with their
trace, and errors become Zharp issues. It is a thin layer over the official OpenTelemetry SDK —
anything you instrument with OpenTelemetry yourself is sent too.
"""

import contextlib
import logging
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Optional, TypeVar, Union

from opentelemetry import context as otel_context
from opentelemetry import trace as _trace
from opentelemetry._logs import LogRecord, SeverityNumber

from . import _config, _context, _errors, _setup
from ._version import __version__

__all__ = [
    "__version__",
    "capture_error",
    "clear_user",
    "flush",
    "init",
    "is_active",
    "log",
    "set_tag",
    "set_user",
    "shutdown",
    "start_span",
]

T = TypeVar("T")

_state: Optional[_setup.State] = None
_lock = threading.Lock()


def init(
    key: Optional[str] = None,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    endpoint: Optional[str] = None,
    *,
    sample_rate: Optional[float] = None,
    logging_level: Union[str, int, bool, None] = "env",
    capture_uncaught: bool = True,
    instrument: bool = True,
    tracing: Optional[bool] = None,
    quiet: bool = False,
    _exporters=None,
    _batch: bool = True,
) -> bool:
    """Start sending traces, logs and errors to Zharp. Call once, as early as possible — at the
    top of manage.py / wsgi.py / asgi.py, or in the module that creates the FastAPI or Flask app.

    Anything not passed is read from the environment: ``ZHARP_KEY``, ``ZHARP_SERVICE`` (then
    ``OTEL_SERVICE_NAME``), ``ZHARP_ENVIRONMENT``, ``ZHARP_RELEASE``, ``ZHARP_ENDPOINT``,
    ``ZHARP_SAMPLE_RATE``, ``ZHARP_LOGGING_LEVEL``, ``ZHARP_TRACING``. ``ZHARP_DISABLED=true`` turns
    it off.

    :param sample_rate: share of traces kept, 0.0–1.0 (default 1.0). Logs and handled errors are
        always sent.
    :param logging_level: the lowest ``logging`` level sent (default ``"INFO"``); ``False``
        leaves ``logging`` alone.
    :param capture_uncaught: report exceptions that end the program or a thread.
    :param instrument: instrument the supported libraries that are installed.
    :param tracing: ``False`` for logs only: logs, errors, user and tags are sent, no spans are
        recorded, HTTP clients and databases aren't instrumented and nothing is injected into
        outgoing requests. Logs in a request still carry its incoming trace id. Default ``True``
        (``ZHARP_TRACING=false`` in the environment).

    Returns True when it started. Never raises.
    """
    global _state
    try:
        with _lock:
            if _state is not None:
                return True
            if _config.disabled_by_env():
                return False
            options = _config.resolve(
                key, service, environment, release, endpoint, sample_rate, logging_level, capture_uncaught, instrument,
                tracing,
            )
            if options is None:
                if not quiet:
                    _config.announce("No key — set ZHARP_KEY or pass key= to zharp.init(). Nothing is sent.")
                return False
            _state = _setup.start(options, exporters=_exporters, batch=_batch)
            if options.capture_uncaught:
                _errors.install_uncaught_hooks(_report, flush)
        if not quiet:
            _setup.announce_started(_state)
        return True
    except Exception as error:  # never break the app that imports us
        _config.announce("Could not start: {}".format(error))
        return False


def is_active() -> bool:
    return _state is not None


# ─── Logs ──────────────────────────────────────────────────────────────────────


class _Log:
    """``zharp.log.info("message", {"order_id": 1})`` — debug, info, warn (warning), error, fatal.

    Each log carries the active trace and the request's user and session. ``logging`` records are
    sent too; this is for logs you only want in Zharp.
    """

    def debug(self, message: Any, attributes: Optional[Mapping[str, Any]] = None) -> None:
        _emit(SeverityNumber.DEBUG, "DEBUG", message, attributes)

    def info(self, message: Any, attributes: Optional[Mapping[str, Any]] = None) -> None:
        _emit(SeverityNumber.INFO, "INFO", message, attributes)

    def warn(self, message: Any, attributes: Optional[Mapping[str, Any]] = None) -> None:
        _emit(SeverityNumber.WARN, "WARN", message, attributes)

    warning = warn

    def error(self, message: Any, attributes: Optional[Mapping[str, Any]] = None) -> None:
        """An error log. Pass an exception (as the message) to report it as an error, handled."""
        if isinstance(message, BaseException):
            capture_error(message, attributes)
            return
        _emit(SeverityNumber.ERROR, "ERROR", message, attributes)

    def fatal(self, message: Any, attributes: Optional[Mapping[str, Any]] = None) -> None:
        _emit(SeverityNumber.FATAL, "FATAL", message, attributes)


log = _Log()


def _emit(severity: SeverityNumber, text: str, body: Any, attributes: Optional[Mapping[str, Any]]) -> None:
    state = _state
    if state is None:
        return
    try:
        state.logger.emit(
            LogRecord(
                timestamp=time.time_ns(),
                context=otel_context.get_current(),
                severity_text=text,
                severity_number=severity,
                body=body if isinstance(body, str) else str(body),
                attributes=_context.plain(attributes),
            )
        )
    except Exception:
        pass


# ─── Errors ────────────────────────────────────────────────────────────────────


def capture_error(error: BaseException, attributes: Optional[Mapping[str, Any]] = None) -> None:
    """Report an exception you caught. It becomes a Zharp issue (marked handled), with the trace,
    user and session of the current request.

    .. code-block:: python

        try:
            charge(order)
        except PaymentError as e:
            zharp.capture_error(e, {"order_id": order.id})
    """
    _report(error, handled=True, attributes=attributes)


def _report(
    error: BaseException,
    handled: bool,
    attributes: Optional[Mapping[str, Any]] = None,
    fatal: bool = False,
) -> None:
    state = _state
    if state is None or error is None:
        return
    try:
        _errors.mark_reported(error)
        attrs = _context.plain(attributes)
        attrs.update(_errors.exception_attributes(error, handled))
        state.logger.emit(
            LogRecord(
                timestamp=time.time_ns(),
                context=otel_context.get_current(),
                severity_text="FATAL" if fatal else "ERROR",
                severity_number=SeverityNumber.FATAL if fatal else SeverityNumber.ERROR,
                body="{}: {}".format(attrs["exception.type"], error) if str(error) else attrs["exception.type"],
                attributes=attrs,
            )
        )
    except Exception:
        pass


# ─── Context ───────────────────────────────────────────────────────────────────


def set_user(id: Optional[Union[str, int]] = None, email: Optional[str] = None, name: Optional[str] = None) -> None:
    """The user of the current request (``enduser.id``), on its spans, logs and errors from here
    on. Outside a request it applies to everything after. A browser or mobile app using a Zharp SDK
    already sends its user with each request, so this is only needed when the server knows better.
    """
    try:
        _context.set_user(None if id is None else str(id), email, name)
    except Exception:
        pass


def clear_user() -> None:
    try:
        _context.clear_user()
    except Exception:
        pass


def set_tag(key: str, value: Any) -> None:
    """An attribute on every span and log from now on (``None`` removes it)."""
    try:
        _context.set_tag(key, value)
    except Exception:
        pass


# ─── Spans ─────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _span(name: str, attributes: Optional[Mapping[str, Any]]) -> Iterator[Any]:
    tracer = _state.tracer if _state is not None else _trace.get_tracer("zharp", __version__)
    with tracer.start_as_current_span(name, attributes=_context.plain(attributes)) as span:
        yield span


def start_span(name: str, fn: Optional[Callable[[], T]] = None, attributes: Optional[Mapping[str, Any]] = None):
    """A span around some work: ``with zharp.start_span("render invoice"):`` or
    ``zharp.start_span("render invoice", lambda: render(invoice))``. An exception that escapes is
    recorded on the span and re-raised. With ``tracing=False`` the work runs without a span."""
    if fn is None:
        return _span(name, attributes)
    with _span(name, attributes):
        return fn()


# ─── Sending ───────────────────────────────────────────────────────────────────


def flush(timeout: float = 5.0) -> bool:
    """Send everything queued now and wait up to ``timeout`` seconds — at the end of a script, a
    job or a serverless function. True when it all went."""
    state = _state
    if state is None:
        return True
    millis = int(timeout * 1000)
    ok = True
    for provider in (state.tracer_provider, state.logger_provider):
        if provider is None:
            continue
        try:
            ok = bool(provider.force_flush(millis)) and ok
        except Exception:
            ok = False
    return ok


def shutdown() -> None:
    """Send what's left and stop. Runs by itself when the program exits."""
    global _state
    state = _state
    _state = None
    if state is None:
        return
    for provider in (state.tracer_provider, state.logger_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:
            pass
    if state.handler is not None:
        logging.getLogger().removeHandler(state.handler)
