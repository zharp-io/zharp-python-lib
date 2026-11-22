"""Run by test_serverless.py in its own process, once per mode (OTel's global providers are set once
per process): zharp.init() at import, as at a cold start, with in-memory exporters behind the real
batch processors — so a span or log is only there right after an invocation if the wrapper flushed
it. Then invocations as the platform makes them, and what was exported, as JSON.

    python tests/serverless_app.py lambda|lambda-logs-only|slow|chalice|gcf-http|gcf-event
"""

import json
import os
import sys
import time

for name in ("ZHARP_KEY", "ZHARP_API_KEY", "ZHARP_SERVICE", "ZHARP_ENVIRONMENT", "ZHARP_DISABLED", "ZHARP_TRACING"):
    os.environ.pop(name, None)

MODE = sys.argv[1]
TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"
TRACEPARENT = "00-{}-{}-01".format(TRACE, PARENT)

if MODE in ("lambda", "lambda-logs-only", "slow", "chalice"):
    os.environ.update({"AWS_LAMBDA_FUNCTION_NAME": "orders-fn", "AWS_REGION": "eu-west-1", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "512"})
if MODE.startswith("gcf"):
    os.environ.update({"FUNCTION_TARGET": "orders", "K_SERVICE": "orders", "K_REVISION": "orders-00002"})

from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogs
except ImportError:
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as InMemoryLogs


class SlowSpans(InMemorySpanExporter):
    """An endpoint that takes 5 s to answer."""

    def export(self, spans):
        time.sleep(5)
        return SpanExportResult.SUCCESS


SPANS = SlowSpans() if MODE == "slow" else InMemorySpanExporter()
LOGS = InMemoryLogs()

import zharp  # noqa: E402

assert zharp.init(
    key="zpk_test",
    service=None,
    tracing=MODE != "lambda-logs-only",
    quiet=True,
    capture_source_context=False,
    _exporters=(SPANS, LOGS),
)


def records():
    out = []
    for item in LOGS.get_finished_logs():
        record = getattr(item, "log_record", item)
        attributes = dict(record.attributes or {})
        out.append(
            {
                "body": str(record.body),
                "trace": "{:032x}".format(record.trace_id) if record.trace_id else None,
                "span": "{:016x}".format(record.span_id) if record.span_id else None,
                "exception": attributes.get("exception.message"),
                "handled": attributes.get("zharp.handled"),
                "mechanism": attributes.get("zharp.mechanism"),
            }
        )
    return out


def spans():
    out = []
    for span in SPANS.get_finished_spans():
        attributes = {k: (list(v) if isinstance(v, tuple) else v) for k, v in (span.attributes or {}).items()}
        out.append(
            {
                "name": span.name,
                "kind": span.kind.name,
                "trace": "{:032x}".format(span.context.trace_id),
                "span": "{:016x}".format(span.context.span_id),
                "parent": "{:016x}".format(span.parent.span_id) if span.parent else None,
                "status": span.status.status_code.name,
                "attributes": attributes,
                "resource": {k: v for k, v in span.resource.attributes.items()},
            }
        )
    return out


steps = []


def note(label, **extra):
    """What had been exported when the invocation returned."""
    steps.append(
        dict(
            extra,
            label=label,
            invocations=[s["attributes"].get("faas.invocation_id") for s in spans()],
            logs=[r["body"] for r in records()],
        )
    )


class Context:
    def __init__(self, request_id, remaining=30_000):
        self.aws_request_id = request_id
        self.function_name = "orders-fn"
        self.function_version = "$LATEST"
        self.invoked_function_arn = "arn:aws:lambda:eu-west-1:123456789012:function:orders-fn"
        self._remaining = remaining

    def get_remaining_time_in_millis(self):
        return self._remaining


API_V2 = {
    "version": "2.0",
    "routeKey": "GET /items/{id}",
    "rawPath": "/items/42",
    "headers": {"traceparent": TRACEPARENT, "user-agent": "curl/8"},
    "requestContext": {"http": {"method": "GET", "path": "/items/42", "sourceIp": "203.0.113.9", "userAgent": "curl/8"}, "routeKey": "GET /items/{id}"},
}
SQS = {"Records": [{"eventSource": "aws:sqs", "body": "{}"}]}
result = {}

if MODE in ("lambda", "lambda-logs-only"):

    @zharp.aws_lambda
    def handler(event, context):
        """The handler's own docstring."""
        zharp.log.info("lambda ran " + context.aws_request_id)
        if event.get("fail"):
            raise TypeError("lambda boom")
        return {"batchItemFailures": []} if "Records" in event else {"statusCode": 201, "body": '{"ok": true}'}

    import inspect

    result["name"] = handler.__name__
    result["doc"] = handler.__doc__
    result["signature"] = str(inspect.signature(handler))
    result["api"] = handler(API_V2, Context("req-api"))
    note("api")
    result["sqs"] = handler(SQS, Context("req-sqs"))
    note("sqs")
    try:
        handler(dict(SQS, fail=True), Context("req-fail"))
    except TypeError as error:
        result["raised"] = [type(error).__name__, str(error)]
    note("fail")

