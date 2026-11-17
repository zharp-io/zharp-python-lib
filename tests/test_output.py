"""What the app logs and prints.

- logs=True: every line arrives, once, at its level, in the request it was written in (the exact
  captures and capture_output) — and the app still prints all of it.
- By default (logs off) none of it is sent; errors, traces and zharp.log still are.
- log_correlation (on by default): each line printed in a request ends with its trace and span id
  (fields in a JSON line), for the Zharp agent that reads the output.

Runs output_app.py in a subprocess (it replaces sys.stdout/sys.stderr and sets OpenTelemetry's
global providers, once per process). Needs flask, loguru and structlog installed.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from zharp import _output

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_app.py")
TRACE_ID = "5bf92f3577b34da6a3ce929d0e0e4736"

INFO, WARN, ERROR = 9, 13, 17

#: Printed to stdout/stderr without logging: only the output capture sends them.
PRINTED = (
    "print-marker hello",
    "ansi-marker red",
    "split-marker joined",
    "stderr-marker something odd",
    "word-marker went wrong",
    "logfmt-marker slow query",
    "gunicorn-marker booting",
    "json-marker from a JSON logger",
    "json-name-marker",
    "json-num-marker numeric",
    "writelines-marker one",
    "thread-marker whole",
    "main-marker between",
)
#: Logged through logging, loguru, structlog or zharp.log: sent by the exact captures.
LOGGED = (
    "logging-marker once",
    "noprop-marker served",
    "routed-marker once",
    "loguru-marker once",
    "loguru-extra-marker",
    "structlog-marker placed",
    "structlog-std-marker",
    "zharp-log-marker",
)


#: What the Zharp backend reads from the end of a line the agent collected.
STAMP = re.compile(r"\s*\btrace_id=([0-9a-f]{32})\s+span_id=([0-9a-f]{16})\s*$", re.IGNORECASE)


def run(env=None, **args):
    """output_app.py, with ``ZHARP_*`` from ``env`` and init() arguments (``logs=True`` …)."""
    child = {k: v for k, v in os.environ.items() if not k.startswith("ZHARP_")}
    child["PYTHONIOENCODING"] = "utf-8"
    child.update(env or {})
    for name, value in args.items():
        child["ARG_" + name.upper()] = "true" if value else "false"
    env = child
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        out = subprocess.run(
            [sys.executable, APP, path], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        with open(path, encoding="utf-8") as f:
            result = json.load(f)
    finally:
        os.remove(path)
    result["stdout"], result["stderr"] = out.stdout, out.stderr
    return result


def matching(result, text):
    return [log for log in result["logs"] if text in log["body"]]


def errors_with(result, message):
    """Errors as Zharp reads them: logs carrying exception.*, and exception events on spans."""
    logged = [log for log in result["logs"] if message in str(log["attributes"].get("exception.message", ""))]
    return logged + [{"trace_id": TRACE_ID} for text in result["span_errors"] if message in text]


class OutputCaptureTest(unittest.TestCase):
    """logs=True: everything you built last round."""

    @classmethod
    def setUpClass(cls):
        cls.result = run(logs=True)

    def one(self, text):
        found = matching(self.result, text)
        self.assertEqual(len(found), 1, "{}: {}".format(text, [log["body"] for log in found]))
        return found[0]

    def test_every_line_arrives_once(self):
        self.assertEqual(self.result["status"], 200)
        for text in PRINTED + LOGGED + ("loguru-queued-marker", "outside-marker no newline", "unfinished-marker at exit"):
            self.one(text)

    def test_printed_lines_come_from_the_output(self):
        for text in PRINTED:
            self.assertIn(self.one(text)["attributes"]["log.source"], ("stdout", "stderr"), text)
        self.assertEqual(self.one("stderr-marker")["attributes"]["log.source"], "stderr")

    def test_logged_lines_come_from_their_logger_not_the_output(self):
        for text in LOGGED:
            self.assertNotIn(self.one(text)["attributes"].get("log.source"), ("stdout", "stderr"), text)
        self.assertEqual(self.one("loguru-marker")["attributes"]["log.source"], "loguru")
        self.assertEqual(self.one("structlog-marker")["attributes"]["log.source"], "structlog")

    def test_levels(self):
        expected = {
            "print-marker hello": INFO,
            "split-marker joined": INFO,
            "stderr-marker something odd": WARN,
            "word-marker went wrong": ERROR,
            "logfmt-marker slow query": WARN,
            "gunicorn-marker booting": INFO,
            "json-marker from a JSON logger": ERROR,
            "json-name-marker": WARN,
            "json-num-marker numeric": ERROR,
            "logging-marker once": WARN,
            "noprop-marker served": INFO,
            "routed-marker once": WARN,
            "loguru-marker once": WARN,
            "loguru-extra-marker": INFO,
            "structlog-marker placed": INFO,
            "structlog-std-marker": WARN,
        }
        for text, severity in expected.items():
            self.assertEqual(self.one(text)["severity"], severity, text)

    def test_json_lines_send_their_message(self):
        self.assertEqual(self.one("json-marker")["body"], "json-marker from a JSON logger")
        self.assertEqual(self.one("json-num-marker")["body"], "json-num-marker numeric")

    def test_colour_codes_are_dropped(self):
        self.assertEqual(self.one("ansi-marker")["body"], "ansi-marker red")
        self.assertFalse([log for log in self.result["logs"] if "\x1b" in log["body"]])

    def test_bodies_sent_do_not_carry_the_stamp(self):
        stamped = [log["body"] for log in self.result["logs"] if "trace_id=" in log["body"]]
        self.assertEqual(stamped, ["has-id-marker trace_id=0123 already"])

    def test_errors_are_sent_once(self):
        for text in ("exception-marker", "captured-marker", "uncaught-marker"):
            self.assertEqual(len(errors_with(self.result, text)), 1, text)

    def test_fields_are_kept(self):
        self.assertEqual(self.one("structlog-marker")["attributes"]["order_id"], 7)
        self.assertEqual(self.one("structlog-marker")["body"], "structlog-marker placed")
        self.assertEqual(self.one("loguru-extra-marker")["attributes"]["order_id"], 7)

    def test_lines_in_a_request_carry_its_trace(self):
        for text in PRINTED + LOGGED:
            self.assertEqual(self.one(text)["trace_id"], TRACE_ID, text)
        self.assertEqual(self.one("outside-marker")["trace_id"], "0" * 32)

    def test_the_app_still_prints_everything(self):
        printed = self.result["stdout"] + self.result["stderr"]
        # Two threads' pieces interleave on the terminal; each still arrives whole (above).
        for text in tuple(t for t in PRINTED if t != "thread-marker whole") + (
            "thread-",
            "marker whole",
            "logging-marker once",
            "noprop-marker served",
            "loguru-marker once",
            "structlog-marker",
            "loguru-queued-marker",
            "outside-marker no newline",
            "unfinished-marker at exit",
        ):
            self.assertIn(text, printed, text)
        self.assertIn("stderr-marker", self.result["stderr"])
        self.assertFalse(self.result["tap_left_on"])

    def test_turned_off_by_env(self):
        self.check_off(run({"ZHARP_CAPTURE_OUTPUT": "false"}, logs=True))

    def test_turned_off_by_argument(self):
        self.check_off(run(logs=True, capture_output=False))

    def test_logs_turned_on_by_env(self):
        result = run({"ZHARP_LOGS": "true"})
        for text in ("print-marker hello", "logging-marker once", "loguru-marker once", "structlog-marker"):
            self.assertEqual(len(matching(result, text)), 1, text)

    def check_off(self, result):
        for text in PRINTED:
            self.assertFalse(matching(result, text), text)
        for text in LOGGED:
            self.assertEqual(len(matching(result, text)), 1, text)
        self.assertIn("print-marker hello", result["stdout"])


class LogsOffTest(unittest.TestCase):
    """The default: what the app logs and prints is not sent; errors, traces and zharp.log are."""

    @classmethod
    def setUpClass(cls):
        cls.result = run()

    def test_nothing_printed_or_logged_is_sent(self):
        for text in PRINTED + LOGGED + ("loguru-queued-marker", "outside-marker", "unfinished-marker", "has-id-marker"):
            if text == "zharp-log-marker":
                continue
            self.assertFalse(matching(self.result, text), text)
        sources = {log["attributes"].get("log.source") for log in self.result["logs"]}
        self.assertFalse(sources & {"stdout", "stderr", "loguru", "structlog"}, sources)

    def test_errors_and_zharp_log_are_still_sent(self):
        self.assertEqual(len(matching(self.result, "zharp-log-marker")), 1)
        for text in ("exception-marker", "captured-marker", "uncaught-marker"):
            errors = errors_with(self.result, text)
            self.assertEqual(len(errors), 1, text)
            self.assertEqual(errors[0]["trace_id"], TRACE_ID, text)
        bodies = [log["body"] for log in self.result["logs"]]
        self.assertTrue(all("exception.type" in log["attributes"] or log["body"] == "zharp-log-marker" for log in self.result["logs"]), bodies)

    def test_the_argument_wins_over_the_environment(self):
        result = run({"ZHARP_LOGS": "true"}, logs=False)
        self.assertFalse(matching(result, "print-marker hello"))
        self.assertFalse(matching(result, "logging-marker once"))


#: Printed in the request as text lines (in the default run, logs off).
STAMPED_TEXT = (
    "print-marker hello",
    "ansi-marker red",
    "split-marker joined",
    "stderr-marker something odd",
    "word-marker went wrong",
    "logfmt-marker slow query",
    "gunicorn-marker booting",
    "writelines-marker one",
    "logging-marker once",
    "routed-marker once",
    "loguru-marker once",
    "loguru-extra-marker",
    "structlog-marker placed",
    "marker whole",
    "main-marker between",
)
STAMPED_JSON = ("json-marker from a JSON logger", "json-name-marker", "json-num-marker numeric")


def printed_lines(result):
    return (result["stdout"] + result["stderr"]).splitlines()


def record(result, first, last):
    """The printed lines of one multi-line record, from the line containing ``first`` to the one
    containing ``last``."""
    for stream in (result["stdout"], result["stderr"]):
        lines = stream.splitlines()
        ends = [i for i, line in enumerate(lines) if last in line]
        if ends:
            end = ends[0]
            start = next(i for i in range(end, -1, -1) if first in lines[i])
            return lines[start : end + 1]
    raise AssertionError("no record ending with " + last)


def line_with(result, text):
    found = [line for line in printed_lines(result) if text in line]
    assert len(found) == 1, (text, found)
    return found[0]


class StampTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run()

    def test_text_lines_in_a_request_end_with_its_ids(self):
        for text in STAMPED_TEXT:
            line = line_with(self.result, text)
            match = STAMP.search(line)
            self.assertIsNotNone(match, line)
            self.assertEqual(match.group(1), TRACE_ID, line)
            self.assertEqual(line.count("trace_id"), 1, line)

    def check_one_stamp_at_the_end(self, lines):
        self.assertGreater(len(lines), 3, lines)  # a message and its traceback
        text = "\n".join(lines)
        self.assertEqual(text.count("trace_id"), 1, text)
        match = STAMP.search(lines[-1])
        self.assertIsNotNone(match, lines[-1])
        self.assertEqual(match.group(1), TRACE_ID)
        # Joined into one entry, as the agent does, the stamp is still read from its end.
        self.assertEqual(STAMP.search(text).group(1), TRACE_ID)

    def test_a_logged_exception_has_one_stamp_at_its_end(self):
        self.check_one_stamp_at_the_end(record(self.result, "exception-logged-marker failed", "KeyError: 'exception-marker'"))
        # Flask logging the error that ended a request, on stderr.
        self.check_one_stamp_at_the_end(record(self.result, "Exception on /boom", "ValueError: uncaught-marker"))

    def test_a_printed_traceback_has_one_stamp_at_its_end(self):
        lines = record(self.result, "Traceback (most recent call last)", "TypeError: printed-trace-marker")
        self.check_one_stamp_at_the_end(lines)

    def test_json_lines_get_fields_and_stay_valid(self):
        for text in STAMPED_JSON:
            record = json.loads(line_with(self.result, text))
            self.assertEqual(record["trace_id"], TRACE_ID)
            self.assertRegex(record["span_id"], r"^[0-9a-f]{16}$")

    def test_left_alone(self):
        self.assertEqual(line_with(self.result, "outer-print-line"), "outer-print-line plain")
        self.assertEqual(json.loads(line_with(self.result, "outer-json-line")), {"msg": "outer-json-line"})
        self.assertEqual(line_with(self.result, "outside-marker"), "outside-marker no newline")
        self.assertEqual(line_with(self.result, "has-id-marker"), "has-id-marker trace_id=0123 already")
        self.assertEqual(line_with(self.result, "sdk-marker"), "[Zharp] sdk-marker own line")

    def test_stamped_with_logs_on_too(self):
        result = run(logs=True)
        for text in STAMPED_TEXT:
            self.assertRegex(line_with(result, text), STAMP)

    def test_turned_off_by_env(self):
        self.check_off(run({"ZHARP_LOG_CORRELATION": "false"}))

    def test_turned_off_by_argument(self):
        self.check_off(run(log_correlation=False))

    def check_off(self, result):
        stamped = [line for line in printed_lines(result) if "trace_id" in line]
        self.assertEqual(stamped, ["has-id-marker trace_id=0123 already"])
        self.assertEqual(line_with(result, "print-marker"), "print-marker hello")


class _Target:
    """A stream that records each write; ``tty`` says whether it is a terminal."""

    def __init__(self, tty):
        self.tty = tty
        self.writes = []

    def isatty(self):
        return self.tty

    def write(self, text):
        self.writes.append(text)
        return len(text)

    def flush(self):
        pass


class TerminalTest(unittest.TestCase):
    """Collected output (a pipe) holds a piece of a line back for its stamp; a terminal doesn't."""

    IDS = ("5bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")

    def stamper(self, tty):
        target = _Target(tty)
        return target, _output._Stamper(target, _output._Flusher())

    def in_span(self, fn):
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        span = NonRecordingSpan(SpanContext(int(self.IDS[0], 16), int(self.IDS[1], 16), False, TraceFlags(1)))
        with trace.use_span(span):
            fn()

    def test_a_terminal_gets_each_piece_at_once(self):
        target, stamper = self.stamper(tty=True)

        def write():
            stamper.write("hello")
            self.assertEqual(target.writes, ["hello"])  # no delay
            stamper.write("\n")

        self.in_span(write)
        self.assertRegex("".join(target.writes).rstrip("\n"), STAMP)
        self.assertEqual("".join(target.writes), "hello trace_id={} span_id={}\n".format(*self.IDS))

    def test_a_pipe_holds_the_piece_until_its_newline(self):
        target, stamper = self.stamper(tty=False)

        def write():
            stamper.write("hello")
            self.assertEqual(target.writes, [])
            stamper.write("\n")

        self.in_span(write)
        self.assertEqual(target.writes, ["hello trace_id={} span_id={}\n".format(*self.IDS)])

    def test_a_traceback_printed_line_by_line_gets_one_stamp(self):
        target, stamper = self.stamper(tty=True)
        lines = ["Traceback (most recent call last):", '  File "app.py", line 3, in <module>', "    boom()", "ValueError: boom"]

        def write():
            for line in lines:
                stamper.write(line + "\n")

        self.in_span(write)
        text = "".join(target.writes)
        self.assertEqual(text.count("trace_id"), 1, text)
        self.assertEqual(STAMP.search(text.rstrip("\n")).group(1), self.IDS[0])

    def test_lines_printed_one_by_one_are_stamped_one_by_one(self):
        target, stamper = self.stamper(tty=False)

        def write():
            for text in ("first line", "second line"):
                stamper.write(text)
                stamper.write("\n")

        self.in_span(write)
        self.assertEqual("".join(target.writes).count("trace_id"), 2)


