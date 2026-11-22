"""Serverless functions, each mode in its own process (serverless_app.py): ``@zharp.aws_lambda``,
Chalice, ``@zharp.cloud_function`` through the Functions Framework, logs only, and a slow endpoint.
One SERVER span per invocation with the FaaS attributes, the caller's trace continued, coldstart on
the first invocation only, a raised exception reported once and raised on, and everything exported
by the time the invocation returned.

    python -m unittest tests.test_serverless
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "serverless_app.py")
TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"


def run(mode):
    done = subprocess.run([sys.executable, APP, mode], capture_output=True, text=True, timeout=300, cwd=HERE)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        raise AssertionError("{} failed:\n{}".format(mode, done.stderr[-3000:]))
    return json.loads(lines[-1])


def by_invocation(output, invocation_id):
    return [s for s in output["spans"] if s["attributes"].get("faas.invocation_id") == invocation_id]


def step(output, label):
    return next(s for s in output["steps"] if s["label"] == label)


class LambdaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run("lambda")

    def test_the_handler_keeps_its_name_signature_and_results(self):
        result = self.out["result"]
        self.assertEqual(result["name"], "handler")
        self.assertEqual(result["doc"], "The handler's own docstring.")
        self.assertEqual(result["signature"], "(event, context)")
        self.assertEqual(result["api"], {"statusCode": 201, "body": '{"ok": true}'})
        self.assertEqual(result["sqs"], {"batchItemFailures": []})

    def test_one_server_span_per_invocation(self):
        self.assertEqual([s["kind"] for s in self.out["spans"]], ["SERVER"] * 3)
        for invocation in ("req-api", "req-sqs", "req-fail"):
            self.assertEqual(len(by_invocation(self.out, invocation)), 1, invocation)

    def test_api_gateway_v2_continues_the_callers_trace(self):
        (span,) = by_invocation(self.out, "req-api")
        self.assertEqual(span["name"], "GET /items/{id}")
        self.assertEqual((span["trace"], span["parent"]), (TRACE, PARENT))
        attributes = span["attributes"]
        self.assertEqual(attributes["http.route"], "/items/{id}")
        self.assertEqual(attributes["url.path"], "/items/42")
        self.assertEqual(attributes["http.response.status_code"], 201)
        self.assertEqual(attributes["client.address"], "203.0.113.9")
        (sqs,) = by_invocation(self.out, "req-sqs")
        self.assertNotEqual(sqs["trace"], TRACE)
        self.assertIsNone(sqs["parent"])

    def test_faas_attributes_and_coldstart_on_the_first_only(self):
        spans = [by_invocation(self.out, i)[0] for i in ("req-api", "req-sqs", "req-fail")]
        self.assertEqual([s["attributes"]["faas.coldstart"] for s in spans], [True, False, False])
        self.assertEqual([s["attributes"]["faas.trigger"] for s in spans], ["http", "pubsub", "pubsub"])
        for span in spans:
            self.assertEqual(span["attributes"]["faas.name"], "orders-fn")
            self.assertEqual(span["attributes"]["cloud.provider"], "aws")
            self.assertEqual(span["attributes"]["cloud.account.id"], "123456789012")
        self.assertEqual(spans[0]["resource"]["cloud.platform"], "aws_lambda")
        self.assertEqual(spans[0]["resource"]["faas.max_memory"], 512 * 1024 * 1024)

    def test_a_raised_exception_is_reported_once_and_raised_on(self):
        self.assertEqual(self.out["result"]["raised"], ["TypeError", "lambda boom"])
        reports = [r for r in self.out["logs"] if r["exception"] == "lambda boom"]
        self.assertEqual(len(reports), 1)
        (span,) = by_invocation(self.out, "req-fail")
        self.assertEqual((reports[0]["handled"], reports[0]["mechanism"]), ("false", "aws-lambda"))
        self.assertEqual(reports[0]["span"], span["span"])
        self.assertEqual(span["status"], "ERROR")

    def test_everything_was_exported_before_each_invocation_returned(self):
        for label, invocation in (("api", "req-api"), ("sqs", "req-sqs"), ("fail", "req-fail")):
            exported = step(self.out, label)
            self.assertIn(invocation, exported["invocations"], label)
            self.assertIn("lambda ran " + invocation, exported["logs"], label)
        self.assertIn("TypeError: lambda boom", step(self.out, "fail")["logs"])


class LambdaLogsOnlyTest(unittest.TestCase):
    def test_no_spans_but_logs_and_errors_flushed_with_the_callers_trace(self):
        out = run("lambda-logs-only")
        self.assertEqual(out["spans"], [])
        self.assertEqual(out["result"]["api"], {"statusCode": 201, "body": '{"ok": true}'})
        self.assertEqual(out["result"]["raised"], ["TypeError", "lambda boom"])
        self.assertEqual(len([r for r in out["logs"] if r["exception"] == "lambda boom"]), 1)
        self.assertIn("lambda ran req-api", step(out, "api")["logs"])
        self.assertIn("TypeError: lambda boom", step(out, "fail")["logs"])
        api_log = next(r for r in out["logs"] if r["body"] == "lambda ran req-api")
        self.assertEqual(api_log["trace"], TRACE)


class FlushTimeoutTest(unittest.TestCase):
    def test_a_slow_endpoint_holds_an_invocation_no_longer_than_its_flush_timeout(self):
        result = run("slow")["result"]
        self.assertEqual(result["custom"]["value"], "done")
        self.assertGreaterEqual(result["custom"]["seconds"], 0.25)
        self.assertLess(result["custom"]["seconds"], 1.0)
        self.assertGreaterEqual(result["default"]["seconds"], 1.9)
        self.assertLess(result["default"]["seconds"], 2.8)
        # Never past the invocation's remaining time (400 ms here).
        self.assertLess(result["near-timeout"]["seconds"], 0.4)


@unittest.skipIf(importlib.util.find_spec("chalice") is None, "chalice not installed")
class ChaliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run("chalice")

    def test_rest_api_one_span_per_request_named_by_route(self):
        self.assertEqual(self.out["result"]["codes"], [200, 404, 500])
        for invocation, status, state in (("ch-ok", 200, "UNSET"), ("ch-404", 404, "UNSET"), ("ch-500", 500, "ERROR")):
            (span,) = by_invocation(self.out, invocation)
            self.assertEqual(span["kind"], "SERVER")
            self.assertEqual(span["name"], "GET /items/{id}")
            self.assertEqual((span["trace"], span["parent"]), (TRACE, PARENT))
            self.assertEqual(span["attributes"]["http.response.status_code"], status)
            self.assertEqual(span["status"], state)
        self.assertTrue(by_invocation(self.out, "ch-ok")[0]["attributes"]["faas.coldstart"])
        self.assertFalse(by_invocation(self.out, "ch-404")[0]["attributes"]["faas.coldstart"])

    def test_a_views_unhandled_exception_is_reported_once(self):
        reports = [r for r in self.out["logs"] if r["exception"] == "chalice boom"]
        self.assertEqual(len(reports), 1)
        self.assertEqual((reports[0]["handled"], reports[0]["mechanism"]), ("false", "chalice"))
        self.assertEqual(reports[0]["span"], by_invocation(self.out, "ch-500")[0]["span"])
        self.assertFalse([r for r in self.out["logs"] if r["exception"] == "no such item"], "a 404 is not an error")

    def test_event_sources_are_invocations_too(self):
        (ok,) = by_invocation(self.out, "ch-sqs")
        (failed,) = by_invocation(self.out, "ch-sqs-fail")
        self.assertEqual(ok["attributes"]["faas.trigger"], "pubsub")
        self.assertEqual(failed["status"], "ERROR")
        self.assertEqual(self.out["result"]["raised"], "chalice sqs boom")
        self.assertEqual(len([r for r in self.out["logs"] if r["exception"] == "chalice sqs boom"]), 1)

    def test_flushed_before_each_invocation_returned(self):
        for invocation in ("ch-ok", "ch-404", "ch-500", "ch-sqs", "ch-sqs-fail"):
            self.assertIn(invocation, step(self.out, invocation)["invocations"])
        self.assertIn("ValueError: chalice boom", step(self.out, "ch-500")["logs"])


@unittest.skipIf(importlib.util.find_spec("functions_framework") is None, "functions-framework not installed")
class CloudFunctionTest(unittest.TestCase):
    def test_http_function(self):
        out = run("gcf-http")
        self.assertEqual(out["result"]["codes"], [201, 500, 201])
        servers = [s for s in out["spans"] if s["kind"] == "SERVER"]
        self.assertEqual(len(servers), 3, "the Flask request span is the invocation's: no second span")
        first, failed, third = (by_invocation(out, i)[0] for i in ("gcf-exec-1", "gcf-exec-2", "gcf-exec-3"))
        self.assertEqual(first["name"], "GET /orders")
        self.assertEqual((first["trace"], first["parent"]), (TRACE, PARENT))
        self.assertEqual([s["attributes"]["faas.coldstart"] for s in (first, failed, third)], [True, False, False])
        for span in (first, failed, third):
            self.assertEqual(span["attributes"]["faas.trigger"], "http")
            self.assertEqual(span["attributes"]["faas.name"], "orders")
            self.assertEqual(span["attributes"]["cloud.provider"], "gcp")
        self.assertEqual(failed["status"], "ERROR")
        reports = [r for r in out["logs"] if r["exception"] == "gcf boom"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["span"], failed["span"])
        # Flushed after Flask ended the span, before the response went back.
        for label in ("gcf-exec-1", "gcf-exec-2", "gcf-exec-3"):
            self.assertIn(label, step(out, label)["invocations"])
        self.assertIn("SyntaxError: gcf boom", step(out, "gcf-exec-2")["logs"])

    def test_cloud_event_function(self):
        out = run("gcf-event")
        self.assertEqual(out["result"]["codes"], [200, 200])
        first, second = (by_invocation(out, i) for i in ("ce-1", "ce-2"))
        self.assertEqual((len(first), len(second)), (1, 1))
        self.assertEqual(first[0]["attributes"]["faas.trigger"], "pubsub")
        self.assertEqual(first[0]["attributes"]["cloudevents.event_type"], "google.cloud.pubsub.topic.v1.messagePublished")
        self.assertEqual([first[0]["attributes"]["faas.coldstart"], second[0]["attributes"]["faas.coldstart"]], [True, False])
        self.assertEqual(first[0]["trace"], TRACE)
        self.assertIn("ce-1", step(out, "ce-1")["invocations"])
        self.assertIn("event ran ce-2", step(out, "ce-2")["logs"])


if __name__ == "__main__":
    unittest.main()
