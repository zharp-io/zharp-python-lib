"""Frameworks without an OpenTelemetry instrumentation of their own, traced through the generic
WSGI / ASGI middleware or a small hook: Bottle, Quart, Tryton, RQ workers, and any WSGI or ASGI
app wrapped by hand with ``zharp.wsgi(app)`` / ``zharp.asgi(app)``.

Each hook patches the framework's own class (not a module attribute), so an app or job is traced
however its class was imported, before or after ``zharp.init()``.
"""

import importlib
import importlib.util
import logging
from typing import Any, Callable, List, Optional

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_log = logging.getLogger("zharp")

#: Each app object's traced wrapper, by id (Falcon's apps have __slots__, no __dict__ to keep it on).
#: Apps live as long as the process, so holding them here costs nothing.
_WRAPPED: dict = {}

#: Where an RQ job carries the trace of the code that enqueued it.
_RQ_TRACE = "_zharp_trace"

#: ASGI's per-message "http send" / "http receive" spans say nothing the request span doesn't.
_ASGI_OPTIONS = {"exclude_spans": ["receive", "send"]}


def wsgi(app: Callable, tracer_provider: Any = None) -> Callable:
    """A WSGI app, traced: one SERVER span per request, continuing the caller's trace."""
    from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware

    if isinstance(app, OpenTelemetryMiddleware):
        return app
    return OpenTelemetryMiddleware(app, tracer_provider=tracer_provider)


def asgi(app: Callable, tracer_provider: Any = None) -> Callable:
    """An ASGI app, traced: one SERVER span per request, continuing the caller's trace."""
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    if isinstance(app, OpenTelemetryMiddleware):
        return app
    try:
        return OpenTelemetryMiddleware(app, tracer_provider=tracer_provider, **_ASGI_OPTIONS)
    except TypeError:  # an older middleware without exclude_spans
        return OpenTelemetryMiddleware(app, tracer_provider=tracer_provider)


def _wrap_wsgi_call(cls: type, tracer_provider: Any) -> None:
    """Route ``cls.__call__`` (a WSGI app object) through the WSGI middleware, one per instance."""
    original = cls.__call__
    if getattr(original, "_zharp", False):
        return

    def __call__(self, environ, start_response):
        wrapped = _WRAPPED.get(id(self))
        if wrapped is None or wrapped[0] is not self:
            wrapped = (self, wsgi(lambda e, s: original(self, e, s), tracer_provider))
            _WRAPPED[id(self)] = wrapped
        return wrapped[1](environ, start_response)

    __call__._zharp = True  # type: ignore[attr-defined]
    __call__.__wrapped__ = original  # type: ignore[attr-defined]
    cls.__call__ = __call__


def _wrap_asgi_call(cls: type, tracer_provider: Any) -> None:
    """Route ``cls.__call__`` (an ASGI app object) through the ASGI middleware, one per instance."""
    original = cls.__call__
    if getattr(original, "_zharp", False):
        return

    async def __call__(self, scope, receive, send):
        wrapped = _WRAPPED.get(id(self))
        if wrapped is None or wrapped[0] is not self:
            async def app(s, r, w):
                return await original(self, s, r, w)

            wrapped = (self, asgi(app, tracer_provider))
            _WRAPPED[id(self)] = wrapped
        return await wrapped[1](scope, receive, send)

    __call__._zharp = True  # type: ignore[attr-defined]
    __call__.__wrapped__ = original  # type: ignore[attr-defined]
    cls.__call__ = __call__


def name_route(route: Optional[str]) -> None:
    """The request span takes the route it matched ("/orders/<id>"): Zharp groups requests by it,
    rather than by every path ("/orders/7", "/orders/8"…)."""
    if not route:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("http.route", route)
    attributes = getattr(span, "attributes", None) or {}
    method = attributes.get("http.request.method") or attributes.get("http.method")
    if method:
        span.update_name("{} {}".format(method, route))


def record_error(error: BaseException) -> None:
    """An exception a framework turned into a 500 itself, on the request's span."""
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(error, attributes={"exception.escaped": True})
        span.set_status(Status(StatusCode.ERROR, str(error)))


def _hook_init(cls: type, after: Callable[[Any], None], what: str) -> None:
    """Run ``after(app)`` on every instance of ``cls`` as it is created."""
    original = cls.__init__
    if getattr(original, "_zharp", False):
        return

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            after(self)
        except Exception as error:
            _log.debug("Zharp: not tracing this %s app: %s", what, error)

    __init__._zharp = True  # type: ignore[attr-defined]
    __init__.__wrapped__ = original  # type: ignore[attr-defined]
    cls.__init__ = __init__


