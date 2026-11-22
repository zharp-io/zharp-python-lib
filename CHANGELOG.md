# Changelog

## 0.5.0 (`zharp`) — 2026-11-22

- More frameworks, traced by the same `zharp.init()`: Starlette, Quart, AIOHTTP, Falcon (WSGI and ASGI), Bottle, Pyramid, Tornado and Tryton — one span per request named by its route, headers with secrets filtered, and the error a request raised.
- Workers: each Celery task and RQ job is a span with its errors, joined to the request that queued it.
- `zharp.wsgi(app)` / `zharp.asgi(app)` trace any other WSGI or ASGI app.
- Falcon is traced without OpenTelemetry's Falcon instrumentation, which breaks `falcon.testing` on Falcon 4.

## 0.4.0 (`zharp`) — 2026-11-21

- Request and response headers on request spans (`capture_headers`, `ZHARP_CAPTURE_HEADERS`); secrets are sent as `[Filtered]`.
- A failed query is reported once, with your code's stack and without the row's values (`capture_query_errors`).
- The code that ran goes with each error (`capture_source_context`), so the error page shows it without a repository.
- FastAPI and Flask apps are traced whatever the import order: `zharp.init()` only has to run before the app is created.

## 0.3.0 (`zharp`) — 2026-11-17

**Behaviour change:** what the app logs and prints is no longer sent unless `logs=True` / `ZHARP_LOGS=true`. Where the Zharp agent reads the app's output it brings those lines in, linked by the `trace_id=… span_id=…` stamp; turn `logs` on where there is no agent. Errors, traces and `zharp.log` are sent as before.

- **Logs are off by default** (`logs=False`, `ZHARP_LOGS`): on a server the Zharp agent reads the
  app's output and brings its logs in; the SDK sending them too made every line arrive twice. With
  logs off nothing the app logs or prints is sent — not `logging`, loguru, structlog or stdout.
  Errors (uncaught, `capture_error`, `logger.exception`), traces and `zharp.log` are sent as
  before, and `logging` no longer lowers the root logger's level. `logs=True` (or
  `ZHARP_LOGS=true`) where there is no agent, such as serverless.
- **Log correlation** (`log_correlation=True`, `ZHARP_LOG_CORRELATION=false` to turn off): every
  line printed during a request ends with ` trace_id=<32 hex> span_id=<16 hex>`; a JSON line gets
  `trace_id` and `span_id` fields and stays valid JSON. Added at the handler for `logging` (every
  handler, files too) and on stdout/stderr for everything else (`print()`, loguru, structlog —
  their streams captured at import are pointed at it). Lines outside a request, lines already
  carrying a `trace_id` and the SDK's `[Zharp]` lines are left alone; a line is never stamped twice.
  A multi-line record (a logged exception, a printed traceback) gets one stamp, at the end of its
  last line. On a terminal nothing is held back; collected output (pipes, files, container logs)
  holds a piece of a line printed in a request until its newline, a flush or 200 ms.
- With `logs=True`, every log line reaches Zharp, tied to the request it was written in:
  - `capture_output` (and `ZHARP_CAPTURE_OUTPUT`), on with `logs`: whatever is printed to
    `sys.stdout`/`sys.stderr` without a logging library — `print()`, libraries writing to the
    streams — is sent as logs. Level read from the line (JSON `level`/`severity`/`levelname`,
    Python's numeric levels, words like `ERROR`, `[warn]`, `level=error`), else info (stdout) or
    warning (stderr). Lines split per thread, colour codes dropped, 32 KB cap, an unfinished line
    sent after 200 ms. The app prints exactly as before.
  - `logging` loggers that don't propagate to the root (uvicorn, gunicorn, `propagate = False`)
    are sent too.
  - loguru and structlog are captured with their level and fields (a sink that survives
    `logger.remove()`; structlog events as the processors accept them).
  - A line is sent once: what a capture prints isn't read again, nested captures (logging routed
    into loguru and back) send it from the outer one, and a line printed shortly after it was sent
    (queued handlers) is skipped. The traceback printed for an uncaught exception, already reported
    as an error, isn't sent line by line.

## 0.2.0 (`zharp`)

- `capture_bodies` (and `ZHARP_CAPTURE_BODIES`): request and response bodies on HTTP spans, off by
  default — `"errors"` (status 400+ or no response) or `"all"`, or `CaptureBodies(mode, max_size,
  redact_keys, urls)`. Incoming requests in Django (WSGI/ASGI), Flask and FastAPI; outgoing calls
  with requests and httpx. Same attributes and rules as the other Zharp SDKs: field names kept,
  secret values `[Filtered]` (secret objects keep their keys), uploads listed with files as
  `[file name · type · size]`, inline images and binaries described, gzip/deflate inflated, 32 KB
  default / 128 KB max.
- Secret-looking query values are masked in `url.full`, `http.url`, `http.target` and `url.query` on
  every span, with or without `capture_bodies`.
- Fixed: when Django was instrumented at `django.setup()` (init before settings), the instrumentor
  was given `django.setup()`'s own arguments.

## 0.1.0 (`zharp`)

- First release of `zharp`, Zharp Application Monitoring on OpenTelemetry: `zharp.init()` configures
  the OTel SDK (OTLP/HTTP exporters with gzip, resource, W3C tracecontext + baggage) and instruments
  Django, FastAPI, Flask, requests, httpx, psycopg, psycopg2 and SQLAlchemy when installed.
- `logging` bridged with trace context; `zharp.log.*`.
- `enduser.id` / `session.id` copied from incoming baggage onto every span and log; `zharp.set_user`.
- Errors: escaped request exceptions (handled=false, reported once), uncaught exceptions in the main
  thread and other threads, `zharp.capture_error` (handled=true).
- `zharp.set_tag`, `zharp.start_span`, `zharp.flush`, `zharp.shutdown`.
- `tracing=False` / `ZHARP_TRACING=false`: logs only (logs, errors, user and tags; no spans, no
  client or database instrumentation, nothing injected into outgoing requests).
- Replaces the logs-only `zharp-logs` package (0.1.x), which stays on PyPI unchanged.