class ParseLineTest(unittest.TestCase):
    def level(self, line):
        level, _ = _output.parse_line(line)
        return level[1] if level else None

    def test_common_formats(self):
        self.assertEqual(self.level("WARNING:root:disk almost full"), "WARN")
        self.assertEqual(self.level("INFO:     127.0.0.1:5000 - \"GET / HTTP/1.1\" 200 OK"), "INFO")
        self.assertEqual(self.level("ERROR:    Exception in ASGI application"), "ERROR")
        self.assertEqual(self.level("[2026-11-17 08:33:00 +0000] [61] [CRITICAL] WORKER TIMEOUT"), "FATAL")
        self.assertEqual(self.level("2026-11-17 08:33:00,123 - app - DEBUG - cache miss"), "DEBUG")
        self.assertEqual(self.level("time=1 level=error msg=boom"), "ERROR")
        self.assertEqual(self.level('{"severity": "warning", "message": "x"}'), "WARN")
        self.assertEqual(self.level('{"level": 30, "msg": "x"}'), "WARN")
        self.assertIsNone(self.level("ValueError: boom"))
        self.assertIsNone(self.level("[17/Nov/2026 08:33:00] \"GET / HTTP/1.1\" 200 1234"))
        self.assertIsNone(self.level("{not json} at all"))


if __name__ == "__main__":
    unittest.main()
