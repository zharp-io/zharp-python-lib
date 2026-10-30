# Changelog

## 0.1.0 (`zharp`)

- First release of `zharp`, Zharp Application Monitoring on OpenTelemetry: `zharp.init()` configures
  the OTel SDK (OTLP/HTTP exporters with gzip, resource, W3C tracecontext + baggage) and instruments
  Django, FastAPI, Flask, requests, httpx, psycopg, psycopg2 and SQLAlchemy when installed.
- `logging` bridged with trace context; `zharp.log.*`.
- `enduser.id` / `session.id` copied from incoming baggage onto every span and log; `zharp.set_user`.
- Errors: escaped request exceptions (handled=false, reported once), uncaught exceptions in the main
  thread and other threads, `zharp.capture_error` (handled=true).
- `zharp.set_tag`, `zharp.start_span`, `zharp.flush`, `zharp.shutdown`.
- Replaces the logs-only `zharp-logs` package (0.1.x), which stays on PyPI unchanged.
