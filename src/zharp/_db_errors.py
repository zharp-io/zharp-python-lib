"""Failed database queries as errors that say where they came from.

A driver's exception is recorded on the query's span inside the instrumentation, before it has
travelled anywhere: its traceback is the instrumentation and the driver, none of the app. So:

- The instrumentation's ``record_exception`` on a query's span is held back, not reported, and
  the frames of the app's call are taken there (raw; formatted only if it is reported).
- If the exception then escapes the request (Django, Flask or FastAPI report it), that one report
  carries the query — statement, system, database, code — and by then its traceback is complete.
- If the app caught it, it is reported as handled when the request ends, with the call site as its
  stack.

One report per failure, either way. A wrapper (SQLAlchemy's ``IntegrityError``) counts as its
driver's exception (``.orig``, ``__cause__``).
"""

import os
import sys
import sysconfig
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

from . import _errors, _source

_DB_SYSTEM = ("db.system", "db.system.name")
_STATEMENT = ("db.statement", "db.query.text")
_DATABASE = ("db.name", "db.namespace")
#: How long a failure outside any request waits for a handler before it is reported as caught.
_LONE_WAIT = 1.5
_MAX_PENDING = 200
_MAX_FRAMES = 40

#: Frames that are never the app's: OpenTelemetry, Zharp, wrapt, and the interpreter's own machinery
#: — by where those packages are installed, not by name (an app may live under a "zharp" folder).
def _package_dirs() -> tuple:
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    for name in ("opentelemetry", "wrapt"):
        try:
            module = __import__(name)
            dirs.extend(getattr(module, "__path__", []) or [os.path.dirname(getattr(module, "__file__", "") or "")])
        except Exception:
            pass
    return tuple(os.path.normcase(os.path.realpath(d)) + os.sep for d in dirs if d)


_NOT_APP = _package_dirs()
_LIBRARY = ("site-packages", "dist-packages")

Frame = Tuple[str, int, str]

