import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import zharp_logs  # noqa: E402
from zharp_logs import _logger, _message, _transport  # noqa: E402
from zharp_logs._pipeline import MAX_QUEUED, Pipeline  # noqa: E402


class FakeZharp:
    """Stands in for Zharp's ingest endpoint: records each request, answers with ``status``."""

    def __init__(self):
        self.bodies = []
        self.headers = []
        self.status = 201
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                fake.headers.append(dict(self.headers))
                fake.bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(fake.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = "http://127.0.0.1:{}/api/v1/".format(self.server.server_address[1])

    def logs(self):
        return [log for body in self.bodies for log in body["logs"]]

    def wait_for(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while len(self.logs()) < count and time.time() < deadline:
            time.sleep(0.02)
        return self.logs()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ZharpLogsTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeZharp()
        _logger._reset_for_tests()
        for name in [k for k in os.environ if k.startswith("ZHARP_")]:
            del os.environ[name]

    def tearDown(self):
        zharp_logs.close(timeout=2)
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, zharp_logs.ZharpHandler):
                root.removeHandler(handler)
        self.fake.stop()

    def start(self, **options):
        defaults = dict(api_key="test-key", service="checkout-api", environment="production",
                        app_version="2.4.0", endpoint=self.fake.endpoint, flush_interval=0.05)
        defaults.update(options)
        zharp_logs.init(**defaults)

    def test_sends_the_shape_every_zharp_sdk_sends(self):
        self.start()
        zharp_logs.set_context({"region": "eu"})
        zharp_logs.info("User signed in", {"user_id": 42})
        log = self.fake.wait_for(1)[0]

        self.assertEqual(log["service"], "checkout-api")
        self.assertEqual(log["level"], "info")
        self.assertEqual(log["message"], "User signed in")
        self.assertRegex(log["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(log["environment"], "production")
        self.assertEqual(log["version"], "2.4.0")
        self.assertNotIn("source", log)

        meta = log["metadata"]
        self.assertEqual(meta["user_id"], 42)
        self.assertEqual(meta["sessionId"], zharp_logs.session_id())
        self.assertEqual(meta["environment"], "production")
        context = meta["context"]
        self.assertEqual(context["region"], "eu")
        self.assertEqual(context["appVersion"], "2.4.0")
        for key in ("hostname", "pid", "pythonVersion", "platform"):
            self.assertIn(key, context)

        headers = {k.lower(): v for k, v in self.fake.headers[0].items()}
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["x-zharp-sdk"], "python-logs/" + zharp_logs.__version__)

    def test_a_caught_exception_brings_its_type_and_traceback_without_being_asked(self):
        self.start(flush_interval=60)
        try:
            raise ValueError("card declined")
        except ValueError as e:
            zharp_logs.error(e)
            zharp_logs.error("Payment failed", {"order_id": 99}, exc=e)
            zharp_logs.exception("Charge raised")
            zharp_logs.warn("Retrying", {"err": e})
        # error goes at once without waiting for the minute-long interval; flush sends the warn.
        zharp_logs.flush(5)
        logs = self.fake.wait_for(4)

        plain = logs[0]
        self.assertEqual(plain["message"], "ValueError: card declined")
        self.assertEqual(plain["metadata"]["name"], "ValueError")
        self.assertIn("test_a_caught_exception", plain["metadata"]["stack"])
        self.assertIn("raise ValueError", plain["metadata"]["stack"])

        self.assertEqual(logs[1]["message"], "Payment failed")
        self.assertEqual(logs[1]["metadata"]["order_id"], 99)
        self.assertIn("ValueError: card declined", logs[1]["metadata"]["stack"])

        self.assertEqual(logs[2]["message"], "Charge raised")
        self.assertIn("ValueError", logs[2]["metadata"]["stack"])

        self.assertEqual(logs[3]["level"], "warn")
        self.assertIn("card declined", logs[3]["metadata"]["stack"])
        self.assertEqual(logs[3]["metadata"]["err"]["name"], "ValueError")

    def test_a_chained_exception_keeps_its_cause(self):
        try:
            try:
                {}["missing"]
            except KeyError as cause:
                raise RuntimeError("lookup failed") from cause
        except RuntimeError as e:
            stack = _message.stack_of(e)
        self.assertIn("KeyError", stack)
        self.assertIn("RuntimeError: lookup failed", stack)

    def test_an_exception_never_raised_gets_where_it_was_logged(self):
        stack = _message.stack_of(ValueError("made, not raised"))
        self.assertIn("test_an_exception_never_raised", stack)
        self.assertNotIn("_message.py", stack)

    def test_a_stack_the_caller_gave_wins(self):
        self.start()
        entry = _logger.build_entry(_logger._setup, _logger.LogLevel.ERROR, "boom", {"stack": "my own"},
                                    RuntimeError("x"), None, 0)
        self.assertEqual(entry["metadata"]["stack"], "my own")
        self.assertEqual(entry["timestamp"], "1970-01-01T00:00:00.000Z")

    def test_one_log_can_go_under_another_service(self):
        self.start()
        zharp_logs.log("warning", "Slow", service=" payments ")
        log = self.fake.wait_for(1)[0]
        self.assertEqual(log["service"], "payments")
        self.assertEqual(log["level"], "warn")

    def test_every_message_becomes_text(self):
        self.assertEqual(_message.to_message(None), "(empty message)")
        self.assertEqual(_message.to_message("  "), "(empty message)")
        self.assertEqual(_message.to_message(42), "42")
        self.assertEqual(_message.to_message({"a": 1}), '{"a":1}')
        self.assertEqual(_message.to_message(KeyError()), "KeyError")

    def test_metadata_json_cannot_hold_becomes_text(self):
        import datetime
        import decimal
        import uuid

        loop = {}
        loop["self"] = loop
        meta = _message.to_json_map({
            "nan": float("nan"),
            "when": datetime.datetime(2026, 10, 26, 9, 0, tzinfo=datetime.timezone.utc),
            "price": decimal.Decimal("9.99"),
            "id": uuid.UUID(int=1),
            "tags": {"a"},
            "level": zharp_logs.LogLevel.WARN,
            "loop": loop,
            "obj": object(),
        })
        json.dumps(meta)  # all of it is JSON now
        self.assertEqual(meta["nan"], "nan")
        self.assertEqual(meta["when"], "2026-10-26T09:00:00.000Z")
        self.assertEqual(meta["price"], "9.99")
        self.assertEqual(meta["tags"], ["a"])
        self.assertEqual(meta["level"], "warn")

    def test_standard_logging_goes_through_the_handler(self):
        self.start()
        logger = logging.getLogger("shop.checkout")
        logger.addHandler(zharp_logs.ZharpHandler())
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            logger.warning("Stock low for %s", "SKU-1", extra={"sku": "SKU-1"})
            try:
                1 / 0
            except ZeroDivisionError:
                logger.exception("Pricing failed")
            logs = self.fake.wait_for(2)
        finally:
            logger.handlers.clear()
            logger.propagate = True

        self.assertEqual(logs[0]["level"], "warn")
        self.assertEqual(logs[0]["message"], "Stock low for SKU-1")
        self.assertEqual(logs[0]["metadata"]["logger"], "shop.checkout")
        self.assertEqual(logs[0]["metadata"]["sku"], "SKU-1")
        self.assertEqual(logs[1]["level"], "error")
        self.assertEqual(logs[1]["metadata"]["name"], "ZeroDivisionError")
        self.assertIn("1 / 0", logs[1]["metadata"]["stack"])

    def test_capture_logging_attaches_once_to_the_root_logger(self):
        self.start(capture_logging="WARNING")
        self.start(capture_logging="WARNING")
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, zharp_logs.ZharpHandler)]
        self.assertEqual(len(handlers), 1)
        logging.getLogger("x").info("not sent")
        logging.getLogger("x").error("sent")
        logs = self.fake.wait_for(1)
        time.sleep(0.2)
        self.assertEqual([log["message"] for log in self.fake.logs()], ["sent"])

    def test_settings_come_from_the_environment(self):
        os.environ.update({"ZHARP_API_KEY": "env-key", "ZHARP_SERVICE": "from-env", "ZHARP_ENV": "staging",
                           "ZHARP_ENDPOINT": self.fake.endpoint})
        zharp_logs.init(flush_interval=0.05)
        zharp_logs.info("hello")
        log = self.fake.wait_for(1)[0]
        self.assertEqual(log["service"], "from-env")
        self.assertEqual(log["environment"], "staging")
        self.assertEqual({k.lower(): v for k, v in self.fake.headers[0].items()}["x-api-key"], "env-key")

    def test_nothing_starts_without_a_key_or_when_disabled(self):
        zharp_logs.init(service="x", endpoint=self.fake.endpoint)
        self.assertFalse(zharp_logs.is_active())
        os.environ["ZHARP_DISABLED"] = "true"
        zharp_logs.init(api_key="k", service="x", endpoint=self.fake.endpoint)
        self.assertFalse(zharp_logs.is_active())
        zharp_logs.error("dropped")
        time.sleep(0.1)
        self.assertEqual(self.fake.bodies, [])

    def test_server_errors_are_retried_and_refusals_are_not(self):
        transport = _transport.Transport("k", _transport.normalize_endpoint(self.fake.endpoint), retry_delay=0.001)
        self.fake.status = 503
        self.assertFalse(transport.send([{}]))
        self.assertEqual(len(self.fake.bodies), 3)
        self.fake.bodies.clear()
        self.fake.status = 429
        self.assertTrue(transport.send([{}]))
        self.assertEqual(len(self.fake.bodies), 1)

    def test_bodies_are_cut_by_size_and_a_large_entry_goes_alone(self):
        entries = [{"m": "x" * 40} for _ in range(3)] + [{"m": "y" * 500}]
        bodies = _transport.split_by_size(entries, 120)
        self.assertEqual(sum(len(json.loads(b)["logs"]) for b in bodies), 4)
        self.assertEqual(len(json.loads(bodies[-1])["logs"]), 1)

    def test_the_queue_keeps_the_newest_500(self):
        pipeline = Pipeline(_transport.Transport("k", "http://127.0.0.1:9"), 60, 1000)
        for i in range(600):
            pipeline._queue.append({"i": i})
        left = pipeline.drain()
        self.assertEqual(len(left), MAX_QUEUED)
        self.assertEqual(left[0]["i"], 100)

    def test_endpoints_lose_trailing_slashes_and_default_to_zharp(self):
        self.assertEqual(_transport.normalize_endpoint(None), "https://api.zharp.io/api/v1")
        self.assertEqual(_transport.normalize_endpoint(" https://x.io/api/v1// "), "https://x.io/api/v1")


