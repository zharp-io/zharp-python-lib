# Changelog

## 0.1.0

- First release: `zharp_logs.init()` and `debug`/`info`/`warn`/`error`/`fatal`, from code or `ZHARP_*` environment variables.
- Caught exceptions carry their type and full traceback automatically; `zharp_logs.exception()` inside `except`.
- `ZharpHandler` for Python's `logging`, or `capture_logging` to attach it to the root logger.
- Uncaught exceptions sent (main thread and others), then handled as before.
- Background sending, batches, retries; everything left sent at exit; safe across forks.