elif MODE == "slow":
    fast = zharp.aws_lambda(flush_timeout=0.3)(lambda event, context: "done")
    plain = zharp.aws_lambda(lambda event, context: "done")
    for label, fn, context in (("custom", fast, Context("slow-1")), ("default", plain, Context("slow-2")), ("near-timeout", plain, Context("slow-3", remaining=400))):
        start = time.monotonic()
        value = fn({}, context)
        result[label] = {"value": value, "seconds": round(time.monotonic() - start, 3)}

elif MODE == "chalice":
    from chalice import Chalice, NotFoundError

    app = Chalice(app_name="orders")

    @app.route("/items/{id}")
    def item(id):
        zharp.log.info("chalice route " + id)
        if id == "missing":
            raise NotFoundError("no such item")
        if id == "boom":
            raise ValueError("chalice boom")
        return {"id": id}

    @app.on_sqs_message(queue="orders")
    def on_message(event):
        zharp.log.info("chalice sqs")
        for record in event:
            if record.body == "fail":
                raise RuntimeError("chalice sqs boom")

    def rest(path_id, request_id):
        return {
            "resource": "/items/{id}",
            "path": "/items/" + path_id,
            "httpMethod": "GET",
            "headers": {"Traceparent": TRACEPARENT, "Host": "abc.execute-api.eu-west-1.amazonaws.com"},
            "multiValueHeaders": {},
            "queryStringParameters": None,
            "multiValueQueryStringParameters": None,
            "pathParameters": {"id": path_id},
            "stageVariables": None,
            "body": None,
            "isBase64Encoded": False,
            "requestContext": {"resourcePath": "/items/{id}", "httpMethod": "GET", "requestId": request_id, "identity": {"sourceIp": "203.0.113.9"}},
        }

    responses = []
    for path_id, request_id in (("7", "ch-ok"), ("missing", "ch-404"), ("boom", "ch-500")):
        responses.append(app(rest(path_id, request_id), Context(request_id))["statusCode"])
        note(request_id)
    result["codes"] = responses
    on_message({"Records": [{"eventSource": "aws:sqs", "body": "hi", "receiptHandle": "r", "messageId": "m", "attributes": {}, "messageAttributes": {}, "md5OfBody": "", "eventSourceARN": "arn:aws:sqs:eu-west-1:1:orders", "awsRegion": "eu-west-1"}]}, Context("ch-sqs"))
    note("ch-sqs")
    try:
        on_message({"Records": [{"eventSource": "aws:sqs", "body": "fail", "receiptHandle": "r", "messageId": "m2", "attributes": {}, "messageAttributes": {}, "md5OfBody": "", "eventSourceARN": "arn:aws:sqs:eu-west-1:1:orders", "awsRegion": "eu-west-1"}]}, Context("ch-sqs-fail"))
    except RuntimeError as error:
        result["raised"] = str(error)
    note("ch-sqs-fail")

elif MODE in ("gcf-http", "gcf-event"):
    import functions_framework

    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serverless_gcf.py")
    target = "orders" if MODE == "gcf-http" else "on_message"
    client = functions_framework.create_app(target, source, "http" if MODE == "gcf-http" else "cloudevent").test_client()
    if MODE == "gcf-http":
        codes = []
        for path, execution in (("/orders?x=1", "gcf-exec-1"), ("/fail", "gcf-exec-2"), ("/orders", "gcf-exec-3")):
            response = client.get(path, headers={"traceparent": TRACEPARENT, "Function-Execution-Id": execution})
            codes.append(response.status_code)
            note(execution)
        result["codes"] = codes
    else:
        codes = []
        for event_id in ("ce-1", "ce-2"):
            response = client.post(
                "/",
                headers={
                    "ce-specversion": "1.0",
                    "ce-type": "google.cloud.pubsub.topic.v1.messagePublished",
                    "ce-source": "//pubsub.googleapis.com/projects/p/topics/t",
                    "ce-id": event_id,
                    "traceparent": TRACEPARENT,
                    "Content-Type": "application/json",
                },
                data=json.dumps({"message": {"data": ""}}),
            )
            codes.append(response.status_code)
            note(event_id)
        result["codes"] = codes

print(json.dumps({"result": result, "steps": steps, "spans": spans(), "logs": records()}))
sys.stdout.flush()
os._exit(0)  # the slow exporter may still be "sending"
