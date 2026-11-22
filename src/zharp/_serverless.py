"""Serverless functions: AWS Lambda (directly, through the Serverless Framework, or Chalice) and
Google Cloud Functions.

A function's process is frozen (or ended) as soon as the handler returns, so whatever the batch
processors still hold would be sent at some later invocation, or never. Each invocation here is one
SERVER span with the OpenTelemetry FaaS attributes (``faas.trigger``, ``faas.invocation_id``,
``faas.coldstart``, ``faas.name``, ``cloud.provider``), continuing the caller's trace when the
trigger carries a ``traceparent``; an exception the handler raises is reported once and raised on;
and everything is flushed — for at most ``flush_timeout`` seconds — before the result goes back.
"""

import contextvars
import functools
import inspect
import logging
import os
import sys
import threading
import uuid
from typing import Any, Callable, Dict, Optional

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_log = logging.getLogger("zharp")

DEFAULT_FLUSH_TIMEOUT = 2.0

#: No invocation has run in this process yet: the next one is the cold start.
_cold = [True]
_cold_lock = threading.Lock()

#: Inside an invocation already (a Chalice app wrapped by hand as well): run nested ones as they are.
_inside = contextvars.ContextVar("zharp_invocation", default=False)


def take_coldstart() -> bool:
    with _cold_lock:
        cold, _cold[0] = _cold[0], False
    # Provisioned concurrency starts the environment ahead of any request: never a cold start.
    return cold and os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE") != "provisioned-concurrency"


# ─── headers ────────────────────────────────────────────────────────────────────


def header(carrier: Any, name: str) -> Optional[str]:
    """A header from a dict (any case), a Werkzeug/Chalice headers object, or None."""
    if carrier is None:
        return None
    try:
        getter = getattr(carrier, "get", None)
        if getter is None:
            return None
        value = getter(name)
        if value is None and isinstance(carrier, dict):
            wanted = name.lower()
            for key, item in carrier.items():
                if isinstance(key, str) and key.lower() == wanted:
                    value = item
                    break
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        return None if value is None else str(value)
    except Exception:
        return None


class _HeaderGetter:
    """OpenTelemetry's getter for any of the above."""

    def get(self, carrier: Any, key: str):
        value = header(carrier, key)
        return None if value is None else [value]

    def keys(self, carrier: Any):
        try:
            return list(carrier.keys())
        except Exception:
            return []


GETTER = _HeaderGetter()