class ProcessTest(unittest.TestCase):
    """What only a real process shows: exit and crashes."""

    def setUp(self):
        self.fake = FakeZharp()

    def tearDown(self):
        self.fake.stop()

    def run_script(self, body):
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, {src!r})
            import zharp_logs
            zharp_logs.init(api_key="k", service="script", endpoint={endpoint!r})
        """).format(src=SRC, endpoint=self.fake.endpoint) + textwrap.dedent(body)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(script)
        try:
            started = time.time()
            result = subprocess.run([sys.executable, f.name], capture_output=True, text=True, timeout=30)
            return result, time.time() - started
        finally:
            os.unlink(f.name)

    def test_a_short_script_sends_its_logs_and_ends_promptly(self):
        result, took = self.run_script('zharp_logs.info("last words")\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([log["message"] for log in self.fake.logs()], ["last words"])
        self.assertLess(took, 5)

    def test_an_uncaught_exception_is_sent_and_still_ends_the_program(self):
        result, _ = self.run_script('def charge():\n    raise RuntimeError("card declined")\ncharge()\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn("RuntimeError: card declined", result.stderr)  # still printed as before
        logs = self.fake.logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["level"], "fatal")
        self.assertEqual(logs[0]["metadata"]["type"], "uncaughtException")
        self.assertIn("in charge", logs[0]["metadata"]["stack"])

    def test_an_exception_in_a_thread_is_an_error(self):
        result, _ = self.run_script(
            'import threading\n'
            't = threading.Thread(target=lambda: 1 / 0, name="worker-1")\n'
            't.start(); t.join()\n'
        )
        self.assertEqual(result.returncode, 0)
        logs = self.fake.logs()
        self.assertEqual(logs[0]["level"], "error")
        self.assertEqual(logs[0]["metadata"]["thread"], "worker-1")


if __name__ == "__main__":
    unittest.main()
