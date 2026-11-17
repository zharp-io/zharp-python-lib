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

Every logger is included, also those that don't propagate to the root (uvicorn's and gunicorn's
access and error logs, libraries that set `propagate = False`). loguru and structlog are captured
too, with their level and fields (`bind()`, `extra`, keyword arguments); structlog on top of
`logging` goes through the `logging` bridge.

Everything else the app prints — `print()`, libraries writing to `sys.stdout`/`sys.stderr`
directly — is sent as well (`capture_output`, on by default). Each line keeps the request it was
printed in. Its level is read from the line: a JSON record's own `level`/`severity`/`levelname`
(Python's numbers too), or a word such as `ERROR`, `[warn]`, `level=error`, `WARNING:root:`;
otherwise stdout is info and stderr a warning. Colour codes are dropped, lines are capped at 32 KB
and an unfinished line is sent after 200 ms. A line sent by `logging`, loguru or structlog is never
sent a second time when it is printed, and your app prints everything exactly as before.
`capture_output=False` (or `ZHARP_CAPTURE_OUTPUT=false`) turns the catch-all off.

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

## Request and response bodies

Off by default. Turn on `capture_bodies` to see what a request sent and what came back, on its span:

```python
zharp.init(key="zpk_…", service="checkout-api", capture_bodies="errors")   # failed requests only; "all" for every one

# or ZHARP_CAPTURE_BODIES=errors in the environment, or, with more control:
from zharp import CaptureBodies
zharp.init(capture_bodies=CaptureBodies(mode="all", max_size=4096, redact_keys=["date_of_birth"], urls=["api.example.com"]))
```

- `"errors"` (or `True`) keeps bodies only for requests that answered 400 and above, or got no
  response. `"all"` keeps every one.
- Incoming requests in Django (WSGI and ASGI), Flask and FastAPI; outgoing calls with requests and
  httpx. `urls` (hosts, URL prefixes or compiled regexes) narrows which requests.
- Text — JSON, forms, XML, plain text — is kept up to 32 KB (`max_size`, up to 128 KB); gzip and
  deflate bodies are inflated first. Every field name is kept. A file in an upload shows as
  `[file photo.png · image/png · 240 KB]`, an inline image or long base64 value as
  `[image/png · 24 KB]`, a body that isn't text as `[binary · application/pdf · 1.2 MB]` or
  `[event stream]`. File contents are never sent.
- Your app reads and writes its bodies exactly as before; the SDK keeps a copy of what passes, up to
  the limit. A body the app never read (a request it refused) is read after it has answered.
- A response your code streams (`requests(..., stream=True)`, every httpx response) is read after its
  span has ended, so it shows as `[stream · application/json · 60 B]` rather than its text.

Values under keys that look secret — `password`, `pwd`, `token`, `secret`, `apiKey`,
`authorization`, `cookie`, `card`, `cvv`, `ssn` and similar — become `[Filtered]` in your process,
before anything is sent; the field stays, only its value is hidden (under a secret object such as
`card`, each value inside is hidden and its keys kept). Anything else, such as names and email
addresses, is sent as it is: add those keys to `redact_keys`.

Whether or not bodies are on, secret-looking values in URL query strings (`?token=`, `key`, `sig`,
`session`, `password` …) are recorded as `[Filtered]` on every span.

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
| `capture_bodies` | `ZHARP_CAPTURE_BODIES` | off (`"errors"`, `"all"`, or `CaptureBodies(…)`) |
| `capture_output` | `ZHARP_CAPTURE_OUTPUT` | `True` (everything else printed to stdout/stderr is sent as logs; level read from the line; never sent twice) |

Telemetry is batched and sent every 5 seconds from a background thread, gzip-compressed, and
whatever is left goes when the program exits. `init()` never raises.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . flask fastapi httpx requests django uvicorn loguru structlog
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