#: Python's own library (threading, contextlib, asyncio…): never the app's code.
_STDLIB = tuple(
    {os.path.normcase(os.path.realpath(p)) for p in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")) if p}
)


def _never_app(filename: str) -> bool:
    if filename.startswith("<"):  # <frozen importlib._bootstrap>, <string>
        return True
    normal = os.path.normcase(os.path.realpath(filename)) if os.path.isabs(filename) else os.path.normcase(filename)
    if normal.startswith(_NOT_APP):
        return True
    return any(normal.startswith(root) and "site-packages" not in normal for root in _STDLIB)


def _first(attributes: Any, names) -> Optional[str]:
    for name in names:
        value = attributes.get(name) if attributes else None
        if value not in (None, ""):
            return str(value)
    return None


def call_site(skip: int = 2) -> List[Frame]:
    """The current call's frames, outermost last: (file, line, function), nothing formatted yet."""
    frames: List[Frame] = []
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return frames
    while frame is not None and len(frames) < 80:
        code = frame.f_code
        frames.append((code.co_filename, frame.f_lineno, code.co_name))
        frame = frame.f_back
    return frames


def app_frames(frames: List[Frame]) -> List[Frame]:
    """The app's part of a call site, innermost first: from its own first frame on (a query
    builder or ORM between the app and the driver is left out), never OpenTelemetry or Zharp."""
    kept = [f for f in frames if not _never_app(f[0])]
    first_app = next((i for i, f in enumerate(kept) if not any(lib in f[0] for lib in _LIBRARY)), 0)
    return kept[first_app:][:_MAX_FRAMES]


def format_stack(frames: List[Frame], exc: BaseException) -> str:
    """A Python traceback of the call site (outermost first), ending with the exception's line."""
    summary = traceback.StackSummary.from_list([(f, line, name, None) for f, line, name in reversed(frames)])
    for entry in summary:
        try:
            import linecache

            entry._line = linecache.getline(entry.filename, entry.lineno).strip()  # type: ignore[attr-defined]
        except Exception:
            pass
    return "Traceback (most recent call last):\n" + "".join(summary.format()) + _errors.scrub(
        "".join(traceback.format_exception_only(type(exc), exc))
    ) + "\n"


def related(exc: Any) -> List[Any]:
    """An exception and what it wraps: SQLAlchemy's ``.orig``, Django's ``__cause__``."""
    out = [exc]
    for name in ("orig", "__cause__", "__context__"):
        inner = getattr(exc, name, None)
        if isinstance(inner, BaseException) and inner not in out:
            out.append(inner)
    return out


def query_attributes(attributes: Any, exc: Optional[BaseException]) -> Dict[str, str]:
    """What goes on the error about its query."""
    out: Dict[str, str] = {"db.system": _first(attributes, _DB_SYSTEM) or "database"}
    statement = _first(attributes, _STATEMENT)
    if statement:
        out["db.statement"] = statement[:4000]
    database = _first(attributes, _DATABASE)
    if database:
        out["db.name"] = database
    code = None
    for candidate in related(exc) if exc is not None else []:
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            break
    if code:
        out["db.error.code"] = str(code)
    for candidate in related(exc) if exc is not None else []:
        diag = getattr(candidate, "diag", None)
        constraint = getattr(diag, "constraint_name", None) if diag is not None else None
        if constraint:
            out["db.error.constraint"] = str(constraint)
            break
    return out


def _span_id(span: Any) -> int:
    context = span.get_span_context() if hasattr(span, "get_span_context") else span.context
    return context.span_id if context else id(span)


class _Failure:
    __slots__ = ("exc", "frames", "attributes", "span_context", "done")

    def __init__(self, exc, frames, attributes, span_context):
        self.exc = exc
        self.frames = frames
        self.attributes = attributes
        self.span_context = span_context
        self.done = False


class DbErrorSpanProcessor(SpanProcessor):
    """Watches query spans: their call site when they start, their failure when they end.
    ``send(exc, stack, attributes, span_context)`` reports, as caught, what nothing else claimed."""

    def __init__(self, send: Callable[[BaseException, str, Dict[str, str], Any], None]):
        self._send = send
        self._lock = threading.Lock()
        self._sites: Dict[int, List[Frame]] = {}
        self._held: Dict[int, BaseException] = {}
        self._pending: Dict[int, List[_Failure]] = {}

    # Called by the patched Span.record_exception.
    def hold(self, span: Any, exception: BaseException) -> bool:
        """Keep a query span's exception instead of recording it. True when held."""
        if not _first(getattr(span, "attributes", None), _DB_SYSTEM):
            return False
        # Taken here, not when the span started: the instrumentation names the database only
        # after it starts, and this runs inside the app's own call, so the stack is all there —
        # and a query that succeeds costs nothing.
        site = call_site(2)
        with self._lock:
            key = _span_id(span)
            if key not in self._held:
                self._held[key] = exception
                self._sites[key] = site
        return True

    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        # on_end gets a read-only copy, not the span on_start saw: matched by span id.
        key = _span_id(span)
        with self._lock:
            frames = self._sites.pop(key, None)
            exc = self._held.pop(key, None)
        trace_id = span.context.trace_id if span.context else 0
        if frames is not None and exc is not None and span.status.status_code == StatusCode.ERROR:
            self._add(trace_id, _Failure(exc, app_frames(frames), query_attributes(span.attributes, exc), span.context))
        parent = span.parent
        if span.kind == SpanKind.SERVER or parent is None or parent.is_remote:
            with self._lock:
                waiting = trace_id in self._pending
            if not waiting:
                return
            if frames is not None or span.status.status_code == StatusCode.ERROR:
                # The query was the whole trace, or its root failed: the exception may still be on
                # its way to a handler, which reports it itself.
                timer = threading.Timer(_LONE_WAIT, self.flush, args=(trace_id,))
                timer.daemon = True
                timer.start()
            else:
                self.flush(trace_id)

    def _add(self, trace_id: int, failure: _Failure) -> None:
        with self._lock:
            failures = self._pending.setdefault(trace_id, [])
            # The same failure seen by two instrumentations (SQLAlchemy over psycopg): once.
            mine = related(failure.exc)
            if any(f.exc in mine or failure.exc in related(f.exc) for f in failures):
                return
            failures.append(failure)
            if len(self._pending) > _MAX_PENDING:
                oldest = next(iter(self._pending))
                if oldest != trace_id:
                    self._pending.pop(oldest, None)

    def claim(self, exc: Optional[BaseException]) -> Optional[Dict[str, str]]:
        """``exc`` is being reported by a framework or the app: the query attributes of the failed
        query it came from, if any, and that failure is not reported again."""
        if exc is None:
            return None
        wanted = related(exc)
        with self._lock:
            for failures in self._pending.values():
                for failure in failures:
                    if not failure.done and any(c is failure.exc or c in related(failure.exc) for c in wanted):
                        failure.done = True
                        return dict(failure.attributes)
        return None

    def flush(self, trace_id: int) -> None:
        with self._lock:
            failures = self._pending.pop(trace_id, [])
        for failure in failures:
            if failure.done or _errors.is_reported(failure.exc):
                continue
            try:
                attributes = dict(failure.attributes)
                # The call site's code, not the driver's: where the app ran the query.
                code = _source.context_of((f, line) for f, line, _ in failure.frames)
                if code:
                    attributes[_source.ATTRIBUTE] = code
                self._send(failure.exc, format_stack(failure.frames, failure.exc), attributes, failure.span_context)
            except Exception:
                pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        for trace_id in list(self._pending):
            self.flush(trace_id)
        return True

    def shutdown(self) -> None:
        self.force_flush()
