"""Unit tests: `python -m unittest discover -s tests` (needs flask, fastapi and httpx installed).

zharp.init() sets OpenTelemetry's global providers, which can be set once per process, so it runs
once here with in-memory exporters and every test reads what they caught.
"""

import logging
import os
import unittest

for name in ("ZHARP_KEY", "ZHARP_API_KEY", "ZHARP_SERVICE", "ZHARP_ENVIRONMENT", "ZHARP_DISABLED"):
    os.environ.pop(name, None)

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogs
except ImportError:  # older SDKs
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as InMemoryLogs

import zharp

SPANS = InMemorySpanExporter()
LOGS = InMemoryLogs()

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
HEADERS = {
    "traceparent": "00-{}-{}-01".format(TRACE_ID, PARENT_ID),
    "baggage": "enduser.id=test-user,session.id=test-session",
}


def setUpModule():
    started = zharp.init(
        key="zpk_test",
        service="unit-test-api",
        environment="test",
        release="api@1.2.3",
        quiet=True,
        _exporters=(SPANS, LOGS),
        _batch=False,
    )
    assert started


def spans():
    return list(SPANS.get_finished_spans())


def logs():
    return [getattr(item, "log_record", item) for item in LOGS.get_finished_logs()]


def hex_trace(value):
    return format(value, "032x")


def error_records():
    """What the server turns into error events: span 'exception' events and logs with exception.*."""
    found = []
    for span in spans():
        for event in span.events:
            if event.name == "exception":
                found.append(dict(event.attributes))
    for record in logs():
        attrs = dict(record.attributes or {})
        if any(key.startswith("exception.") for key in attrs):
            found.append(attrs)
    return found


def flask_app():
    import flask

    app = flask.Flask("unit_test_flask")

    @app.route("/hello/<name>")
    def hello(name):
        logging.getLogger("shop").warning("hello %s", name)
        zharp.log.info("from zharp.log", {"order_id": 7})
        return "hi " + name

    @app.route("/boom")
    def boom():
        raise ValueError("boom")

    @app.route("/caught")
    def caught():
        try:
            raise KeyError("missing")
        except KeyError as error:
            zharp.capture_error(error, {"order_id": 9})
        return "ok"

    @app.route("/logged")
    def logged():
        try:
            raise RuntimeError("logged it")
        except RuntimeError:
            logging.getLogger("shop").exception("could not do it")
        return "ok"

    @app.route("/who")
    def who():
        zharp.set_user(id="server-user", email="a@example.com")
        logging.getLogger("shop").warning("after set_user")
        return "ok"

    return app


