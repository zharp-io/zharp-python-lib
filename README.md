# zharp-logs

Send your Python app's logs and uncaught exceptions to [Zharp](https://zharp.io). No dependencies —
only the standard library — and Python 3.8 and up. Works in scripts, web apps (Django, Flask,
FastAPI), workers (Celery, RQ) and anything that uses Python's `logging`.

Full documentation: [zharp.io/docs/sdks/python](https://zharp.io/docs/sdks/python)

---

## 1. Install

```bash
pip install zharp-logs
```

## 2. Get an API key

1. In Zharp, open **Integrations & API** and choose **Main API keys**.
2. Click **Create API key**, give it a name, and tick **Ingest only**. That key can send logs but
   can't read anything, so it's safe in a server's environment.
3. Click **Create key** and copy the key — it's shown only once.

<p>
  <img src="https://www.zharp.io/images/docs/api-key-create.png" alt="Create API key, with Ingest only ticked" width="48%">
  <img src="https://www.zharp.io/images/docs/api-key-created.png" alt="The new key, shown once with a Copy button" width="48%">
</p>

## 3. Start it

Call `init()` once, as early as possible. Give it your key in code, or let it read the key from the
environment — either works.

**In code:**

```python
import zharp_logs

zharp_logs.init(api_key="your-ingest-only-key", service="checkout-api", environment="production")
```

**From the environment** — set the variables and call `init()` with nothing:

```bash
ZHARP_API_KEY=your-ingest-only-key
ZHARP_SERVICE=checkout-api
ZHARP_ENV=production
```

```python
zharp_logs.init()
```

You can mix them: what you pass to `init()` is used, and anything you leave out is read from the
environment. Without a service name, the `name` in your `pyproject.toml` is used when one is found.

When it starts, it prints one line to stderr so you know it's working:

```
[Zharp] Logging active — service "checkout-api", environment "production"
```

With no API key anywhere it prints how to give it one and sends nothing — it never raises.

## 4. Log

```python
zharp_logs.info("User signed in", {"user_id": 42})
zharp_logs.warn("Slow network detected", {"latency_ms": 3200})
zharp_logs.error("Payment failed", {"order_id": 99, "reason": "insufficient_funds"})
```

`debug` and `fatal` are there too (and `warning` and `critical`, the names `logging` uses). The dict
is optional and is kept with the log. A caught exception brings its type and full traceback — causes
included — by itself:

```python
try:
    charge(order)
except Exception as e:
    zharp_logs.exception("Payment failed", {"order_id": order.id})   # the exception being handled
    zharp_logs.error(e)                                              # or the exception itself
    zharp_logs.error("Payment failed", exc=e)                        # or your message, with it
```

Logs are sent from a background thread in batches every 5 seconds; `error` and `fatal` go at once,
and everything left is sent when the program exits. Logging never blocks on the network and never
raises.

## 5. See your logs

Sign in at [zharp.io](https://zharp.io/login), open **Logs**, and pick your service in the service
dropdown.

![The Logs page on zharp.io](https://www.zharp.io/images/docs/logs-dashboard.png)

---

## Python's `logging`

Most apps already log through `logging`. Send it to Zharp with the handler:

```python
import logging
import zharp_logs

zharp_logs.init()
logging.getLogger().addHandler(zharp_logs.ZharpHandler(logging.INFO))
```

Or let `init()` attach it to the root logger: `zharp_logs.init(capture_logging=True)` (`INFO` and up)
or `capture_logging="WARNING"`, or set `ZHARP_CAPTURE_LOGGING`. Levels map as you'd expect —
`WARNING` → `warn`, `CRITICAL` → `fatal`. Each log carries the logger's name and any fields you pass
with `extra={...}`, and `logger.exception(...)` brings its traceback.

**Django** — in `settings.py`:

```python
import zharp_logs

zharp_logs.init()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"zharp": {"class": "zharp_logs.ZharpHandler", "level": "INFO"}},
    "root": {"handlers": ["zharp"], "level": "INFO"},
}
```

**Flask / FastAPI** — call `zharp_logs.init(capture_logging=True)` where the app is created.

Where the [Zharp agent](https://zharp.io/docs/agent/setup) already collects your app's output
(Docker, journald, Kubernetes), send `logging` through one of them, not both, or every line arrives
twice.

## Uncaught exceptions

From the moment `init()` succeeds, an uncaught exception is sent with its traceback — `fatal` in
the main thread, `error` in another thread — then Python prints it and the program ends as it would
have. Turn it off with `capture_exceptions=False`. An exception your web framework catches and turns
into a 500 isn't uncaught; log those with the `logging` handler (frameworks log them at `ERROR`).

## Options

| Option | Environment variable | Default |
|---|---|---|
| `api_key` | `ZHARP_API_KEY` | — |
| `service` | `ZHARP_SERVICE`, then `OTEL_SERVICE_NAME` | the `name` in your `pyproject.toml`, else `unknown-service` |
| `environment` | `ZHARP_ENV` | none |
| `app_version` | `ZHARP_VERSION` | none |
| `source` | `ZHARP_SOURCE` | none |
| `endpoint` | `ZHARP_ENDPOINT` | `https://api.zharp.io/api/v1` |
| `capture_logging` | `ZHARP_CAPTURE_LOGGING` | off |
| `capture_exceptions` | — | on |
| `flush_interval` | — | 5 seconds |
| `max_batch_size` | — | 50 |

An option passed to `init()` wins. `ZHARP_DISABLED=true` turns the SDK off.

## What's sent automatically

Every log carries a session ID (the same for one run of the program) and where it ran: hostname,
process ID, Python version, platform and OS. Add your own fields to every log with
`zharp_logs.set_context({"region": "eu-west"})`; start a new session ID with
`zharp_logs.reset_session_id()`.

## Workers and forks

Safe in gunicorn, uWSGI, Celery and multiprocessing workers: a process forked after `init()` starts
its own sender on its first log. `zharp_logs.flush()` sends what's queued and waits (up to 5 seconds
by default) — useful at the end of a job or a serverless function. `zharp_logs.close()` sends
everything and stops.

## Releasing (maintainers)

Published to PyPI as [`zharp-logs`](https://pypi.org/project/zharp-logs/).

### Each release

1. Bump `__version__` in `src/zharp_logs/_version.py` and add the changes to `CHANGELOG.md`.
2. Run the tests: `python -m unittest discover -s tests`.
3. Build — with setuptools alone, no extra tools:
   ```bash
   rm -rf dist
   python -c "from setuptools import build_meta as b; b.build_sdist('dist'); b.build_wheel('dist')"
   ```
   (or `python -m build`, if the `build` package is installed).
4. Upload: `python -m twine upload dist/*`. It's installable with `pip install zharp-logs` a minute
   later.
5. Tag it: `git tag -a v0.1.1 -m "0.1.1" && git push origin v0.1.1`.

### One-time setup on a new machine

1. `pip install twine`.
2. On [pypi.org](https://pypi.org) → **Account settings** → **API tokens**, create a token (once the
   project exists, scope it to `zharp-logs`).
3. Save it in `~/.pypirc` — outside this repo, never committed:
   ```ini
   [pypi]
   username = __token__
   password = pypi-…
   ```

## License

MIT © [Zharp](https://zharp.io)
