"""Send your Python app's logs and uncaught exceptions to Zharp.

.. code-block:: python

    import zharp_logs

    zharp_logs.init(api_key="your-ingest-only-key", service="checkout-api")
    zharp_logs.info("User signed in", {"user_id": 42})

Logs are queued and sent in batches from a background thread; ``error`` and ``fatal`` go at once,
and everything left is sent when the program exits.
"""

from typing import Any, Mapping, Optional

from ._handler import ZharpHandler
from ._logger import (
    LogLevel,
    capture_exception,
    close,
    flush,
    init,
    is_active,
    log,
    reset_session_id,
    session_id,
    set_context,
)
from ._version import __version__

__all__ = [
    "LogLevel",
    "ZharpHandler",
    "__version__",
    "capture_exception",
    "close",
    "critical",
    "debug",
    "error",
    "exception",
    "fatal",
    "flush",
    "info",
    "init",
    "is_active",
    "log",
    "reset_session_id",
    "session_id",
    "set_context",
    "warn",
    "warning",
]


def debug(message: Any, metadata: Optional[Mapping[str, Any]] = None) -> None:
    log(LogLevel.DEBUG, message, metadata)


def info(message: Any, metadata: Optional[Mapping[str, Any]] = None) -> None:
    log(LogLevel.INFO, message, metadata)


def warn(message: Any, metadata: Optional[Mapping[str, Any]] = None, *, exc: Optional[BaseException] = None) -> None:
    log(LogLevel.WARN, message, metadata, exc=exc)


def error(message: Any, metadata: Optional[Mapping[str, Any]] = None, *, exc: Optional[BaseException] = None) -> None:
    """Logs at ``error`` and sends at once. An exception — as the message or ``exc`` — brings its
    traceback."""
    log(LogLevel.ERROR, message, metadata, exc=exc)


def fatal(message: Any, metadata: Optional[Mapping[str, Any]] = None, *, exc: Optional[BaseException] = None) -> None:
    """Logs at ``fatal`` and sends at once."""
    log(LogLevel.FATAL, message, metadata, exc=exc)


def exception(message: Any, metadata: Optional[Mapping[str, Any]] = None) -> None:
    """An ``error`` with the exception being handled — call it in an ``except`` block, like
    ``logging.exception``."""
    import sys

    log(LogLevel.ERROR, message, metadata, exc=sys.exc_info()[1])


#: The names Python's ``logging`` uses.
warning = warn
critical = fatal
