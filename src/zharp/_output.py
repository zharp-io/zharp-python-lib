"""The process's output as logs: everything written to ``sys.stdout`` and ``sys.stderr`` that the
exact captures (``logging``, loguru, structlog, ``zharp.log``) didn't already send.

Plenty of Python output never goes through ``logging``: ``print()``, click/rich output, libraries
and hand-rolled loggers writing to ``sys.stdout``. This is the net under the exact captures, so a
line printed any way at all still arrives, in the request it was written in. Each line still prints
exactly as before.

A line's level is read from the line itself: a JSON record's own level, or a level word such as
``ERROR``, ``[warn]``, ``level=error``, ``WARNING:root:``, uvicorn's ``INFO:``; failing that,
stdout is info and stderr is a warning.

What the captures share lives here too, so a line is sent once however many of them see it:

- while an exact capture is printing a line it sends itself, the output capture stands aside
  (:func:`quiet`, per thread);
- when captures nest (``logging`` routed into loguru, structlog on top of ``logging``), the
  outermost one owns the line and the others leave it (:func:`exact`, :func:`owner`);
- bodies the exact captures sent in the last few seconds are remembered, and a printed line that
  contains one is skipped (a handler that prints later, from a queue or another thread).
"""

import atexit
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from opentelemetry import context as otel_context
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LogRecordProcessor

# ─── What the captures share ────────────────────────────────────────────────────

_local = threading.local()


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """The output capture ignores what this thread writes until the block ends."""
    _local.quiet = getattr(_local, "quiet", 0) + 1
    try:
        yield
    finally:
        _local.quiet -= 1


def is_quiet() -> bool:
    return getattr(_local, "quiet", 0) > 0


@contextlib.contextmanager
def exact(name: str) -> Iterator[None]:
    """An exact capture (``"logging"``, ``"loguru"``, ``"structlog"``) is handling a call: it owns
    the line unless an outer capture already does, and what it prints is not read again."""
    previous = getattr(_local, "owner", None)
    if previous is None:
        _local.owner = name
    try:
        with quiet():
            yield
    finally:
        _local.owner = previous


def owner() -> Optional[str]:
    """The capture that owns the call in progress on this thread, if any."""
    return getattr(_local, "owner", None)


#: Bodies sent in the last few seconds, by a capture other than the output's.
RECENT_SECONDS = 3.0
RECENT_MAX = 500
_recent: List[Tuple[float, str]] = []
_recent_lock = threading.Lock()
_SPACES = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _reset_recent() -> None:
    global _recent_lock
    _recent_lock = threading.Lock()
    del _recent[:]


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_recent)


def remember_sent(body: Any) -> None:
    if not isinstance(body, str):
        return
    text = _normalise(body)
    if len(text) < 4:
        return
    with _recent_lock:
        _recent.append((time.monotonic(), text))
        if len(_recent) > RECENT_MAX:
            del _recent[: len(_recent) - RECENT_MAX]


def already_sent(line: str) -> bool:
    """Whether a printed line is one a capture already sent: its text contains a recent body."""
    now = time.monotonic()
    with _recent_lock:
        while _recent and now - _recent[0][0] > RECENT_SECONDS:
            _recent.pop(0)
        if not _recent:
            return False
        text = _normalise(line)
        for i in range(len(_recent) - 1, -1, -1):
            if _recent[i][1] in text:
                del _recent[i]
                return True
    return False


OUTPUT_SOURCES = ("stdout", "stderr")


class SentLineProcessor(LogRecordProcessor):
    """Remembers what every other capture sent, so the same line reaching the output a moment
    later isn't sent twice."""

    def on_emit(self, log_record) -> None:
        try:
            record = getattr(log_record, "log_record", log_record)
            if (record.attributes or {}).get("log.source") not in OUTPUT_SOURCES:
                remember_sent(record.body)
        except Exception:
            pass

    # Older SDKs call it emit.
    emit = on_emit

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ─── Levels ─────────────────────────────────────────────────────────────────────

Level = Tuple[SeverityNumber, str]
TRACE: Level = (SeverityNumber.TRACE, "TRACE")
DEBUG: Level = (SeverityNumber.DEBUG, "DEBUG")
INFO: Level = (SeverityNumber.INFO, "INFO")
WARN: Level = (SeverityNumber.WARN, "WARN")
ERROR: Level = (SeverityNumber.ERROR, "ERROR")
FATAL: Level = (SeverityNumber.FATAL, "FATAL")

LEVELS: Dict[str, Level] = {
    "trace": TRACE,
    "verbose": DEBUG,
    "debug": DEBUG,
    "log": INFO,
    "msg": INFO,
    "info": INFO,
    "notice": INFO,
    "success": INFO,
    "warn": WARN,
    "warning": WARN,
    "error": ERROR,
    "err": ERROR,
    "exception": ERROR,
    "crit": FATAL,
    "critical": FATAL,
    "fatal": FATAL,
    "emerg": FATAL,
    "alert": FATAL,
}


