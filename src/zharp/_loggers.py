"""The exact log captures, which know each line's level and fields: ``logging`` (every logger,
including those that don't propagate to the root), loguru and structlog.

Each one hands its records to Zharp with their own level, in the context they were logged in, and
prints exactly as before. While one is printing, the output capture stands aside, and when they
nest (``logging`` routed into loguru, loguru propagating into ``logging``) the outer one sends the
line and the inner one leaves it.
"""

import importlib.util
import logging
from typing import Any, Callable, Dict, List, Optional

from . import _context, _errors, _output

_log = logging.getLogger("zharp")

Undo = Callable[[], None]


# ─── logging ────────────────────────────────────────────────────────────────────


def _reaches_root(logger: logging.Logger) -> bool:
    """Whether a record logged here propagates up to the root logger (where the bridge is)."""
    current: Optional[logging.Logger] = logger
    while current is not None:
        if current.parent is None:
            return True
        if not current.propagate:
            return False
        current = current.parent
    return False


class ExceptionsOnly(logging.Filter):
    """Logs off: of what ``logging`` records, only exceptions (``logger.exception``, a framework
    logging the error that ended a request) are sent — they are errors, not logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        return bool(exc_info and exc_info[0] is not None)


def stamp_logging() -> Undo:
    """Every ``logging`` handler's output (stream, file, syslog …) ends with the request's ids:
    ``Handler.format`` is the last step before a handler writes, whatever its formatter."""
    original = logging.Handler.format
    if getattr(original, "_zharp", False):
        return lambda: None

    def format(self, record):
        text = original(self, record)
        if getattr(self, "_zharp", False):
            return text  # the bridge: its records carry the ids as fields already
        try:
            return _output.stamp_text(text)
        except Exception:
            return text

    format._zharp = True  # type: ignore[attr-defined]
    logging.Handler.format = format  # type: ignore[method-assign]

    def undo() -> None:
        if logging.Handler.format is format:
            logging.Handler.format = original  # type: ignore[method-assign]

    return undo


def rebind_streams(replaced: Dict[Any, Any]) -> Undo:
    """loguru's default sink and structlog's PrintLogger hold on to the ``sys.stdout`` /
    ``sys.stderr`` they found at import. Pointed at the stand-ins, their lines are stamped (and
    seen) like everything else printed."""
    undo: List[Undo] = []
    if not replaced:
        return lambda: None
    try:
        import sys

        if "loguru" in sys.modules:
            from loguru import logger

            for handler in list(getattr(getattr(logger, "_core", None), "handlers", {}).values()):
                sink = getattr(handler, "_sink", None)
                stream = getattr(sink, "_stream", None)
                if sink is not None and stream in replaced:
                    sink._stream = replaced[stream]
                    undo.append(lambda sink=sink, stream=stream: setattr(sink, "_stream", stream))
        if "structlog" in sys.modules:
            from structlog import _output as structlog_output

            for name in ("stdout", "stderr"):
                stream = getattr(structlog_output, name, None)
                if stream in replaced:
                    setattr(structlog_output, name, replaced[stream])
                    undo.append(lambda name=name, stream=stream: setattr(structlog_output, name, stream))
    except Exception as error:
        _log.debug("Zharp: rebinding streams: %s", error)

    def run() -> None:
        for fn in undo:
            try:
                fn()
            except Exception:
                pass

    return run


class OwnerFilter(logging.Filter):
    """On the bridge: a record logged from inside loguru or structlog is theirs to send."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _output.owner() in (None, "logging")


def capture_logging(get_handler: Callable[[], Optional[logging.Handler]]) -> Undo:
    """Every logger's records go through ``Logger.callHandlers``. Wrapped, so that:

    - what its handlers print (a StreamHandler, ``logging.lastResort``) isn't read again by the
      output capture;
    - a record from a logger that stops propagating before the root (uvicorn's and gunicorn's,
      libraries that set ``propagate = False``) still reaches the bridge on the root.
    """
    original = logging.Logger.callHandlers
    if getattr(original, "_zharp", False):
        return lambda: None

    def callHandlers(self, record):
        with _output.exact("logging"):
            original(self, record)
            try:
                handler = get_handler()
                if (
                    handler is not None
                    and record.levelno >= handler.level
                    and not _reaches_root(self)
                    and _output.owner() == "logging"
                ):
                    handler.handle(record)
            except Exception:
                pass

    callHandlers._zharp = True  # type: ignore[attr-defined]
    logging.Logger.callHandlers = callHandlers  # type: ignore[method-assign]

    def undo() -> None:
        if logging.Logger.callHandlers is callHandlers:
            logging.Logger.callHandlers = original  # type: ignore[method-assign]

    return undo


