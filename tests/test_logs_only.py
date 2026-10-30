"""tracing=False / ZHARP_TRACING=false: logs and errors go out, spans don't, nothing is injected.

Runs logs_only_app.py in a subprocess, since zharp.init() sets OpenTelemetry's global providers once
per process and test_zharp.py starts it with tracing on.
"""

import json
import os
import subprocess
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs_only_app.py")
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def run(env_tracing=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZHARP_")}
    if env_tracing is not None:
        env["ZHARP_TRACING"] = env_tracing
    out = subprocess.run([sys.executable, APP], env=env, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class LogsOnlyTest(unittest.TestCase):
    def check(self, result):
        self.assertTrue(result["started"])
        self.assertEqual(result["statuses"], [200, 500, 200])
        self.assertEqual(result["span_result"], 42)
        self.assertEqual(result["spans"], [])

        logs = {log["body"]: log for log in result["logs"]}
        for body in ("hello", "from zharp.log"):
            log = logs[body]
            self.assertEqual(log["trace_id"], TRACE_ID)  # the incoming trace, read but not recorded
            self.assertEqual(log["attributes"]["enduser.id"], "test-user")
            self.assertEqual(log["attributes"]["session.id"], "test-session")
            self.assertEqual(log["attributes"]["plan"], "pro")
        self.assertEqual(logs["from zharp.log"]["attributes"]["order_id"], 7)

        errors = [log["attributes"] for log in result["logs"] if "exception.type" in log["attributes"]]
        by_type = {e["exception.type"]: e for e in errors}
        self.assertEqual(len(errors), 2, errors)
        self.assertEqual(by_type["ValueError"]["zharp.handled"], "false")
        self.assertEqual(by_type["ValueError"]["exception.message"], "boom")
        self.assertEqual(by_type["KeyError"]["zharp.handled"], "true")

        self.assertEqual(len(result["downstream_headers"]), 1)
        sent = result["downstream_headers"][0]
        self.assertNotIn("traceparent", sent)
        self.assertNotIn("baggage", sent)

    def test_tracing_false(self):
        self.check(run())

    def test_env_tracing_false(self):
        self.check(run(env_tracing="false"))


if __name__ == "__main__":
    unittest.main()
