# zharp

[Zharp](https://zharp.io) Application Monitoring for Python: every request traced, outgoing HTTP
calls and database queries timed, `logging` records sent with their trace, and errors grouped into
issues. Built on the official [OpenTelemetry](https://opentelemetry.io) SDK — `zharp` only
configures it (exporter, resource, propagation, the `logging` bridge) and turns on the
instrumentations for the libraries you have.

Full documentation: [zharp.io/docs/sdks/python](https://zharp.io/docs/sdks/python)

## Install

```bash
pip install zharp
```

Python 3.9+.

## Start it

Copy your app's key from **Applications → your app → Setup** in Zharp, then call `init()` once,
before your framework starts:

```python
import zharp

zharp.init(key="zpk_…", service="checkout-api", environment="production")
```

- **Django** — at the top of `manage.py` and `wsgi.py` / `asgi.py`.
- **FastAPI / Flask** — at the top of the module that creates the app, before `FastAPI()` /
  `Flask(__name__)`.

That's all. What gets instrumented, when installed: Django, FastAPI, Flask, `requests`, `httpx`,
`psycopg`, `psycopg2`, SQLAlchemy. Any other OpenTelemetry instrumentation you add is sent too.

Leave arguments out to read them from the environment: `ZHARP_KEY`, `ZHARP_SERVICE` (then
`OTEL_SERVICE_NAME`), `ZHARP_ENVIRONMENT`, `ZHARP_RELEASE`, `ZHARP_ENDPOINT`. `ZHARP_DISABLED=true`
turns it off.

## Users and sessions

A browser or mobile app using a Zharp SDK sends `traceparent` and `baggage` (`enduser.id`,
`session.id`) with its API calls. The request's trace continues from the front end, and every span
and log of it carries the user and session — nothing to do on the server. When the server knows
the user better:

```python
zharp.set_user(id=user.id, email=user.email)   # for the rest of this request
```

## Logs

`logging` is bridged: records at `INFO` and above (`logging_level=` to change, `False` to leave
`logging` alone) are sent with the active trace and the request's user. Or log to Zharp directly:

```python
zharp.log.info("Order placed", {"order_id": 42})   # debug, info, warn, error, fatal
```

## Errors

An exception that escapes a request is reported (unhandled) with the request's trace; so is one
that ends the program or a thread. Report one you caught:

```python
try:
    charge(order)
except PaymentError as e:
    zharp.capture_error(e, {"order_id": order.id})   # handled
```

`logger.exception(...)` reports too. Each exception is reported once, even when both the
instrumentation and the framework's logger see it.

## Logs only

To send logs and errors without tracing:

```python
zharp.init(key="zpk_…", service="checkout-api", tracing=False)   # or ZHARP_TRACING=false
```

Logs (`zharp.log.*` and `logging`), errors (uncaught and `capture_error`), the user, session and
tags still go out. No spans are recorded or sent, HTTP clients and databases aren't instrumented,
and no `traceparent`/`baggage` is added to outgoing requests. Django, FastAPI and Flask are hooked
with a no-op tracer only to read the incoming `traceparent` and `baggage`, so a request's logs still
carry the caller's trace id, user and session. `zharp.start_span()` runs its work without a span.

## More

```python
zharp.set_tag("region", "eu-west")                  # on every span and log after
with zharp.start_span("render invoice"):            # a span around some work
    render(invoice)
zharp.flush()                                       # send now: end of a job or serverless call
```

| Option | Environment | Default |
|---|---|---|
| `key` | `ZHARP_KEY` | — (nothing is sent without one) |
| `service` | `ZHARP_SERVICE`, `OTEL_SERVICE_NAME` | `name` in `pyproject.toml` |
| `environment` | `ZHARP_ENVIRONMENT` | none |
| `release` | `ZHARP_RELEASE` | none |
| `endpoint` | `ZHARP_ENDPOINT` | `https://api.zharp.io/api/v1/otlp` |
| `sample_rate` | `ZHARP_SAMPLE_RATE` | `1.0` (share of traces kept; logs and handled errors always sent) |
| `logging_level` | `ZHARP_LOGGING_LEVEL` | `INFO` |
| `capture_uncaught` | — | `True` |
| `instrument` | — | `True` |
| `tracing` | `ZHARP_TRACING` | `True` (`False`: logs only) |

Telemetry is batched and sent every 5 seconds from a background thread, gzip-compressed, and
whatever is left goes when the program exits. `init()` never raises.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . flask fastapi httpx uvicorn
.venv/bin/python -m unittest discover -s tests
```

`examples/fastapi_app.py` and `examples/crash.py` run against any Zharp endpoint (set `ZHARP_KEY`
and `ZHARP_ENDPOINT`).

### Releasing

1. Bump `src/zharp/_version.py`, update `CHANGELOG.md`, run the tests.
2. `rm -rf dist && python -m build` (or `python -c "from setuptools import build_meta as b; b.build_sdist('dist'); b.build_wheel('dist')"`).
3. `python -m twine upload dist/*`, then tag `vX.Y.Z`.

## License

MIT © [Zharp](https://zharp.io)
