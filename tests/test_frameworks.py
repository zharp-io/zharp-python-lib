"""Every framework beyond Django, Flask and FastAPI, each in its own process (frameworks_app.py):
one span per request named by its route, the failing request's exception on it, headers with
secrets redacted, and the caller's trace continued. A framework that isn't installed is skipped.

    python -m unittest tests.test_frameworks
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "frameworks_app.py")
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

#: framework -> (library it needs, the route its working request should be named by, or None)
WEB = {
    "starlette": ("starlette", "/ok/{id}"),
    "falcon": ("falcon", "/ok/{id}"),
    "tornado": ("tornado", "/ok/{id}"),
    "pyramid": ("pyramid", "/ok/{id}"),
    "aiohttp": ("aiohttp", "/ok/{id}"),
    "bottle": ("bottle", "/ok/<id>"),
    "quart": ("quart", "/ok/<id>"),
    "sanic": ("sanic_testing", "/ok/<id>"),
    "wsgi": ("webtest", None),
    "asgi": ("httpx", None),
}


def run(framework):
    done = subprocess.run([sys.executable, APP, framework], capture_output=True, text=True, timeout=300, cwd=HERE)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        raise AssertionError("{} failed:\n{}".format(framework, done.stderr[-3000:]))
    return json.loads(lines[-1])


class FrameworksTest(unittest.TestCase):
    def test_web_frameworks(self):
        for framework, (library, route) in WEB.items():
            with self.subTest(framework=framework):
                if importlib.util.find_spec(library) is None:
                    self.skipTest(library + " not installed")
                result = run(framework)
                self.assertEqual(result["codes"], [200, 500])
                servers = [s for s in result["spans"] if s["kind"] == "SERVER"]
                self.assertEqual(len(servers), 2, result["spans"])
                ok, boom = servers
                # The caller's traceparent is continued. (Sanic's test client is httpx, which is
                # traced itself: its CLIENT span is the caller there.)
                if framework == "sanic":
                    callers = {s["trace"] for s in result["spans"] if s["kind"] == "CLIENT"}
                    self.assertTrue(all(s["trace"] in callers for s in servers), result["spans"])
                else:
                    self.assertTrue(all(s["trace"] == TRACE for s in servers))
                if route:
                    self.assertEqual(ok["route"], route)
                    self.assertTrue(ok["name"].endswith(route), ok["name"])
                self.assertEqual(ok["status"], "UNSET")
                self.assertEqual(boom["status"], "ERROR")
                self.assertTrue(any(framework + " boom" in (m or "") for m in boom["exceptions"]), boom)
                self.assertEqual(ok["headers"].get("http.request.header.x_shop"), ["north"])
                self.assertEqual(ok["headers"].get("http.request.header.authorization"), ["[REDACTED]"])
                # No per-message ASGI spans: one span per request.
                self.assertFalse([s for s in result["spans"] if "http send" in s["name"] or "http receive" in s["name"]])

    def test_rq_jobs_continue_the_enqueuing_trace(self):
        if importlib.util.find_spec("rq") is None or importlib.util.find_spec("fakeredis") is None:
            self.skipTest("rq / fakeredis not installed")
        spans = run("rq")["spans"]
        jobs = {s["name"]: s for s in spans if s["kind"] == "CONSUMER"}
        parent = next(s for s in spans if s["name"] == "place order")
        self.assertEqual(set(jobs), {"rq.job rq_jobs.ok_job", "rq.job rq_jobs.boom_job"})
        self.assertTrue(all(s["trace"] == parent["trace"] for s in jobs.values()))
        self.assertEqual(jobs["rq.job rq_jobs.boom_job"]["status"], "ERROR")
        self.assertEqual(jobs["rq.job rq_jobs.boom_job"]["exceptions"], ["rq boom"])

    def test_celery_tasks(self):
        if importlib.util.find_spec("celery") is None:
            self.skipTest("celery not installed")
        spans = {s["name"]: s for s in run("celery")["spans"] if s["kind"] == "CONSUMER"}
        self.assertEqual(spans["run/shop.boom_task"]["status"], "ERROR")
        self.assertEqual(spans["run/shop.boom_task"]["exceptions"], ["celery boom"])
        self.assertEqual(spans["run/shop.ok_task"]["status"], "UNSET")


if __name__ == "__main__":
    unittest.main()