def _clean(attributes: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in attributes.items():
        if value is None or value == "":
            continue
        out[key] = value if isinstance(value, (bool, int, float)) else str(value)
    return out


class Invocation:
    def __init__(self, name: str, attributes: Dict[str, Any], carrier: Any = None, http: bool = False, mechanism: str = "serverless"):
        self.name = name
        self.attributes = _clean(attributes)
        self.carrier = carrier
        self.http = http
        self.mechanism = mechanism


# ─── AWS Lambda ─────────────────────────────────────────────────────────────────


def _lambda_source(event: Any) -> Optional[str]:
    if not isinstance(event, dict):
        return None
    records = event.get("Records")
    record = records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else {}
    source = record.get("eventSource") or record.get("EventSource") or event.get("source")
    return source if isinstance(source, str) else None


def _lambda_trigger(event: Any, http: bool) -> str:
    if http or (isinstance(event, dict) and isinstance(event.get("requestContext"), dict) and "elb" in event["requestContext"]):
        return "http"
    source = _lambda_source(event) or ""
    if source in ("aws:sqs", "aws:sns", "aws:kinesis", "aws:kafka", "aws:mq") or (isinstance(event, dict) and event.get("eventSource") == "aws:kafka"):
        return "pubsub"
    if source in ("aws:s3", "aws:dynamodb"):
        return "datasource"
    if (isinstance(event, dict) and event.get("detail-type") == "Scheduled Event") or source == "aws.scheduler":
        return "timer"
    return "other"


def lambda_invocation(event: Any, context: Any) -> Invocation:
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    request_context = request_context if isinstance(request_context, dict) else {}
    http_v2 = request_context.get("http") if isinstance(request_context.get("http"), dict) else None
    http_v1 = isinstance(event, dict) and isinstance(event.get("httpMethod"), str)
    http = bool(http_v2 or http_v1)
    function_name = getattr(context, "function_name", None) or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    arn = getattr(context, "invoked_function_arn", None)
    arn = arn if isinstance(arn, str) else None
    base = {
        "faas.trigger": _lambda_trigger(event, http),
        "faas.invocation_id": getattr(context, "aws_request_id", None),
        "faas.coldstart": take_coldstart(),
        "faas.name": function_name,
        "faas.version": getattr(context, "function_version", None) or os.environ.get("AWS_LAMBDA_FUNCTION_VERSION"),
        "cloud.provider": "aws",
        "cloud.platform": "aws_lambda",
        "cloud.region": os.environ.get("AWS_REGION"),
        "cloud.resource_id": arn,
        "cloud.account.id": arn.split(":")[4] if arn and arn.count(":") >= 5 else None,
    }
    if not http:
        source = _lambda_source(event)
        if source:
            base["faas.event_source"] = source
        return Invocation(function_name or "lambda", base, mechanism="aws-lambda")
    headers = event.get("headers") or event.get("multiValueHeaders")
    method = http_v2.get("method") if http_v2 else event.get("httpMethod")
    path = (http_v2.get("path") or event.get("rawPath")) if http_v2 else event.get("path")
    route_key = request_context.get("routeKey")
    route = route_key.split(" ")[-1] if isinstance(route_key, str) and route_key != "$default" else None
    if route is None:
        resource = event.get("resource") or request_context.get("resourcePath")
        route = resource if isinstance(resource, str) and "{proxy+}" not in resource else None
    identity = request_context.get("identity") if isinstance(request_context.get("identity"), dict) else {}
    base.update(
        {
            "http.request.method": method,
            "url.path": path,
            "http.route": route,
            "server.address": header(headers, "host") or request_context.get("domainName"),
            "user_agent.original": (http_v2 or {}).get("userAgent") or header(headers, "user-agent"),
            "client.address": (http_v2 or {}).get("sourceIp") or identity.get("sourceIp"),
        }
    )
    return Invocation("{} {}".format(method, route or path or "/"), base, carrier=headers, http=True, mechanism="aws-lambda")


def lambda_status(result: Any, event: Any) -> Optional[int]:
    """The response status a Lambda HTTP result stands for (API Gateway's own rules)."""
    if isinstance(result, dict) and isinstance(result.get("statusCode"), int):
        return result["statusCode"]
    # HTTP API (payload 2.0): anything without a statusCode is a 200 with that as the body.
    if isinstance(event, dict) and isinstance(event.get("requestContext"), dict) and "http" in event["requestContext"]:
        return 200
    return None


def lambda_budget(context: Any, limit: float) -> float:
    """``limit``, kept inside the invocation's remaining time."""
    try:
        left = float(context.get_remaining_time_in_millis()) / 1000.0
        return max(0.0, min(limit, left - 0.1))
    except Exception:
        return limit


# ─── Google Cloud Functions ─────────────────────────────────────────────────────


def _gcf_name() -> Optional[str]:
    return os.environ.get("K_SERVICE") or os.environ.get("FUNCTION_TARGET") or os.environ.get("FUNCTION_NAME")


def _gcf_base() -> Dict[str, Any]:
    return {
        "faas.coldstart": take_coldstart(),
        "faas.name": _gcf_name(),
        "faas.version": os.environ.get("K_REVISION"),
        "cloud.provider": "gcp",
        "cloud.platform": "gcp_cloud_functions",
        "cloud.region": os.environ.get("FUNCTION_REGION"),
    }


def is_http_request(value: Any) -> bool:
    return hasattr(value, "headers") and isinstance(getattr(value, "method", None), str) and hasattr(value, "path")


def gcf_execution_id(request: Any) -> str:
    # Gen 1 sends the execution id; gen 2 (Cloud Run) has only the trace header.
    trace_header = header(request.headers, "x-cloud-trace-context")
    return header(request.headers, "function-execution-id") or (trace_header.split("/")[0] if trace_header else None) or uuid.uuid4().hex


def gcf_http_attributes(request: Any) -> Dict[str, Any]:
    attributes = _gcf_base()
    attributes.update({"faas.trigger": "http", "faas.invocation_id": gcf_execution_id(request)})
    return attributes


def gcf_http_invocation(request: Any) -> Invocation:
    method = request.method
    path = getattr(request, "path", None) or "/"
    attributes = gcf_http_attributes(request)
    attributes.update(
        {
            "http.request.method": method,
            "url.path": path,
            "server.address": header(request.headers, "host"),
            "user_agent.original": header(request.headers, "user-agent"),
        }
    )
    return Invocation("{} {}".format(method, path), attributes, carrier=request.headers, http=True, mechanism="gcp-functions")


def _cloud_event_attribute(event: Any, name: str) -> Any:
    try:
        if isinstance(event, dict):
            return event.get(name)
        getter = getattr(event, "get", None)
        if getter is not None:
            return getter(name)
        return event[name]
    except Exception:
        return getattr(event, name, None)


def gcf_event_invocation(args: tuple) -> Invocation:
    """A CloudEvent (``functions_framework.cloud_event``) or a background event ``(data, context)``."""
    first = args[0] if args else None
    is_cloud_event = len(args) == 1 and _cloud_event_attribute(first, "specversion") is not None
    if is_cloud_event:
        kind = _cloud_event_attribute(first, "type") or ""
        event_id = _cloud_event_attribute(first, "id")
        source = _cloud_event_attribute(first, "source")
        # The CloudEvents distributed tracing extension.
        carrier = {k: v for k in ("traceparent", "tracestate") for v in [_cloud_event_attribute(first, k)] if v}
    else:
        context = args[1] if len(args) > 1 else None
        kind = getattr(context, "event_type", None) or (context.get("eventType") if isinstance(context, dict) else "") or ""
        event_id = getattr(context, "event_id", None) or (context.get("eventId") if isinstance(context, dict) else None)
        source, carrier = None, None
    lowered = str(kind).lower()
    trigger = (
        "pubsub" if "pubsub" in lowered
        else "datasource" if any(word in lowered for word in ("storage", "firestore", "firebase", "datastore", "database"))
        else "timer" if "scheduler" in lowered
        else "other"
    )
    attributes = _gcf_base()
    attributes.update(
        {
            "faas.trigger": trigger,
            "faas.invocation_id": event_id or uuid.uuid4().hex,
            "cloudevents.event_type": kind or None,
            "cloudevents.event_source": source,
        }
    )
    return Invocation(_gcf_name() or "function", attributes, carrier=carrier or None, mechanism="gcp-functions")


def flask_status(result: Any) -> int:
    """The status a Flask view's return value stands for."""
    status = getattr(result, "status_code", None)
    if isinstance(status, int):
        return status
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], int):
        return result[1]
    return 200


