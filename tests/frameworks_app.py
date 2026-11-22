"""Run by test_frameworks.py in its own process, once per framework (OTel's global providers are set
once per process): zharp.init() with in-memory exporters, a tiny app in that framework, one request
that works and one that raises, then what was exported, as JSON.

    python tests/frameworks_app.py falcon
"""

import asyncio
import json
import os
import sys
import threading

for name in ("ZHARP_KEY", "ZHARP_API_KEY", "ZHARP_SERVICE", "ZHARP_ENVIRONMENT", "ZHARP_DISABLED"):
    os.environ.pop(name, None)

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogs
except ImportError:
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as InMemoryLogs

FRAMEWORK = sys.argv[1]
SPANS = InMemorySpanExporter()
LOGS = InMemoryLogs()
HEADERS = {"X-Shop": "north", "Authorization": "Bearer secret", "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}

# Imported before init, as most files do: the hooks must still reach these.
if FRAMEWORK == "bottle":
    from bottle import Bottle
if FRAMEWORK == "quart":
    from quart import Quart
if FRAMEWORK == "starlette":
    from starlette.applications import Starlette

import zharp

assert zharp.init(key="zpk_test", service="fw-" + FRAMEWORK, environment="test", logs=True, quiet=True, _exporters=(SPANS, LOGS), _batch=False)


def wsgi_get(app, path):
    import webtest

    return webtest.TestApp(app).get(path, headers=HEADERS, expect_errors=True).status_int


def asgi_get(app, path):
    import httpx

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t") as client:
            return (await client.get(path, headers=HEADERS)).status_code

    return asyncio.run(go())


def run():
    if FRAMEWORK == "falcon":
        import falcon
        import falcon.testing

        class Ok:
            def on_get(self, req, resp, id):
                resp.text = "ok"

        class Boom:
            def on_get(self, req, resp):
                raise ValueError("falcon boom")

        app = falcon.App()
        app.add_route("/ok/{id}", Ok())
        app.add_route("/boom", Boom())
        client = falcon.testing.TestClient(app)
        return [client.simulate_get("/ok/7", headers=HEADERS).status_code, client.simulate_get("/boom", headers=HEADERS).status_code]

    if FRAMEWORK == "starlette":
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def ok(request):
            return PlainTextResponse("ok")

        async def boom(request):
            raise ValueError("starlette boom")

        app = Starlette(routes=[Route("/ok/{id}", ok), Route("/boom", boom)])
        return [asgi_get(app, "/ok/7"), asgi_get(app, "/boom")]

    if FRAMEWORK == "tornado":
        import tornado.httpserver
        import tornado.ioloop
        import tornado.web
        import urllib.request

        class Ok(tornado.web.RequestHandler):
            def get(self, id):
                self.write("ok")

        class Boom(tornado.web.RequestHandler):
            def get(self):
                raise ValueError("tornado boom")

        ready, box = threading.Event(), {}

        def serve():
            asyncio.set_event_loop(asyncio.new_event_loop())
            app = tornado.web.Application([(r"/ok/(\d+)", Ok), (r"/boom", Boom)])
            server = tornado.httpserver.HTTPServer(app)
            sockets = tornado.netutil.bind_sockets(0, "127.0.0.1")
            server.add_sockets(sockets)
            box["port"] = sockets[0].getsockname()[1]
            box["loop"] = tornado.ioloop.IOLoop.current()
            ready.set()
            box["loop"].start()

        import tornado.netutil

        threading.Thread(target=serve, daemon=True).start()
        ready.wait(10)
        codes = []
        for path in ("/ok/7", "/boom"):
            try:
                codes.append(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:{}{}".format(box["port"], path), headers=HEADERS)).status)
            except urllib.error.HTTPError as error:
                codes.append(error.code)
        box["loop"].add_callback(box["loop"].stop)
        return codes

    if FRAMEWORK == "pyramid":
        from pyramid.config import Configurator
        from pyramid.response import Response

        def ok(request):
            return Response("ok")

        def boom(request):
            raise ValueError("pyramid boom")

        with Configurator() as config:
            config.add_route("ok", "/ok/{id}")
            config.add_view(ok, route_name="ok")
            config.add_route("boom", "/boom")
            config.add_view(boom, route_name="boom")
            app = config.make_wsgi_app()
        codes = [wsgi_get(app, "/ok/7")]
        try:
            codes.append(wsgi_get(app, "/boom"))
        except ValueError:
            codes.append(500)
        return codes

    if FRAMEWORK == "aiohttp":
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def ok(request):
            return web.Response(text="ok")

        async def boom(request):
            raise ValueError("aiohttp boom")

        async def go():
            app = web.Application()
            app.router.add_get("/ok/{id}", ok)
            app.router.add_get("/boom", boom)
            async with TestClient(TestServer(app)) as client:
                return [(await client.get("/ok/7", headers=HEADERS)).status, (await client.get("/boom", headers=HEADERS)).status]

        return asyncio.run(go())

    if FRAMEWORK == "bottle":
        app = Bottle()

        @app.route("/ok/<id>")
        def ok(id):
            return "ok"

        @app.route("/boom")
        def boom():
            raise ValueError("bottle boom")

        app.catchall = False
        codes = [wsgi_get(app, "/ok/7")]
        try:
            codes.append(wsgi_get(app, "/boom"))
        except ValueError:
            codes.append(500)
        return codes

    if FRAMEWORK == "quart":
        app = Quart(__name__)

        @app.route("/ok/<id>")
        async def ok(id):
            return "ok"

        @app.route("/boom")
        async def boom():
            raise ValueError("quart boom")

        return [asgi_get(app, "/ok/7"), asgi_get(app, "/boom")]

    if FRAMEWORK == "sanic":
        from sanic import Sanic
        from sanic.response import text

        app = Sanic("zharp_test")

        @app.get("/ok/<id>")
        async def ok(request, id):
            return text("ok")

        @app.get("/boom")
        async def boom(request):
            raise ValueError("sanic boom")

        codes = []
        for path in ("/ok/7", "/boom"):
            _, response = app.test_client.get(path, headers=HEADERS)
            codes.append(response.status)
        return codes

    if FRAMEWORK == "wsgi":
        def app(environ, start_response):
            if environ["PATH_INFO"] == "/boom":
                raise ValueError("wsgi boom")
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        traced = zharp.wsgi(app)
        codes = [wsgi_get(traced, "/ok/7")]
        try:
            codes.append(wsgi_get(traced, "/boom"))
        except ValueError:
            codes.append(500)
        return codes

    if FRAMEWORK == "asgi":
        async def app(scope, receive, send):
            if scope["path"] == "/boom":
                raise ValueError("asgi boom")
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})

        traced = zharp.asgi(app)
        return [asgi_get(traced, "/ok/7"), asgi_get(traced, "/boom")]

    if FRAMEWORK == "rq":
        import fakeredis
        from rq import Queue, SimpleWorker

        import rq_jobs

        connection = fakeredis.FakeStrictRedis()
        queue = Queue("orders", connection=connection)
        with zharp.start_span("place order"):  # the worker's spans continue this trace
            queue.enqueue(rq_jobs.ok_job, 7)
            queue.enqueue(rq_jobs.boom_job)
        SimpleWorker([queue], connection=connection).work(burst=True)
        return ["ran"]

    if FRAMEWORK == "celery":
        from celery import Celery

        app = Celery("shop", broker="memory://", backend="cache+memory://")
        app.conf.task_always_eager = True

        @app.task
        def ok_task(n):
            return n

        @app.task
        def boom_task():
            raise ValueError("celery boom")

        ok_task.delay(7)
        try:
            boom_task.delay()
        except ValueError:
            pass
        return ["ran"]

    raise SystemExit("unknown framework " + FRAMEWORK)


codes = run()
zharp.flush()
out = []
for span in SPANS.get_finished_spans():
    attrs = dict(span.attributes or {})
    out.append({
        "name": span.name,
        "kind": span.kind.name,
        "status": span.status.status_code.name,
        "route": attrs.get("http.route"),
        "trace": format(span.context.trace_id, "032x"),
        "headers": {k: v for k, v in attrs.items() if k.startswith("http.request.header.")},
        "exceptions": [dict(e.attributes).get("exception.message") for e in span.events if e.name == "exception"],
    })
print(json.dumps({"codes": codes, "spans": out}, default=str))