class ZharpTest(unittest.TestCase):
    def setUp(self):
        SPANS.clear()
        LOGS.clear()

    def test_resource(self):
        zharp.log.info("resource check")
        resource = LOGS.get_finished_logs()[0].resource.attributes
        self.assertEqual(resource["service.name"], "unit-test-api")
        self.assertEqual(resource["deployment.environment.name"], "test")
        self.assertEqual(resource["service.version"], "api@1.2.3")
        self.assertEqual(resource["telemetry.sdk.name"], "zharp")
        self.assertEqual(resource["telemetry.sdk.language"], "python")
        self.assertEqual(resource["telemetry.sdk.version"], zharp.__version__)

    def test_request_continues_trace_and_copies_baggage(self):
        client = flask_app().test_client()
        response = client.get("/hello/ann", headers=HEADERS)
        self.assertEqual(response.status_code, 200)

        server = [s for s in spans() if s.kind.name == "SERVER"]
        self.assertEqual(len(server), 1)
        span = server[0]
        self.assertEqual(hex_trace(span.context.trace_id), TRACE_ID)
        self.assertEqual(format(span.parent.span_id, "016x"), PARENT_ID)
        self.assertEqual(span.attributes["enduser.id"], "test-user")
        self.assertEqual(span.attributes["session.id"], "test-session")
        self.assertEqual(span.attributes["http.route"], "/hello/<name>")

        bodies = {record.body: record for record in logs()}
        for body in ("hello ann", "from zharp.log"):
            record = bodies[body]
            self.assertEqual(hex_trace(record.trace_id), TRACE_ID)
            self.assertEqual(record.attributes["enduser.id"], "test-user")
            self.assertEqual(record.attributes["session.id"], "test-session")
        self.assertEqual(bodies["from zharp.log"].attributes["order_id"], 7)

    def test_uncaught_error_reported_once_unhandled(self):
        client = flask_app().test_client()
        response = client.get("/boom", headers=HEADERS)
        self.assertEqual(response.status_code, 500)

        errors = error_records()
        self.assertEqual(len(errors), 1, errors)
        error = errors[0]
        self.assertIn("ValueError", error["exception.type"])
        self.assertEqual(error["exception.message"], "boom")
        self.assertEqual(error["zharp.handled"], "false")
        self.assertTrue(error["exception.stacktrace"].startswith("Traceback (most recent call last)"))

        server = [s for s in spans() if s.kind.name == "SERVER"][0]
        self.assertEqual(server.status.status_code.name, "ERROR")
        # The framework's log line stays, with the trace.
        self.assertTrue(any(hex_trace(r.trace_id) == TRACE_ID for r in logs()))

    def test_capture_error_is_handled(self):
        client = flask_app().test_client()
        client.get("/caught", headers=HEADERS)
        errors = error_records()
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0]["exception.type"], "KeyError")
        self.assertEqual(errors[0]["zharp.handled"], "true")
        self.assertEqual(errors[0]["order_id"], 9)
        record = [r for r in logs() if "exception.type" in (r.attributes or {})][0]
        self.assertEqual(hex_trace(record.trace_id), TRACE_ID)
        self.assertEqual(record.attributes["session.id"], "test-session")

    def test_logged_exception_is_handled_error(self):
        flask_app().test_client().get("/logged")
        errors = error_records()
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0]["exception.type"], "RuntimeError")
        self.assertEqual(errors[0]["exception.message"], "logged it")
        self.assertEqual(errors[0]["zharp.handled"], "true")
        self.assertNotIn("zharp.exception_type", errors[0])

    def test_set_user_overrides_baggage_for_the_request_only(self):
        client = flask_app().test_client()
        client.get("/who", headers=HEADERS)
        record = [r for r in logs() if r.body == "after set_user"][0]
        self.assertEqual(record.attributes["enduser.id"], "server-user")
        self.assertEqual(record.attributes["enduser.email"], "a@example.com")
        self.assertEqual(record.attributes["session.id"], "test-session")

        LOGS.clear()
        client.get("/hello/bob")
        record = [r for r in logs() if r.body == "hello bob"][0]
        self.assertNotIn("enduser.id", record.attributes)

    def test_fastapi(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/items/{item_id}")
        def item(item_id: int):
            logging.getLogger("shop").warning("item %s", item_id)
            return {"id": item_id}

        @app.get("/fail")
        def fail():
            raise ZeroDivisionError("nope")

        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.get("/items/3", headers=HEADERS).status_code, 200)
        server = [s for s in spans() if s.kind.name == "SERVER"]
        self.assertEqual(len(server), 1)
        self.assertEqual(hex_trace(server[0].context.trace_id), TRACE_ID)
        self.assertEqual(server[0].attributes["http.route"], "/items/{item_id}")
        self.assertEqual(server[0].attributes["enduser.id"], "test-user")
        self.assertFalse([s.name for s in spans() if s.name.endswith((" http send", " http receive"))])
        record = [r for r in logs() if r.body == "item 3"][0]
        self.assertEqual(hex_trace(record.trace_id), TRACE_ID)
        self.assertEqual(record.attributes["session.id"], "test-session")

        SPANS.clear()
        LOGS.clear()
        self.assertEqual(client.get("/fail", headers=HEADERS).status_code, 500)
        errors = error_records()
        self.assertEqual(len(errors), 1, errors)
        self.assertEqual(errors[0]["exception.type"], "ZeroDivisionError")
        self.assertEqual(errors[0]["zharp.handled"], "false")

    def test_start_span_and_tags(self):
        zharp.set_tag("plan", "pro")
        try:
            with zharp.start_span("render invoice", attributes={"invoice": 4}):
                zharp.log.debug("inside")
            self.assertEqual(zharp.start_span("compute", lambda: 42), 42)
        finally:
            zharp.set_tag("plan", None)
        named = {s.name: s for s in spans()}
        self.assertEqual(named["render invoice"].attributes["plan"], "pro")
        self.assertEqual(named["render invoice"].attributes["invoice"], 4)
        record = logs()[0]
        self.assertEqual(record.trace_id, named["render invoice"].context.trace_id)
        self.assertEqual(record.attributes["plan"], "pro")

    def test_log_levels(self):
        zharp.log.warn("w")
        zharp.log.error("e")
        zharp.log.fatal("f")
        self.assertEqual([r.severity_number.value for r in logs()], [13, 17, 21])

    def test_flush(self):
        self.assertTrue(zharp.flush(1.0))


if __name__ == "__main__":
    unittest.main()