def _bottle(tracer_provider: Any) -> None:
    import bottle

    _wrap_wsgi_call(bottle.Bottle, tracer_provider)

    def routes(app):
        def matched():
            route = bottle.request.environ.get("route.handle") or bottle.request.environ.get("bottle.route")
            name_route(getattr(route, "rule", None))

        app.add_hook("after_request", matched)  # Bottle matches the route after before_request

    _hook_init(bottle.Bottle, routes, "Bottle")


class _FalconRoutes:
    """Falcon middleware: the request span takes the matched URI template."""

    def process_resource(self, req, resp, resource, params):
        name_route(getattr(req, "uri_template", None))

    async def process_resource_async(self, req, resp, resource, params):
        name_route(getattr(req, "uri_template", None))


def _falcon(tracer_provider: Any) -> None:
    """Falcon through the WSGI / ASGI middleware. OpenTelemetry's Falcon instrumentation replaces
    ``falcon.App`` with a subclass that ``falcon.testing`` can't import (Falcon 4), which would
    break the app's own tests."""
    import falcon

    def on_error(cls: type) -> None:
        original = cls._handle_exception
        if getattr(original, "_zharp", False):
            return
        if asyncio_function(original):
            async def handle(self, req, resp, ex, *args, **kwargs):
                record_error(ex)
                return await original(self, req, resp, ex, *args, **kwargs)
        else:
            def handle(self, req, resp, ex, *args, **kwargs):  # type: ignore[misc]
                record_error(ex)
                return original(self, req, resp, ex, *args, **kwargs)
        handle._zharp = True  # type: ignore[attr-defined]
        cls._handle_exception = handle

    _wrap_wsgi_call(falcon.App, tracer_provider)
    _hook_init(falcon.App, lambda app: app.add_middleware(_FalconRoutes()), "Falcon")
    on_error(falcon.App)
    try:
        import falcon.asgi

        _wrap_asgi_call(falcon.asgi.App, tracer_provider)
        on_error(falcon.asgi.App)
    except ImportError:
        pass


def asyncio_function(fn: Callable) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


def _tryton(tracer_provider: Any) -> None:
    from trytond import wsgi as trytond_wsgi

    _wrap_wsgi_call(trytond_wsgi.TrytondWSGI, tracer_provider)


def _quart(tracer_provider: Any) -> None:
    """Quart serves through ``app.asgi_app``: wrap it on every app as it is created."""
    import quart

    original = quart.Quart.__init__
    if getattr(original, "_zharp", False):
        return

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            self.asgi_app = asgi(self.asgi_app, tracer_provider)

            @self.before_request
            async def _zharp_route():
                rule = getattr(quart.request, "url_rule", None)
                name_route(getattr(rule, "rule", None))
        except Exception as error:
            _log.debug("Zharp: not tracing this Quart app: %s", error)

    __init__._zharp = True  # type: ignore[attr-defined]
    __init__.__wrapped__ = original  # type: ignore[attr-defined]
    quart.Quart.__init__ = __init__

    # Quart answers an unhandled exception with a 500 itself: record it on the request's span.
    from quart.signals import got_request_exception

    async def on_exception(sender, exception=None, **kwargs):
        if exception is not None:
            record_error(exception)

    got_request_exception.connect(on_exception, weak=False)


def _rq(tracer_provider: Any) -> None:
    """Each RQ job a CONSUMER span named after its function; a job that fails records why."""
    from opentelemetry import propagate
    from rq.job import Job
    from rq.queue import Queue

    original = Job.perform
    if getattr(original, "_zharp", False):
        return
    tracer = trace.get_tracer("zharp.rq", tracer_provider=tracer_provider)

    # The trace (and baggage: user, session) of whatever enqueued the job travels in its meta.
    original_enqueue = Queue.enqueue_job

    def enqueue_job(self, job, *args, **kwargs):
        try:
            carrier: dict = {}
            propagate.inject(carrier)
            if carrier:
                job.meta[_RQ_TRACE] = carrier
        except Exception as error:
            _log.debug("Zharp: no trace on this RQ job: %s", error)
        return original_enqueue(self, job, *args, **kwargs)

    Queue.enqueue_job = enqueue_job

    def perform(self, *args, **kwargs):
        name = getattr(self, "func_name", None) or "job"
        attributes = {"messaging.system": "rq", "messaging.destination.name": getattr(self, "origin", "") or "", "messaging.message.id": str(getattr(self, "id", ""))}
        parent = propagate.extract((getattr(self, "meta", None) or {}).get(_RQ_TRACE) or {})
        with tracer.start_as_current_span("rq.job " + name, context=parent, kind=SpanKind.CONSUMER, attributes=attributes, record_exception=False, set_status_on_exception=False) as span:
            try:
                return original(self, *args, **kwargs)
            except BaseException as error:
                span.record_exception(error, attributes={"exception.escaped": True})
                span.set_status(Status(StatusCode.ERROR, str(error)))
                raise

    perform._zharp = True  # type: ignore[attr-defined]
    perform.__wrapped__ = original  # type: ignore[attr-defined]
    Job.perform = perform


