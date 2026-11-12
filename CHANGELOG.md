# Changelog

## Unreleased

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
