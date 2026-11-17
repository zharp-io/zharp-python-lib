"""capture_output and the exact log captures: every line the app prints or logs arrives, once, at
its level, in the request it was written in — and the app still prints all of it.

Runs output_app.py in a subprocess (it replaces sys.stdout/sys.stderr and sets OpenTelemetry's
global providers, once per process). Needs flask, loguru and structlog installed.
"""

import json
import os
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


def run(env_capture=None, arg_capture=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZHARP_")}
    env["PYTHONIOENCODING"] = "utf-8"
    if env_capture is not None:
        env["ZHARP_CAPTURE_OUTPUT"] = env_capture
    if arg_capture is not None:
        env["CAPTURE_OUTPUT_ARG"] = arg_capture
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


class OutputCaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run()

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
        self.check_off(run(env_capture="false"))

    def test_turned_off_by_argument(self):
        self.check_off(run(arg_capture="false"))

    def check_off(self, result):
        for text in PRINTED:
            self.assertFalse(matching(result, text), text)
        for text in LOGGED:
            self.assertEqual(len(matching(result, text)), 1, text)
        self.assertIn("print-marker hello", result["stdout"])


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