def level_of_number(number: float) -> Level:
    """Python's numbers (``logging`` and loguru share them): 50 critical, 40 error, 30 warning,
    20 info, 10 debug, below that trace."""
    if number >= 50:
        return FATAL
    if number >= 40:
        return ERROR
    if number >= 30:
        return WARN
    if number >= 20:
        return INFO
    if number >= 10:
        return DEBUG
    return TRACE


def level_of(value: Any) -> Optional[Level]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return level_of_number(value)
    if isinstance(value, str):
        return LEVELS.get(value.strip().lower())
    return None


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: The first level word in the opening of a line: ``ERROR``, ``[warn]``, ``level=error``,
#: ``WARNING:root:``, ``INFO:     `` (uvicorn), ``[INFO]`` (gunicorn).
_WORD = re.compile(
    r"(?:^|[\s\[(|:=\"'])(trace|verbose|debug|info|notice|warn(?:ing)?|err(?:or)?|crit(?:ical)?|fatal|emerg|alert)"
    r"(?=$|[\s\]):|,\"'])",
    re.IGNORECASE,
)

MAX_LINE = 32 * 1024
FLUSH_SECONDS = 0.2


def parse_line(raw: str) -> Tuple[Optional[Level], str]:
    """A line, read for its level and, for a JSON record, its own message."""
    line = _ANSI.sub("", raw).rstrip()
    if line.startswith("{") and line.endswith("}"):
        try:
            record = json.loads(line)
        except ValueError:
            record = None  # not JSON after all; read it as text
        if isinstance(record, dict):
            level = None
            for key in ("level", "severity", "levelname", "lvl", "log.level"):
                if key in record:
                    level = level_of(record[key])
                    if level is not None:
                        break
            message = record.get("msg", record.get("message", record.get("event")))
            return level, message if isinstance(message, str) and message.strip() else line
    word = _WORD.search(line[:120])
    return (LEVELS.get(word.group(1).lower()) if word else None), line


# ─── Emitting ───────────────────────────────────────────────────────────────────


def emit(
    logger,
    level: Level,
    body: str,
    attributes: Optional[Dict[str, Any]] = None,
    ctx: Optional[otel_context.Context] = None,
) -> None:
    """A log record, in ``ctx`` (else the current context). Nothing the export prints is read."""
    token = otel_context.attach(ctx) if ctx is not None else None
    try:
        with quiet():
            logger.emit(
                LogRecord(
                    timestamp=time.time_ns(),
                    context=ctx if ctx is not None else otel_context.get_current(),
                    severity_text=level[1],
                    severity_number=level[0],
                    body=body,
                    attributes=attributes or {},
                )
            )
    except Exception:
        pass
    finally:
        if token is not None:
            otel_context.detach(token)


# ─── The tap ────────────────────────────────────────────────────────────────────


class _Pending:
    __slots__ = ("text", "ctx", "since")

    def __init__(self) -> None:
        self.text = ""
        self.ctx: Optional[otel_context.Context] = None
        self.since = 0.0


class _Tap:
    """Splits one stream's writes into lines, per thread, and sends each line."""

    def __init__(self, name: str, fallback: Level, logger, flusher: "_Flusher") -> None:
        self.name = name
        self.fallback = fallback
        self.logger = logger
        self.flusher = flusher
        self.enabled = True
        self._lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}

    def after_fork(self) -> None:
        """A worker forked from a preloading server (gunicorn --preload): the parent's threads
        and lock holders don't exist here."""
        self._lock = threading.Lock()
        self._pending = {}

    def take(self, text: Any) -> None:
        if not self.enabled or not isinstance(text, str) or not text or is_quiet():
            return
        if threading.current_thread().name.startswith("Otel"):
            return  # the exporter's own threads: what they print is about the reporting
        done: List[Tuple[str, Optional[otel_context.Context]]] = []
        with self._lock:
            key = threading.get_ident()
            pending = self._pending.get(key)
            if pending is None:
                pending = self._pending[key] = _Pending()
            if not pending.text:
                # A line belongs to the request it started in.
                pending.ctx = otel_context.get_current()
                pending.since = time.monotonic()
            pending.text += text
            if "\n" in pending.text:
                *lines, rest = pending.text.split("\n")
                done.extend((line, pending.ctx) for line in lines)
                pending.text = rest
                if rest:
                    pending.ctx = otel_context.get_current()
                    pending.since = time.monotonic()
            if len(pending.text) > MAX_LINE:
                done.append((pending.text, pending.ctx))
                pending.text = ""
            if not pending.text:
                del self._pending[key]
            else:
                self.flusher.wake()
        for line, ctx in done:
            self.send(line, ctx)

    def flush(self, older_than: Optional[float] = None) -> bool:
        """Send unfinished lines (those waiting longer than ``older_than`` seconds). True while
        some are still waiting."""
        done = []
        now = time.monotonic()
        with self._lock:
            for key, pending in list(self._pending.items()):
                if older_than is None or now - pending.since >= older_than:
                    done.append((pending.text, pending.ctx))
                    del self._pending[key]
            waiting = bool(self._pending)
        for line, ctx in done:
            self.send(line, ctx)
        return waiting

    def send(self, line: str, ctx: Optional[otel_context.Context]) -> None:
        try:
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE]
            if not line.strip() or "[Zharp]" in line or already_sent(line):
                return
            level, body = parse_line(line)
            if not body.strip():
                return
            attributes = {"log.source": self.name, "log.iostream": self.name}
            emit(self.logger, level or self.fallback, body, attributes, ctx)
        except Exception:
            pass


