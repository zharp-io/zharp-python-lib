"""Errors: one report per exception, marked handled or not.

An exception that escapes a request is seen twice — the instrumentation records it on the request's
span, and the framework logs it (``django.request``, Flask's app logger, ``uvicorn.error``). Zharp
turns both into error events, so each exception object is marked when it is first reported and
later sightings keep their log line or span status but drop the duplicate exception.
"""

import logging
import re
import sys
import threading
import time
import traceback
from typing import Any, Dict, Optional

from ._context import DUPLICATE_EXCEPTION, EXCEPTION_MESSAGE_OVERRIDE, EXCEPTION_TYPE_OVERRIDE

_MARK = "__zharp_reported__"


#: Loggers through which frameworks and servers report exceptions that escaped a request.
_UNCAUGHT_LOGGERS = {
    "django.request",
    "django.server",
    "uvicorn.error",
    "gunicorn.error",
    "hypercorn.error",
    "waitress",
    "tornado.application",
}
#: Flask logs "Exception on /path [GET]" through the app's own logger, named after the app.
_FLASK_UNCAUGHT = re.compile(r"^Exception on \S* \[[A-Z]+\]$")


def is_reported(exc: Optional[BaseException]) -> bool:
    return exc is not None and getattr(exc, _MARK, False) is True


def mark_reported(exc: Optional[BaseException]) -> None:
    if exc is None:
        return
    try:
        setattr(exc, _MARK, True)
    except Exception:
        pass


def exception_type(exc: BaseException) -> str:
    """``ValueError``, or ``module.Qualname`` outside builtins — what OTel puts on spans."""
    cls = type(exc)
    module = cls.__module__
    return "{}.{}".format(module, cls.__qualname__) if module and module != "builtins" else cls.__qualname__


def exception_attributes(exc: BaseException, handled: bool) -> Dict[str, Any]:
    return {
        "exception.type": exception_type(exc),
        "exception.message": str(exc),
        "exception.stacktrace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "zharp.handled": "true" if handled else "false",
    }


def patch_record_exception() -> None:
    """Span.record_exception skips an exception already reported, and says handled=false.

    Instrumentations call it for an exception that escaped their span, which is what an uncaught
    error is; a caller that knows better passes ``{"zharp.handled": "true"}``.
    """
    from opentelemetry.sdk.trace import Span

    original = Span.record_exception
    if getattr(original, "_zharp", False):
        return

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False):
        if is_reported(exception):
            return None
        mark_reported(exception)
        merged = {"zharp.handled": "false"}
        if attributes:
            merged.update(attributes)
        return original(self, exception, merged, timestamp, escaped)

    record_exception._zharp = True  # type: ignore[attr-defined]
    Span.record_exception = record_exception  # type: ignore[method-assign]


class ExceptionFilter(logging.Filter):
    """On Zharp's logging handler: marks each logged exception, flags repeats, and says whether it
    was caught by the app (handled) or reported by the framework because it escaped (not)."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        # OTel's own loggers (an exporter that failed) would loop back into the exporter.
        if name.startswith("opentelemetry") or name == "zharp" or name.startswith("zharp."):
            return False
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            if is_reported(exc):
                record.__dict__[DUPLICATE_EXCEPTION] = True
            else:
                mark_reported(exc)
                # The handler names the type without its module and takes args[0] as the message;
                # the processor puts these back so a log groups with the same error on a span.
                record.__dict__[EXCEPTION_TYPE_OVERRIDE] = exception_type(exc)
                record.__dict__[EXCEPTION_MESSAGE_OVERRIDE] = str(exc)
                uncaught = name in _UNCAUGHT_LOGGERS or bool(_FLASK_UNCAUGHT.match(str(record.msg)))
                record.__dict__.setdefault("zharp.handled", "false" if uncaught else "true")
        return True


class _Hooks:
    installed = False
    previous_sys = None
    previous_threading = None


def install_uncaught_hooks(report, flush) -> None:
    """sys.excepthook and threading.excepthook: report (handled=false), send, then the previous hook."""
    if _Hooks.installed:
        return
    _Hooks.installed = True
    _Hooks.previous_sys = sys.excepthook

    def excepthook(exc_type, exc, tb):
        try:
            if exc is not None and not isinstance(exc, KeyboardInterrupt) and not is_reported(exc):
                report(exc, handled=False, fatal=True)
                flush(2.0)
        except Exception:
            pass
        previous = _Hooks.previous_sys or sys.__excepthook__
        previous(exc_type, exc, tb)

    sys.excepthook = excepthook

    if hasattr(threading, "excepthook"):
        _Hooks.previous_threading = threading.excepthook

        def thread_excepthook(args):
            try:
                exc = args.exc_value
                if exc is not None and not isinstance(exc, SystemExit) and not is_reported(exc):
                    report(exc, handled=False, fatal=False)
            except Exception:
                pass
            previous = _Hooks.previous_threading or threading.__excepthook__
            previous(args)

        threading.excepthook = thread_excepthook


def now_ns() -> int:
    return time.time_ns()