def record_request_headers(span, pairs) -> None:
    """Request headers on a span, the way the server instrumentations record them: every one,
    secrets as [REDACTED], unless header capture is off."""
    import os
    import re

    from . import _headers

    if not span.is_recording() or not os.environ.get(_headers.SERVER_REQUEST):
        return
    secret = [re.compile(p, re.I) for p in (os.environ.get(_headers.SANITIZE) or "").split(",") if p]
    for name, value in pairs:
        key = str(name).lower()
        shown = "[REDACTED]" if any(p.search(key) for p in secret) else value
        span.set_attribute("http.request.header." + key.replace("-", "_"), [shown])


def _sanic(tracer_provider: Any) -> None:
    """Sanic runs its own server: a SERVER span per request from its request/response
    middleware, named by the matched route, with the request's headers and any error it raised."""
    import sanic
    from opentelemetry import context as otel_context, propagate

    tracer = trace.get_tracer("zharp.sanic", tracer_provider=tracer_provider)

    def route_of(request) -> Optional[str]:
        route = getattr(request, "route", None)
        path = getattr(route, "path", None)
        if path is None:
            return None
        # "ok/<id:str>" → "/ok/<id>": the parameter's name, not its type.
        import re

        return "/" + re.sub(r"<(\w+):[^>]*>", r"<\1>", str(path).lstrip("/"))

    async def on_request(request):
        parent = propagate.extract({k.lower(): v for k, v in request.headers.items()})
        route = route_of(request)
        name = "{} {}".format(request.method, route or request.path)
        span = tracer.start_span(name, context=parent, kind=SpanKind.SERVER, attributes={
            "http.request.method": request.method,
            "url.path": request.path,
            "url.scheme": request.scheme,
            **({"http.route": route} if route else {}),
        })
        record_request_headers(span, request.headers.items())
        request.ctx._zharp_span = span
        request.ctx._zharp_token = otel_context.attach(trace.set_span_in_context(span, parent))

    async def on_response(request, response):
        span = getattr(request.ctx, "_zharp_span", None)
        if span is None:
            return
        status = getattr(response, "status", 200)
        span.set_attribute("http.response.status_code", status)
        if status >= 500:
            span.set_status(Status(StatusCode.ERROR))
        span.end()
        token = getattr(request.ctx, "_zharp_token", None)
        if token is not None:
            try:
                otel_context.detach(token)
            except Exception:
                pass

    async def on_exception(request, exception):
        span = getattr(getattr(request, "ctx", None), "_zharp_span", None)
        if span is not None and span.is_recording():
            span.record_exception(exception, attributes={"exception.escaped": True})
            span.set_status(Status(StatusCode.ERROR, str(exception)))

    def install(app):
        app.register_middleware(on_request, "request")
        app.register_middleware(on_response, "response")
        app.signal("http.lifecycle.exception")(on_exception)

    _hook_init(sanic.Sanic, install, "Sanic")


#: (name, library that must be importable, hook)
HOOKS: List[tuple] = [
    ("sanic", "sanic", _sanic),
    ("falcon", "falcon", _falcon),
    ("bottle", "bottle", _bottle),
    ("quart", "quart", _quart),
    ("tryton", "trytond", _tryton),
    ("rq", "rq", _rq),
]

#: The ones that are web frameworks: logs only still reads their requests' trace and baggage.
WEB = ("falcon", "bottle", "quart", "tryton", "sanic")


def tornado_request_hook(span, handler) -> None:
    """Tornado's instrumentation doesn't read the header variables the others do: record the
    request's headers the same way, secrets as [REDACTED]."""
    import os
    import re

    from . import _headers

    if not span.is_recording():
        return
    # Tornado names the span by its route template ("GET /ok/{id}") but leaves http.route unset.
    method, _, route = (getattr(span, "name", "") or "").partition(" ")
    if method and route.startswith("/"):
        span.set_attribute("http.route", route)
    if not os.environ.get(_headers.SERVER_REQUEST):
        return
    secret = [re.compile(p, re.I) for p in (os.environ.get(_headers.SANITIZE) or "").split(",") if p]
    for name, value in handler.request.headers.get_all():
        key = name.lower()
        shown = "[REDACTED]" if any(p.search(key) for p in secret) else value
        span.set_attribute("http.request.header." + key.replace("-", "_"), [shown])


def install(tracer_provider: Any = None, only: Optional[tuple] = None) -> List[str]:
    """Hook every framework above that is installed. Returns their names."""
    done = []
    for name, library, hook in HOOKS:
        if only is not None and name not in only:
            continue
        try:
            if importlib.util.find_spec(library) is None:
                continue
            hook(tracer_provider)
            done.append(name)
        except Exception as error:
            _log.debug("Zharp: not tracing %s: %s", name, error)
    return done