class _Flusher:
    """One background thread that sends lines left unfinished for longer than FLUSH_SECONDS; it
    sleeps while nothing is waiting."""

    def __init__(self) -> None:
        self.taps: List[_Tap] = []
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)

    def _after_fork(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._thread = None  # started again on the next unfinished line
    def wake(self) -> None:
        self._event.set()
        thread = self._thread
        if thread is None or not thread.is_alive():  # never started, or lost to a fork
            with self._lock:
                if (self._thread is None or not self._thread.is_alive()) and not self._stopped:
                    self._thread = threading.Thread(target=self._run, name="zharp-output", daemon=True)
                    self._thread.start()

    def _run(self) -> None:
        while not self._stopped:
            self._event.wait()
            self._event.clear()
            while not self._stopped:
                time.sleep(FLUSH_SECONDS / 2)
                waiting = False
                for tap in self.taps:
                    waiting = tap.flush(FLUSH_SECONDS) or waiting
                if not waiting:
                    break

    def stop(self) -> None:
        self._stopped = True
        self._event.set()


class _Stream:
    """Stands in for ``sys.stdout`` / ``sys.stderr``: writes go to the real stream exactly as
    before, and a copy of each one to the tap. Everything else is the real stream's."""

    def __init__(self, target, tap: _Tap) -> None:
        object.__setattr__(self, "_zharp_target", target)
        object.__setattr__(self, "_zharp_tap", tap)

    def write(self, text):
        result = self._zharp_target.write(text)
        try:
            self._zharp_tap.take(text)
        except Exception:
            pass  # a line that could not be copied is not worth an exception in the caller
        return result

    def writelines(self, lines):
        lines = list(lines)
        result = self._zharp_target.writelines(lines)
        try:
            for text in lines:
                self._zharp_tap.take(text)
        except Exception:
            pass
        return result

    def __getattr__(self, name):
        return getattr(self._zharp_target, name)

    def __setattr__(self, name, value):
        setattr(self._zharp_target, name, value)

    def __iter__(self):
        return iter(self._zharp_target)

    def __enter__(self):
        return self._zharp_target.__enter__()

    def __exit__(self, *args):
        return self._zharp_target.__exit__(*args)

    def __repr__(self):
        return repr(self._zharp_target)


# Code that checks isinstance(sys.stdout, io.TextIOBase) keeps getting True.
io.TextIOBase.register(_Stream)


def capture_output(logger) -> Callable[[], None]:
    """Tap ``sys.stdout`` and ``sys.stderr``. Returns the undo, which sends any unfinished line."""
    flusher = _Flusher()
    undo: List[Callable[[], None]] = []

    for name, fallback in (("stdout", INFO), ("stderr", WARN)):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _Stream) or not callable(getattr(stream, "write", None)):
            continue
        tap = _Tap(name, fallback, logger, flusher)
        flusher.taps.append(tap)
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=tap.after_fork)
        proxy = _Stream(stream, tap)
        setattr(sys, name, proxy)

        def restore(name=name, stream=stream, proxy=proxy, tap=tap) -> None:
            tap.flush()
            tap.enabled = False  # if the app replaced or wrapped it since, it now just passes through
            if getattr(sys, name, None) is proxy:
                setattr(sys, name, stream)

        undo.append(restore)

    def flush_at_exit() -> None:
        for tap in flusher.taps:
            tap.flush()

    # Runs before the logger provider's own exit hook (registered earlier), so the last unfinished
    # line is still sent.
    atexit.register(flush_at_exit)

    def stop() -> None:
        flusher.stop()
        atexit.unregister(flush_at_exit)
        for fn in undo:
            try:
                fn()
            except Exception:
                pass

    return stop
