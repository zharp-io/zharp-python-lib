"""Run by test_capture.py in its own process (OTel's global providers are set once per process):
init(capture_bodies=<argv[1]>) with in-memory exporters, then requests through Flask, FastAPI and
Django and outgoing calls with requests and httpx to a local server. Prints the spans as JSON."""

import gzip
import http.server
import json
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
MODE = sys.argv[1] if len(sys.argv) > 1 else "errors"
RESULTS = {}


class Downstream(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.startswith("/charge"):
            body = gzip.compress(json.dumps({"error": "card declined", "cardNumber": "4242424242424242"}).encode())
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
        elif self.path.startswith("/image"):
            body = b"\x89PNG" + b"\x00" * 3000
            self.send_response(500)
            self.send_header("Content-Type", "image/png")
        else:
            body = b'{"fine":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


def downstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Downstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:{}".format(server.server_address[1])


def multipart(boundary, parts):
    out = b""
    for part in parts:
        out += b"--" + boundary + b"\r\n" + part
    return out + b"--" + boundary + b"--\r\n"


UPLOAD_BOUNDARY = b"zharpBoundary42"
UPLOAD = multipart(
    UPLOAD_BOUNDARY,
    [
        b'Content-Disposition: form-data; name="title"\r\n\r\nHoliday\r\n',
        b'Content-Disposition: form-data; name="password"\r\n\r\nupload-secret\r\n',
        b'Content-Disposition: form-data; name="avatar"; filename="me.png"\r\nContent-Type: image/png\r\n\r\n'
        + b"\x01" * (100 * 1024)
        + b"\r\n",
        b'Content-Disposition: form-data; name="after"\r\n\r\nstill-here\r\n',
    ],
)


def flask_requests():
    import flask

    app = flask.Flask("capture_flask")

    @app.post("/login")
    def login():
        data = flask.request.get_json()
        return flask.jsonify(error="bad credentials for " + data["email"], token="leaked-token"), 401

    @app.route("/ok", methods=["GET", "PUT"])
    def ok():
        flask.request.get_data()
        return flask.jsonify(saved=True)

    @app.post("/upload")
    def upload():
        avatar = flask.request.files["avatar"]
        return flask.jsonify(error="too large", size=len(avatar.read()), title=flask.request.form["title"]), 413

    @app.post("/refuse")
    def refuse():
        return "nope", 403  # never reads the body

    client = app.test_client()
    RESULTS["flask_login"] = client.post("/login", json={"email": "ada@x.test", "password": "pw-secret"}).get_json()
    RESULTS["flask_ok"] = client.put("/ok?api_key=query-secret-1&page=2", json={"name": "ok-request-body"}).status_code
    RESULTS["flask_upload"] = client.post(
        "/upload", data=UPLOAD, content_type="multipart/form-data; boundary=" + UPLOAD_BOUNDARY.decode()
    ).get_json()
    RESULTS["flask_refuse"] = client.post("/refuse", data=b"refused-body", content_type="text/plain").status_code


def fastapi_requests():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.post("/items")
    async def items(request: Request):
        body = await request.json()
        return JSONResponse({"error": "invalid", "secret": "srv-secret", "got": body["name"]}, status_code=422)

    @app.get("/events")
    async def events():
        async def stream():
            yield b"data: 1\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", status_code=500)

    @app.post("/boom")
    async def boom(request: Request):
        await request.body()
        raise RuntimeError("boom")

    @app.post("/fine")
    async def fine(request: Request):
        await request.body()
        return {"fine": True}

    client = TestClient(app, raise_server_exceptions=False)
    RESULTS["fastapi_items"] = client.post("/items", json={"name": "widget", "apiKey": "k-123"}).json()
    RESULTS["fastapi_events"] = client.get("/events").text
    RESULTS["fastapi_boom"] = client.post("/boom", content=b'{"boom":"body","otp":"123456"}', headers={"content-type": "application/json"}).status_code
    RESULTS["fastapi_fine"] = client.post("/fine", json={"fine": "ok-fastapi-body"}).json()


def django_requests():
    import django
    from django.conf import settings

    settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test",
        MIDDLEWARE=[],
        INSTALLED_APPS=[],
    )
    django.setup()  # the Django instrumentation is installed here, with the body hooks

    from django.test import Client

    client = Client()
    response = client.post("/django/orders?token=dj-query-secret", data=json.dumps({"qty": 0, "cvv": "999"}), content_type="application/json")
    RESULTS["django_orders"] = json.loads(response.content)
    RESULTS["django_list"] = client.get("/django/list").status_code


def django_orders(request):
    from django.http import JsonResponse

    data = json.loads(request.body)
    return JsonResponse({"error": "qty must be positive", "qty": data["qty"], "sessionToken": "dj-token"}, status=400)


def django_list(request):
    from django.http import JsonResponse

    return JsonResponse({"items": ["ok-django-body"]})


try:
    from django.urls import path

    urlpatterns = [path("django/orders", django_orders), path("django/list", django_list)]
except ImportError:  # pragma: no cover
    urlpatterns = []


def client_requests(base):
    import httpx
    import requests

    response = requests.post(base + "/charge?sig=client-query-secret", json={"amount": 5, "cvv": "123"})
    RESULTS["requests_charge"] = response.json()
    RESULTS["requests_fine"] = requests.post(base + "/fine", json={"outgoing": "ok-outgoing-body"}).json()
    with requests.get(base + "/image", stream=True) as streamed:
        RESULTS["requests_stream"] = len(streamed.content)
    try:
        requests.post("http://127.0.0.1:9/down", json={"lost": "network-body", "password": "net-secret"}, timeout=2)
    except requests.RequestException:
        RESULTS["requests_down"] = "failed"

    with httpx.Client() as client:
        RESULTS["httpx_charge"] = client.post(base + "/charge", json={"amount": 7, "token": "httpx-secret"}).status_code
        files = {"doc": ("report.pdf", b"%PDF" + b"\x00" * 5000, "application/pdf")}
        RESULTS["httpx_upload"] = client.post(base + "/image", data={"note": "hello", "pin": "4321"}, files=files).status_code


def main():
    option = None if MODE == "off" else MODE
    started = zharp.init(
        key="zpk_test",
        service="capture-test",
        quiet=True,
        capture_bodies=option if MODE != "options" else {"mode": "all", "max_size": 1000, "redact_keys": ["qty"]},
        _exporters=(SPANS, InMemoryLogs()),
        _batch=False,
    )
    server, base = downstream()
    try:
        flask_requests()
        fastapi_requests()
        django_requests()
        client_requests(base)
    finally:
        server.shutdown()
    zharp.flush()
    spans = [
        {"name": span.name, "kind": span.kind.name, "attributes": dict(span.attributes or {})}
        for span in SPANS.get_finished_spans()
    ]
    sys.stdout.write(json.dumps({"started": started, "results": RESULTS, "spans": spans}))


if __name__ == "__main__":
    os.environ.pop("ZHARP_CAPTURE_BODIES", None)
    main()
