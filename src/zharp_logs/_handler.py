"""A ``logging.Handler`` that sends Python's standard logging to Zharp."""

import logging
import threading
from typing import Any, Dict, Optional, Union

from . import _logger
from ._logger import LogLevel

#: Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}

_emitting = threading.local()


def level_for(levelno: int) -> LogLevel:
    if levelno >= logging.CRITICAL:
        return LogLevel.FATAL
    if levelno >= logging.ERROR:
        return LogLevel.ERROR
    if levelno >= logging.WARNING:
        return LogLevel.WARN
    if levelno >= logging.INFO:
        return LogLevel.INFO
    return LogLevel.DEBUG


class ZharpHandler(logging.Handler):
    """Sends every record it gets to Zharp, at the matching level.

    The logger's name goes with each log, as do fields passed with ``extra={...}``, and the
    exception and traceback of ``logger.exception(...)`` or ``exc_info=True``. Start Zharp with
    :func:`zharp_logs.init` first; records before that are dropped.

    .. code-block:: python

        logging.getLogger().addHandler(ZharpHandler())
    """

    def __init__(self, level: Union[int, str] = logging.NOTSET, service: Optional[str] = None) -> None:
        super().__init__(level)
        self.service = service

    def emit(self, record: logging.LogRecord) -> None:
        # Something logged while sending (a library that logs) mustn't loop back here.
        if getattr(_emitting, "active", False):
            return
        _emitting.active = True
        try:
            metadata: Dict[str, Any] = {"logger": record.name}
            for key, value in vars(record).items():
                if key not in _STANDARD and not key.startswith("_"):
                    metadata[key] = value
            exc = record.exc_info[1] if record.exc_info and record.exc_info[1] is not None else None
            if exc is None and record.stack_info:
                metadata.setdefault("stack", record.stack_info)
            _logger.log(level_for(record.levelno), record.getMessage(), metadata, exc=exc, service=self.service)
        except Exception:
            self.handleError(record)
        finally:
            _emitting.active = False


def attach_to_root(level: Union[bool, str, int]) -> str:
    """Add one ZharpHandler to the root logger (not a second on a repeated init). Returns the level
    it captures from, by name."""
    if level is True or (isinstance(level, str) and level.strip().lower() in ("true", "1", "yes", "on", "all")):
        resolved = logging.INFO
    elif isinstance(level, int):
        resolved = level
    else:
        resolved = logging.getLevelName(str(level).strip().upper())
        if not isinstance(resolved, int):
            resolved = logging.INFO
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, ZharpHandler):
            root.removeHandler(handler)
    root.addHandler(ZharpHandler(resolved))
    # The root logger stops at WARNING by default; let the handler see what it asked for.
    if root.level == logging.NOTSET or root.level > resolved:
        root.setLevel(resolved)
    return logging.getLevelName(resolved)
