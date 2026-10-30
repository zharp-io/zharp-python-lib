"""Run by test_logs_only.py in its own process (OTel's global providers are set once per process):
init(tracing=False) with in-memory exporters, a Flask app that logs, fails, and calls a local HTTP
server; prints what was exported and which headers the outgoing call carried, as JSON."""

import http.server
import json
import logging
import os
import sys
import threading

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogs
except ImportError:
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as InMemoryLogs

import zharp

SPANS = InMemorySpanExporter()
LOGS = InMemoryLogs()
SEEN = []


class Downstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def main():
    explicit = os.environ.get("ZHARP_TRACING") is None
    started = zharp.init(
        key="zpk_test",
        service="logs-only-test",
        quiet=True,
        _exporters=(SPANS, LOGS),
        _batch=False,
        **({"tracing": False} if explicit else {}),
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), Downstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:{}/".format(server.server_port)

    import flask
    import requests

    app = flask.Flask("logs_only_app")

    @app.route("/hello")
    def hello():
        logging.getLogger("shop").warning("hello")
        zharp.log.info("from zharp.log", {"order_id": 7})
        requests.get(url, timeout=5)
        return "ok"

    @app.route("/boom")
    def boom():
        raise ValueError("boom")

    @app.route("/caught")
    def caught():
        try:
            raise KeyError("missing")
        except KeyError as error:
            zharp.capture_error(error)
        return "ok"

    headers = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "baggage": "enduser.id=test-user,session.id=test-session",
    }
    client = app.test_client()
    zharp.set_tag("plan", "pro")
    statuses = [client.get(path, headers=headers).status_code for path in ("/hello", "/boom", "/caught")]
    span_result = zharp.start_span("work", lambda: 42)
    with zharp.start_span("block") as span:
        span.set_attribute("ignored", 1)
    zharp.flush(1.0)
    server.shutdown()

    logs = []
    for item in LOGS.get_finished_logs():
        record = getattr(item, "log_record", item)
        logs.append(
            {
                "body": str(record.body),
                "trace_id": format(record.trace_id or 0, "032x"),
                "attributes": {k: v for k, v in (record.attributes or {}).items() if isinstance(v, (str, int))},
            }
        )
    json.dump(
        {
            "started": started,
            "statuses": statuses,
            "span_result": span_result,
            "spans": [s.name for s in SPANS.get_finished_spans()],
            "logs": logs,
            "downstream_headers": SEEN,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
