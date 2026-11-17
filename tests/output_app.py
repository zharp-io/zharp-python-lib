"""Run by test_output.py in its own process (OTel's global providers are set once per process, and
the output capture replaces sys.stdout/sys.stderr): init() with in-memory exporters, then a Flask
request that logs every way an app does — print(), raw writes, JSON lines, logging (propagating or
not), loguru, structlog — and lines outside a request. Writes what was exported, as JSON, to the
file named by argv[1]."""

import contextvars
import json
import logging
import os
import sys
import threading
import time
import traceback

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogs
except ImportError:
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as InMemoryLogs

import zharp

SPANS = InMemorySpanExporter()
LOGS = InMemoryLogs()
ESC = "\x1b"


def main(out_path):
    # Each switch as an argument when the test asks for one (else the ZHARP_* environment decides).
    options = {}
    for name in ("capture_output", "logs", "log_correlation"):
        value = os.environ.get("ARG_" + name.upper())
        if value is not None:
            options[name] = value == "true"
    zharp.init(key="zpk_test", service="output-test", quiet=True, _exporters=(SPANS, LOGS), _batch=False, **options)

    import flask
    import structlog
    from loguru import logger as loguru_logger

    # The app sets up its own handlers after init, the usual way.
    shop = logging.getLogger("shop")
    shop.addHandler(logging.StreamHandler(sys.stdout))
    # uvicorn / gunicorn style: a logger that doesn't propagate to the root.
    access = logging.getLogger("app.access")
    access.propagate = False
    access.addHandler(logging.StreamHandler(sys.stdout))
    # logging routed into loguru (the InterceptHandler recipe): sent once, by the logging bridge.

    class Intercept(logging.Handler):
        def emit(self, record):
            loguru_logger.opt(depth=1).log(record.levelname, record.getMessage())

    routed = logging.getLogger("routed")
    routed.addHandler(Intercept())
    # loguru: the app replaces the default sink, as most do.
    loguru_logger.remove()
    loguru_logger.add(sys.stdout, format="{time} | {level: <8} | {message}", colorize=False)
    # structlog printing to stdout (rendered with colours), and one on top of logging.
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.dev.ConsoleRenderer(colors=True)],
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )
    slog = structlog.get_logger()
    std_slog = structlog.wrap_logger(
        logging.getLogger("slstd"), processors=[structlog.processors.KeyValueRenderer(key_order=["event"])]
    )

    app = flask.Flask("output_app")

    @app.route("/boom")
    def boom():
        raise ValueError("uncaught-marker")

    @app.route("/orders")
    def orders():
        print("print-marker hello")
        print(ESC + "[31mansi-marker red" + ESC + "[0m")
        sys.stdout.write("split-")
        sys.stdout.write("marker joined\n")
        sys.stderr.write("stderr-marker something odd\n")
        print("ERROR: word-marker went wrong")
        print("level=warn logfmt-marker slow query")
        print("[2026-11-17 08:33:00 +0000] [61] [INFO] gunicorn-marker booting")
        print(json.dumps({"level": "error", "message": "json-marker from a JSON logger"}))
        print(json.dumps({"levelname": "WARNING", "msg": "json-name-marker"}))
        print(json.dumps({"level": 40, "msg": "json-num-marker numeric"}))
        sys.stdout.writelines(["writelines-marker ", "one\n"])
        shop.warning("logging-marker once")
        access.info("noprop-marker served")
        routed.warning("routed-marker once")
        loguru_logger.warning("loguru-marker once")
        loguru_logger.bind(order_id=7).info("loguru-extra-marker")
        slog.info("structlog-marker placed", order_id=7)
        std_slog.warning("structlog-std-marker")
        zharp.log.info("zharp-log-marker")
        print("[Zharp] sdk-marker own line")
        print("has-id-marker trace_id=0123 already")
        try:
            raise KeyError("exception-marker")
        except KeyError:
            shop.exception("exception-logged-marker failed")
        try:
            raise TypeError("printed-trace-marker")
        except TypeError:
            traceback.print_exc(file=sys.stdout)
        try:
            raise LookupError("captured-marker")
        except LookupError as error:
            zharp.capture_error(error)
        # Another thread (given the request's context) printing a line in pieces: on the terminal
        # the two threads' writes interleave; the lines sent don't mix.
        piece = threading.Event()

        def other():
            sys.stdout.write("thread-")
            piece.set()
            time.sleep(0.05)
            sys.stdout.write("marker whole\n")

        t = threading.Thread(target=contextvars.copy_context().run, args=(other,))
        t.start()
        piece.wait()
        print("main-marker between")
        t.join()
        return "ok"

    headers = {"traceparent": "00-5bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    status = app.test_client().get("/orders", headers=headers).status_code
    app.test_client().get("/boom", headers=headers)

    # loguru printing from its queue thread, after the record was sent: not sent again.
    queued = loguru_logger.add(sys.stdout, enqueue=True, format="{message}")
    loguru_logger.info("loguru-queued-marker")
    loguru_logger.complete()
    loguru_logger.remove(queued)

    # Outside any request: printed as they are, never stamped.
    print("outer-print-line plain")
    print(json.dumps({"msg": "outer-json-line"}))
    # A line still unfinished when the flush timer fires.
    sys.stdout.write("outside-marker no newline")
    sys.stdout.flush()
    time.sleep(0.5)
    sys.stdout.write("\n")
    # And one never finished at all: sent at shutdown.
    sys.stdout.write("unfinished-marker at exit")
    zharp.shutdown()

    logs = []
    for item in LOGS.get_finished_logs():
        record = getattr(item, "log_record", item)
        logs.append(
            {
                "body": str(record.body),
                "severity": record.severity_number.value if record.severity_number else None,
                "text": record.severity_text,
                "trace_id": format(record.trace_id or 0, "032x"),
                "attributes": {k: v for k, v in (record.attributes or {}).items() if isinstance(v, (str, int))},
            }
        )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "status": status,
                # Errors reported on spans (an exception that escapes a request).
                "span_errors": [
                    str(event.attributes.get("exception.message"))
                    for span in SPANS.get_finished_spans()
                    for event in span.events
                    if event.name == "exception"
                ],
                "logs": logs,
                # Put back, or (when something wrapped it since, like colorama on Windows) passing
                # through untouched.
                "tap_left_on": any(
                    getattr(getattr(stream, "_zharp_tap", None), "enabled", False)
                    for stream in (sys.stdout, sys.stderr, getattr(sys.stdout, "wrapped", None))
                ),
            },
            f,
        )


if __name__ == "__main__":
    main(sys.argv[1])