# ─── loguru ─────────────────────────────────────────────────────────────────────


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def capture_loguru(otel_logger, threshold: int, send_logs: bool = True) -> Optional[Undo]:
    """A sink on loguru's logger for records at ``threshold`` and above (logs off: only those
    carrying an exception), put back when the app calls ``logger.remove()`` (or
    ``logger.configure(handlers=…)``) to set up its own."""
    if not _installed("loguru"):
        return None
    try:
        from loguru import logger
        from loguru._logger import Logger
    except Exception:
        return None
    if getattr(Logger.remove, "_zharp", False):
        return None

    def sink(message) -> None:
        if _output.owner() not in (None, "loguru"):
            return  # logged from inside logging (an InterceptHandler): the bridge sends it
        try:
            record = message.record
            attributes: Dict[str, Any] = {"log.source": "loguru"}
            if record.get("name"):
                attributes["log.logger"] = record["name"]
            if record.get("function"):
                attributes["code.function"] = record["function"]
            if record.get("line"):
                attributes["code.lineno"] = record["line"]
            attributes.update(_context.plain(record.get("extra")))
            exception = record.get("exception")
            error = getattr(exception, "value", None) if exception else None
            if not send_logs and not isinstance(error, BaseException):
                return
            if isinstance(error, BaseException) and not _errors.is_reported(error):
                _errors.mark_reported(error)
                attributes.update(_errors.exception_attributes(error, handled=True))
            _output.emit(otel_logger, _output.level_of_number(record["level"].no), str(record["message"]), attributes)
        except Exception:
            pass

    state = {"id": None}

    def add() -> None:
        try:
            state["id"] = logger.add(
                sink, level=threshold, format="{message}", colorize=False, catch=True, backtrace=False, diagnose=False
            )
        except Exception as error:
            _log.debug("Zharp: not capturing loguru: %s", error)

    add()

    original_remove = Logger.remove

    def remove(self, handler_id=None):
        result = original_remove(self, handler_id)
        if handler_id is None or handler_id == state["id"]:
            add()
        return result

    remove._zharp = True  # type: ignore[attr-defined]
    Logger.remove = remove  # type: ignore[method-assign]

    # While loguru logs, what its sinks print isn't read again by the output capture.
    original_log = getattr(Logger, "_log", None)
    quiet_log = None
    if callable(original_log):

        def quiet_log(self, *args, **kwargs):
            with _output.exact("loguru"):
                return original_log(self, *args, **kwargs)

        Logger._log = quiet_log  # type: ignore[attr-defined]

    def undo() -> None:
        if Logger.remove is remove:
            Logger.remove = original_remove  # type: ignore[method-assign]
        if quiet_log is not None and getattr(Logger, "_log", None) is quiet_log:
            Logger._log = original_log  # type: ignore[attr-defined]
        try:
            if state["id"] is not None:
                original_remove(logger, state["id"])
        except Exception:
            pass

    return undo


# ─── structlog ──────────────────────────────────────────────────────────────────

_STRUCTLOG_SKIP = ("event", "exc_info", "stack_info")


def capture_structlog(otel_logger, threshold: int) -> Optional[Undo]:
    """Each structlog event, with its bound and passed fields, as the processors accept it. A
    structlog logger that wraps a ``logging`` logger is left to the ``logging`` bridge."""
    if not _installed("structlog"):
        return None
    try:
        from structlog._base import BoundLoggerBase
    except Exception:
        return None
    original_process = getattr(BoundLoggerBase, "_process_event", None)
    original_proxy = getattr(BoundLoggerBase, "_proxy_to_logger", None)
    if not callable(original_process) or not callable(original_proxy) or getattr(original_proxy, "_zharp", False):
        return None

    def _proxy_to_logger(self, *args, **kwargs):
        if isinstance(getattr(self, "_logger", None), logging.Logger):
            return original_proxy(self, *args, **kwargs)
        with _output.exact("structlog"):
            return original_proxy(self, *args, **kwargs)

    def _process_event(self, method_name, event, event_kw):
        # A processor that drops the event raises here, and nothing is sent.
        result = original_process(self, method_name, event, event_kw)
        try:
            if _output.owner() in (None, "structlog") and not isinstance(getattr(self, "_logger", None), logging.Logger):
                level = _output.LEVELS.get(str(method_name).lower(), _output.INFO)
                if _LEVEL_NUMBERS[level[1]] >= threshold:
                    fields: Dict[str, Any] = dict(getattr(self, "_context", None) or {})
                    fields.update(event_kw or {})
                    body = event if event is not None else fields.get("event", "")
                    attributes = {"log.source": "structlog"}
                    attributes.update(_context.plain({k: v for k, v in fields.items() if k not in _STRUCTLOG_SKIP}))
                    _output.emit(otel_logger, level, body if isinstance(body, str) else str(body), attributes)
        except Exception:
            pass
        return result

    _proxy_to_logger._zharp = True  # type: ignore[attr-defined]
    BoundLoggerBase._proxy_to_logger = _proxy_to_logger  # type: ignore[method-assign]
    BoundLoggerBase._process_event = _process_event  # type: ignore[method-assign]

    def undo() -> None:
        if BoundLoggerBase._proxy_to_logger is _proxy_to_logger:
            BoundLoggerBase._proxy_to_logger = original_proxy  # type: ignore[method-assign]
        if BoundLoggerBase._process_event is _process_event:
            BoundLoggerBase._process_event = original_process  # type: ignore[method-assign]

    return undo


_LEVEL_NUMBERS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}


def capture_libraries(otel_logger, threshold: int, send_logs: bool) -> List[Undo]:
    """loguru and structlog. Logs off: loguru's exceptions only (they are errors), and structlog
    is left alone."""
    undo: List[Undo] = []
    captures = [lambda: capture_loguru(otel_logger, threshold, send_logs)]
    if send_logs:
        captures.append(lambda: capture_structlog(otel_logger, threshold))
    for capture in captures:
        try:
            fn = capture()
            if fn is not None:
                undo.append(fn)
        except Exception as error:
            _log.debug("Zharp: log capture: %s", error)
    return undo
