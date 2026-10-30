"""Who and what a span or log belongs to: the user, the session, and tags.

A browser or mobile app sends ``enduser.id`` and ``session.id`` in W3C ``baggage`` next to
``traceparent``. The processors here copy them onto every span and log of that request, so the
back end's telemetry joins the front end's. ``set_user`` overrides them for the rest of the
current request (or, outside one, for the rest of the program).
"""

import threading
from typing import Any, Dict, Mapping, Optional

from opentelemetry import baggage, context as otel_context, trace
from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry.sdk.trace import SpanProcessor

USER_ID = "enduser.id"
USER_EMAIL = "enduser.email"
USER_NAME = "enduser.name"
SESSION_ID = "session.id"

#: Copied from baggage, and inherited from the parent span when baggage isn't in the context.
_PROPAGATED = (USER_ID, SESSION_ID)
_INHERITED = (USER_ID, USER_EMAIL, USER_NAME, SESSION_ID)

_USER_KEY = otel_context.create_key("zharp.user")

#: Marks a log whose exception was already reported elsewhere; the processor drops its exception.*.
DUPLICATE_EXCEPTION = "zharp.duplicate_exception"
#: Set on a logging record by _errors.ExceptionFilter; applied and removed by ContextLogProcessor.
EXCEPTION_TYPE_OVERRIDE = "zharp.exception_type"
EXCEPTION_MESSAGE_OVERRIDE = "zharp.exception_message"

_tags: Dict[str, Any] = {}
_tags_lock = threading.Lock()


def set_user(id: Optional[str] = None, email: Optional[str] = None, name: Optional[str] = None) -> None:
    """Attach a user to the current request (or, outside one, to everything after)."""
    user = {k: str(v) for k, v in ((USER_ID, id), (USER_EMAIL, email), (USER_NAME, name)) if v is not None}
    # Attached without a detach: the instrumentation detaches its own token when the request
    # ends, which drops this with it, so one request's user can't leak into the next.
    otel_context.attach(otel_context.set_value(_USER_KEY, user or None))
    span = trace.get_current_span()
    if user and span.is_recording():
        span.set_attributes(user)


def clear_user() -> None:
    otel_context.attach(otel_context.set_value(_USER_KEY, {}))


def set_tag(key: str, value: Any) -> None:
    """An attribute on every span and log from now on. None removes it."""
    with _tags_lock:
        if value is None:
            _tags.pop(key, None)
        else:
            _tags[key] = value if isinstance(value, (str, bool, int, float)) else str(value)


def attributes_for(ctx: Optional[otel_context.Context] = None) -> Dict[str, Any]:
    """The tags, then user and session from the parent span, baggage and set_user (later wins)."""
    ctx = ctx if ctx is not None else otel_context.get_current()
    out: Dict[str, Any] = dict(_tags) if _tags else {}

    parent = trace.get_current_span(ctx)
    parent_attrs = getattr(parent, "attributes", None)
    if parent_attrs:
        for key in _INHERITED:
            value = parent_attrs.get(key)
            if value:
                out[key] = value

    for key in _PROPAGATED:
        value = baggage.get_baggage(key, ctx)
        if value:
            out[key] = str(value)

    user = otel_context.get_value(_USER_KEY, ctx)
    if user is not None:
        for key in (USER_ID, USER_EMAIL, USER_NAME):
            out.pop(key, None)
        out.update(user)
    return out


class ContextSpanProcessor(SpanProcessor):
    """Puts user, session and tags on every span as it starts."""

    def on_start(self, span, parent_context=None) -> None:
        try:
            attrs = attributes_for(parent_context)
            if attrs:
                span.set_attributes(attrs)
        except Exception:
            pass

    def on_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class ContextLogProcessor(LogRecordProcessor):
    """Puts user, session and tags on every log record, before it is batched."""

    def on_emit(self, log_record) -> None:
        try:
            record = getattr(log_record, "log_record", log_record)
            attributes = record.attributes
            if attributes is None:
                return
            if attributes.get(DUPLICATE_EXCEPTION):
                for key in [k for k in attributes if k.startswith("exception.")] + [DUPLICATE_EXCEPTION, "zharp.handled"]:
                    if key in attributes:
                        del attributes[key]
            for override, target in ((EXCEPTION_TYPE_OVERRIDE, "exception.type"), (EXCEPTION_MESSAGE_OVERRIDE, "exception.message")):
                if override in attributes:
                    if target in attributes or "exception.stacktrace" in attributes:
                        attributes[target] = attributes[override]
                    del attributes[override]
            for key, value in attributes_for().items():
                if key not in attributes:
                    attributes[key] = value
        except Exception:
            pass

    # Older SDKs call it emit.
    emit = on_emit

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def plain(attributes: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """User attributes as OTel attribute values: primitives kept, everything else as text."""
    out: Dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            out[str(key)] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, (str, bool, int, float)) for v in value):
            out[str(key)] = list(value)
        else:
            out[str(key)] = str(value)
    return out