# ─── resource ───────────────────────────────────────────────────────────────────


def resource_attributes() -> Dict[str, Any]:
    """The platform's resource attributes, read from the environment each one sets."""
    env = os.environ
    if env.get("AWS_LAMBDA_FUNCTION_NAME"):
        try:
            memory = int(env.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE") or 0) * 1024 * 1024
        except ValueError:
            memory = 0
        return _clean(
            {
                "cloud.provider": "aws",
                "cloud.platform": "aws_lambda",
                "cloud.region": env.get("AWS_REGION"),
                "faas.name": env.get("AWS_LAMBDA_FUNCTION_NAME"),
                "faas.version": env.get("AWS_LAMBDA_FUNCTION_VERSION"),
                "faas.instance": env.get("AWS_LAMBDA_LOG_STREAM_NAME"),
                "faas.max_memory": memory or None,
            }
        )
    if env.get("FUNCTIONS_WORKER_RUNTIME") and (env.get("WEBSITE_SITE_NAME") or env.get("FUNCTIONS_EXTENSION_VERSION")):
        return _clean({"cloud.provider": "azure", "cloud.platform": "azure_functions", "cloud.region": env.get("REGION_NAME")})
    if env.get("FUNCTION_TARGET") and (env.get("K_SERVICE") or env.get("FUNCTION_NAME")):
        return _clean(
            {
                "cloud.provider": "gcp",
                "cloud.platform": "gcp_cloud_functions",
                "cloud.region": env.get("FUNCTION_REGION"),
                "faas.name": _gcf_name(),
                "faas.version": env.get("K_REVISION"),
            }
        )
    return {}


def function_name() -> Optional[str]:
    """The function's own name, for ``service.name`` when nothing else gives one."""
    env = os.environ
    return env.get("AWS_LAMBDA_FUNCTION_NAME") or env.get("WEBSITE_SITE_NAME") or (_gcf_name() if env.get("FUNCTION_TARGET") else None)


# ─── running an invocation ──────────────────────────────────────────────────────


def _zharp():
    return sys.modules.get("zharp")


def _failed(span: Any, invocation: Invocation, error: BaseException) -> None:
    """Report what the handler raised, in its invocation, and fail the span. Once per exception."""
    if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        return
    try:
        zharp = _zharp()
        if zharp is not None:
            zharp._report(error, handled=False, attributes={"zharp.mechanism": invocation.mechanism})
        if span is not None and span.is_recording():
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.set_attribute("error.type", type(error).__name__)
    except Exception:
        pass


class _Run:
    """One invocation's span (none with tracing off) in a context of its own."""

    def __init__(self, invocation: Invocation):
        self.invocation = invocation
        self.span = None
        self.tokens = []
        zharp = _zharp()
        state = getattr(zharp, "_state", None)
        ctx = otel_context.get_current()
        if invocation.carrier:
            ctx = propagate.extract(invocation.carrier, context=ctx, getter=GETTER)
        # Logs only: no span — the context still carries the caller's trace id for the logs.
        if state is not None and state.tracer_provider is not None:
            self.span = state.tracer.start_span(invocation.name, context=ctx, kind=SpanKind.SERVER, attributes=invocation.attributes)
            ctx = trace.set_span_in_context(self.span, ctx)
        self.tokens.append(otel_context.attach(ctx))
        self.inside = _inside.set(True)

    def failed(self, error: BaseException) -> None:
        _failed(self.span, self.invocation, error)

    def end(self, status: Optional[int]) -> None:
        try:
            if self.span is not None:
                if status is not None:
                    self.span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        self.span.set_status(Status(StatusCode.ERROR))
                self.span.end()
        except Exception:
            pass
        _inside.reset(self.inside)
        for token in reversed(self.tokens):
            try:
                otel_context.detach(token)
            except Exception:
                pass


def _flush(timeout: float) -> None:
    zharp = _zharp()
    if zharp is not None and timeout > 0:
        try:
            zharp.flush(timeout)
        except Exception:
            pass


def active() -> bool:
    zharp = _zharp()
    return getattr(zharp, "_state", None) is not None and not _inside.get()


def run(invocation: Callable[[], Invocation], call: Callable[[], Any], status_of: Callable[[Any], Optional[int]], timeout: Callable[[], float]) -> Any:
    """``call()`` as one invocation; its result or exception comes back unchanged, after the flush."""
    if not active():
        return call()
    current = _Run(invocation())
    status = None
    try:
        result = call()
        try:
            status = status_of(result)
        except Exception:
            status = None
        return result
    except BaseException as error:
        current.failed(error)
        raise
    finally:
        current.end(status)
        _flush(timeout())


async def run_async(invocation: Callable[[], Invocation], call: Callable[[], Any], status_of: Callable[[Any], Optional[int]], timeout: Callable[[], float]) -> Any:
    if not active():
        return await call()
    import asyncio

    current = _Run(invocation())
    status = None
    try:
        result = await call()
        try:
            status = status_of(result)
        except Exception:
            status = None
        return result
    except BaseException as error:
        current.failed(error)
        raise
    finally:
        current.end(status)
        await asyncio.get_running_loop().run_in_executor(None, _flush, timeout())


# ─── public wrappers ────────────────────────────────────────────────────────────


def _decorator(make: Callable[[Callable, float], Callable], fn: Optional[Callable], flush_timeout: Optional[float]):
    limit = DEFAULT_FLUSH_TIMEOUT if flush_timeout is None else float(flush_timeout)
    if fn is None:
        return lambda f: make(f, limit)
    return make(fn, limit)


def aws_lambda(fn: Optional[Callable] = None, *, flush_timeout: Optional[float] = None):
    def make(handler: Callable, limit: float) -> Callable:
        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def async_wrapper(event, context, *args, **kwargs):
                return await run_async(
                    lambda: lambda_invocation(event, context),
                    lambda: handler(event, context, *args, **kwargs),
                    lambda result: lambda_status(result, event) if _is_http_event(event) else None,
                    lambda: lambda_budget(context, limit),
                )

            return async_wrapper

        @functools.wraps(handler)
        def wrapper(event, context, *args, **kwargs):
            return run(
                lambda: lambda_invocation(event, context),
                lambda: handler(event, context, *args, **kwargs),
                lambda result: lambda_status(result, event) if _is_http_event(event) else None,
                lambda: lambda_budget(context, limit),
            )

        return wrapper

    return _decorator(make, fn, flush_timeout)


def _is_http_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    request_context = event.get("requestContext")
    return isinstance(event.get("httpMethod"), str) or (isinstance(request_context, dict) and isinstance(request_context.get("http"), dict))


#: Where a Cloud Function's Flask request asks for a flush once Flask has ended its span.
FLUSH_ENVIRON = "zharp.flush_timeout"


def cloud_function(fn: Optional[Callable] = None, *, flush_timeout: Optional[float] = None):
    def make(handler: Callable, limit: float) -> Callable:
        _flush_after_flask_requests()

        def through_flask(request) -> bool:
            """Inside the Functions Framework's Flask request, which the Flask instrumentation
            already made the span of: that span is the invocation's."""
            environ = getattr(request, "environ", None)
            return isinstance(environ, dict) and getattr(_zharp(), "_state", None) is not None and trace.get_current_span().is_recording()

        def annotate(request, attributes: Dict[str, Any]) -> Any:
            span = trace.get_current_span()
            try:
                span.set_attributes(_clean(attributes))
                # Flushed by Flask.__call__, once Flask has ended the span and before the body goes out.
                if request is not None:
                    request.environ[FLUSH_ENVIRON] = limit
            except Exception:
                pass
            return span

        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def async_wrapper(*args, **kwargs):
                if args and is_http_request(args[0]):
                    request = args[0]
                    return await run_async(lambda: gcf_http_invocation(request), lambda: handler(*args, **kwargs), flask_status, lambda: limit)
                return await run_async(lambda: gcf_event_invocation(args), lambda: handler(*args, **kwargs), lambda _: None, lambda: limit)

            return async_wrapper

        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            if args and is_http_request(args[0]):
                request = args[0]
                if active() and through_flask(request):
                    span = annotate(request, gcf_http_attributes(request))
                    try:
                        # The Functions Framework routes everything through "/<path:path>".
                        span.update_name("{} {}".format(request.method, request.path or "/"))
                    except Exception:
                        pass
                    try:
                        return handler(*args, **kwargs)
                    except BaseException as error:
                        _failed(span, Invocation("", {}, mechanism="gcp-functions"), error)
                        raise
                return run(lambda: gcf_http_invocation(request), lambda: handler(*args, **kwargs), flask_status, lambda: limit)
            if active() and trace.get_current_span().is_recording():
                # A CloudEvent delivered through the Flask app: its request span is the invocation's.
                span = annotate(_flask_request(), gcf_event_invocation(args).attributes)
                try:
                    return handler(*args, **kwargs)
                except BaseException as error:
                    _failed(span, Invocation("", {}, mechanism="gcp-functions"), error)
                    raise
            return run(lambda: gcf_event_invocation(args), lambda: handler(*args, **kwargs), lambda _: None, lambda: limit)

        return wrapper

    return _decorator(make, fn, flush_timeout)


def _flask_request():
    try:
        import flask

        return flask.request._get_current_object() if flask.has_request_context() else None
    except Exception:
        return None


def _flush_after_flask_requests() -> None:
    """Flask ends the request's span as the request context is popped, inside ``wsgi_app`` — so
    once ``Flask.__call__`` returns the span is done, and the WSGI server hasn't sent the body yet.
    Flush there when a Cloud Function asked for it."""
    try:
        import flask
    except ImportError:
        return
    original = flask.Flask.__call__
    if getattr(original, "_zharp", False):
        return

    def __call__(self, environ, start_response):
        response = original(self, environ, start_response)
        timeout = environ.get(FLUSH_ENVIRON) if isinstance(environ, dict) else None
        if timeout:
            _flush(timeout)
        return response

    __call__._zharp = True  # type: ignore[attr-defined]
    __call__.__wrapped__ = original  # type: ignore[attr-defined]
    flask.Flask.__call__ = __call__


# ─── Chalice ────────────────────────────────────────────────────────────────────


def chalice_hook(tracer_provider: Any = None) -> None:
    """Chalice apps run on Lambda: the REST API (``Chalice.__call__``), event sources (SQS, SNS, S3,
    schedules, websockets) and ``@app.lambda_function`` each become one invocation, flushed before
    returning. A view's unhandled exception — which Chalice turns into a 500 — is reported once."""
    from chalice import app as chalice_app

    def wrap(cls: type, attribute: str = "__call__") -> None:
        original = getattr(cls, attribute)
        if getattr(original, "_zharp", False):
            return

        def __call__(self, event, context):
            return run(
                lambda: lambda_invocation(event, context),
                lambda: original(self, event, context),
                lambda result: lambda_status(result, event) if _is_http_event(event) else None,
                lambda: lambda_budget(context, DEFAULT_FLUSH_TIMEOUT),
            )

        __call__._zharp = True  # type: ignore[attr-defined]
        __call__.__wrapped__ = original  # type: ignore[attr-defined]
        setattr(cls, attribute, __call__)

    wrap(chalice_app.Chalice)
    wrap(chalice_app.EventSourceHandler)
    wrap(chalice_app.LambdaFunction)

    handler_class = getattr(chalice_app, "RestAPIEventHandler", None)
    unhandled = getattr(handler_class, "_unhandled_exception_to_response", None)
    if unhandled is not None and not getattr(unhandled, "_zharp", False):

        def _unhandled_exception_to_response(self, *args, **kwargs):
            error = sys.exc_info()[1]
            if error is not None:
                _failed(trace.get_current_span(), Invocation("", {}, mechanism="chalice"), error)
            return unhandled(self, *args, **kwargs)

        _unhandled_exception_to_response._zharp = True  # type: ignore[attr-defined]
        handler_class._unhandled_exception_to_response = _unhandled_exception_to_response
