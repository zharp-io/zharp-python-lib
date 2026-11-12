"""capture_bodies through the real instrumentations: Flask, FastAPI and Django requests, and
requests/httpx calls to a local server. Runs capture_app.py in a subprocess per mode, since
zharp.init() sets OpenTelemetry's global providers once per process."""

import json
import os
import subprocess
import sys
import unittest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_app.py")
_RUNS = {}


def run(mode):
    if mode not in _RUNS:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZHARP_")}
        env["PYTHONIOENCODING"] = "utf-8"
        out = subprocess.run([sys.executable, APP, mode], env=env, capture_output=True, timeout=120)
        if out.returncode != 0:
            raise AssertionError(out.stderr.decode("utf-8", "replace"))
        _RUNS[mode] = json.loads(out.stdout.decode("utf-8"))
    return _RUNS[mode]


def span(result, kind, contains):
    """The first span of this kind whose URL attributes contain ``contains``."""
    for item in result["spans"]:
        attributes = item["attributes"]
        where = " ".join(str(attributes.get(k, "")) for k in ("http.target", "http.url", "url.full", "url.path"))
        if item["kind"] == kind and contains in (where + " " + item["name"]):
            return attributes
    raise AssertionError("no {} span for {}".format(kind, contains))


def everything(result):
    return json.dumps(result["spans"], ensure_ascii=False)


class ErrorsModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run("errors")

    def test_the_apps_see_what_they_would_without_the_sdk(self):
        results = self.result["results"]
        self.assertEqual(results["flask_login"]["token"], "leaked-token")
        self.assertEqual(results["flask_upload"], {"error": "too large", "size": 102400, "title": "Holiday"})
        self.assertEqual(results["fastapi_items"]["got"], "widget")
        self.assertEqual(results["fastapi_events"], "data: 1\n\n")
        self.assertEqual(results["django_orders"]["qty"], 0)
        self.assertEqual(results["requests_charge"]["cardNumber"], "4242424242424242")
        self.assertEqual(results["requests_stream"], 3004)

    def test_flask(self):
        login = span(self.result, "SERVER", "/login")
        self.assertEqual(json.loads(login["http.request.body"]), {"email": "ada@x.test", "password": "[Filtered]"})
        self.assertEqual(json.loads(login["http.response.body"])["token"], "[Filtered]")
        self.assertEqual(login["http.response.header.content-type"], "application/json")
        upload = span(self.result, "SERVER", "/upload")
        self.assertEqual(
            upload["http.request.body"],
            '{"title":"Holiday","password":"[Filtered]","avatar":"[file me.png · image/png · 100 KB]","after":"still-here"}',
        )
        self.assertEqual(span(self.result, "SERVER", "/refuse")["http.request.body"], "refused-body", "a body the app never read")

    def test_fastapi(self):
        items = span(self.result, "SERVER", "/items")
        self.assertEqual(items["http.request.body"], '{"name":"widget","apiKey":"[Filtered]"}')
        self.assertEqual(json.loads(items["http.response.body"])["secret"], "[Filtered]")
        self.assertEqual(span(self.result, "SERVER", "/events")["http.response.body"], "[event stream]")
        self.assertEqual(span(self.result, "SERVER", "/boom")["http.request.body"], '{"boom":"body","otp":"[Filtered]"}')

    def test_django(self):
        orders = span(self.result, "SERVER", "django/orders")
        self.assertEqual(orders["http.request.body"], '{"qty":0,"cvv":"[Filtered]"}')
        self.assertEqual(json.loads(orders["http.response.body"])["sessionToken"], "[Filtered]")

    def test_requests(self):
        charge = span(self.result, "CLIENT", "/charge?sig")
        self.assertEqual(charge["http.request.body"], '{"amount":5,"cvv":"[Filtered]"}')
        self.assertEqual(charge["http.response.body"], '{"error":"card declined","cardNumber":"[Filtered]"}', "inflated")
        self.assertEqual(span(self.result, "CLIENT", "/image")["http.response.body"], "[binary · image/png · 3 KB]")
        down = span(self.result, "CLIENT", "127.0.0.1:9/down")
        self.assertEqual(down["http.request.body"], '{"lost":"network-body","password":"[Filtered]"}', "no response: request body kept")

    def test_httpx(self):
        spans = [s["attributes"] for s in self.result["spans"] if s["kind"] == "CLIENT" and "/charge" in str(s["attributes"].get("http.url", s["attributes"].get("url.full", ""))) and "sig=" not in str(s["attributes"].get("http.url", ""))]
        charge = spans[0]
        self.assertEqual(charge["http.request.body"], '{"amount":7,"token":"[Filtered]"}')
        self.assertEqual(charge["http.response.body"], "[stream · application/json · 60 B]", "never read for the app")
        uploads = [s["attributes"] for s in self.result["spans"] if s["kind"] == "CLIENT" and "multipart" in str(s["attributes"].get("http.request.header.content-type", ""))]
        self.assertEqual(uploads[0]["http.request.body"], '{"note":"hello","pin":"[Filtered]","doc":"[file report.pdf · application/pdf · 5 KB]"}')

    def test_successful_requests_carry_no_bodies(self):
        text = everything(self.result)
        for marker in ("ok-request-body", "ok-fastapi-body", "ok-django-body", "ok-outgoing-body"):
            self.assertNotIn(marker, text)

    def test_secrets_never_leave(self):
        text = everything(self.result)
        for secret in ("pw-secret", "leaked-token", "upload-secret", "srv-secret", "k-123", "123456", "dj-token", "999", "4242424242424242", "net-secret", "httpx-secret", "4321"):
            self.assertNotIn(secret, text)

    def test_query_secrets_are_masked(self):
        text = everything(self.result)
        for secret in ("query-secret-1", "dj-query-secret", "client-query-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(span(self.result, "SERVER", "/ok")["http.target"], "/ok?api_key=[Filtered]&page=2")


class AllModeTest(unittest.TestCase):
    def test_successful_requests_too(self):
        result = run("all")
        text = everything(result)
        for marker in ("ok-request-body", "ok-fastapi-body", "ok-django-body", "ok-outgoing-body"):
            self.assertIn(marker, text)


class OptionsTest(unittest.TestCase):
    def test_max_size_and_redact_keys(self):
        result = run("options")
        orders = span(result, "SERVER", "django/orders")
        self.assertEqual(json.loads(orders["http.request.body"])["qty"], "[Filtered]")
        self.assertIn("ok-request-body", everything(result), "mode all")


class OffTest(unittest.TestCase):
    def test_no_bodies_but_urls_still_masked(self):
        result = run("off")
        self.assertNotIn("http.request.body", everything(result))
        self.assertNotIn("http.response.body", everything(result))
        for secret in ("query-secret-1", "dj-query-secret", "client-query-secret"):
            self.assertNotIn(secret, everything(result))


if __name__ == "__main__":
    unittest.main()
