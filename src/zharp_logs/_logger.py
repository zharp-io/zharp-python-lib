"""The running logger: starting it, building entries, crashes and exit."""

import atexit
import enum
import os
import platform
import socket
import sys
import threading
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Union

from . import _env, _message
from ._pipeline import Pipeline
from ._transport import Transport, normalize_endpoint

#: How long a crashing program, or one that's ending, waits for its last logs to go out.
_EXIT_FLUSH_TIMEOUT = 2.0


class LogLevel(str, enum.Enum):
    """How bad a log is."""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


LevelLike = Union[LogLevel, str]

_ALIASES = {"warning": "warn", "critical": "fatal", "exception": "error", "trace": "debug"}


def level_of(level: LevelLike) -> LogLevel:
    name = level.value if isinstance(level, LogLevel) else str(level).strip().lower()
    try:
        return LogLevel(_ALIASES.get(name, name))
    except ValueError:
        return LogLevel.INFO


class _Setup:
    """One running configuration."""

    def __init__(self, service: str, environment: Optional[str], app_version: Optional[str],
                 source: Optional[str], pipeline: Pipeline) -> None:
        self.service = service
        self.environment = environment
        self.app_version = app_version
        self.source = source
        self.pipeline = pipeline


_setup: Optional[_Setup] = None
_session_id = str(uuid.uuid4())
_extra_context: Dict[str, Any] = {}
_runtime_context: Optional[Dict[str, Any]] = None
_hooks_installed = False
_previous_excepthook = None
_previous_threading_excepthook = None
_handling = threading.local()


def init(
    api_key: Optional[str] = None,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    app_version: Optional[str] = None,
    source: Optional[str] = None,
    endpoint: Optional[str] = None,
    flush_interval: float = 5.0,
    max_batch_size: int = 50,
    capture_exceptions: bool = True,
    capture_logging: Optional[Union[bool, str, int]] = None,
) -> None:
    """Start sending logs. Call once, as early as possible.

    Anything left out is read from the environment: ``ZHARP_API_KEY``, ``ZHARP_SERVICE`` (then
    ``OTEL_SERVICE_NAME``), ``ZHARP_ENV``, ``ZHARP_VERSION``, ``ZHARP_SOURCE``, ``ZHARP_ENDPOINT``,
    ``ZHARP_CAPTURE_LOGGING``. With no API key anywhere, nothing starts and a hint is printed.
    ``ZHARP_DISABLED=true`` turns this off.

    On success it prints one line to stderr:
    ``[Zharp] Logging active — service "checkout-api", environment "production"``.

    Calling it again replaces the running setup; what the old one had queued is still sent.

    :param service: the name logs are filed under. Defaults to the ``name`` in the app's
        ``pyproject.toml`` when one can be found, else ``unknown-service``.
    :param environment: tag every log carries, e.g. ``production``. Not guessed when unset.
    :param capture_exceptions: send uncaught exceptions, in the main thread and others, with
        their traceback (on by default). They're still printed as before.
    :param capture_logging: also send what goes through Python's ``logging`` — ``True`` for
        ``INFO`` and up, or a level such as ``"WARNING"``. Off by default.
    """
    global _setup
    previous = _setup
    if previous is not None:
        _setup = None
        threading.Thread(target=previous.pipeline.close, name="zharp-logs-close", daemon=True).start()

    if _env.disabled_by_env():
        _env.announce("Logging disabled by ZHARP_DISABLED — nothing will be sent")
        return

    key = _env.pick(api_key, _env.env_value("ZHARP_API_KEY"))
    if key is None:
        _env.announce("No API key found — pass api_key to zharp_logs.init(), or set ZHARP_API_KEY")
        return

    name = _env.pick(service, _env.env_value("ZHARP_SERVICE"), _env.env_value("OTEL_SERVICE_NAME"), _env.project_name())
    if name is None:
        # A warning, not an error: a missing name shouldn't stop the app, and the logs are still
        # worth having under a placeholder.
        _env.announce("No service name — pass service to zharp_logs.init() or set ZHARP_SERVICE. Using 'unknown-service'.")
        name = "unknown-service"

    transport = Transport(key, normalize_endpoint(_env.pick(endpoint, _env.env_value("ZHARP_ENDPOINT"))))
    setup = _Setup(
        service=name,
        environment=_env.pick(environment, _env.env_value("ZHARP_ENV")),
        app_version=_env.pick(app_version, _env.env_value("ZHARP_VERSION")),
        source=_env.pick(source, _env.env_value("ZHARP_SOURCE")),
        pipeline=Pipeline(transport, max(0.1, float(flush_interval)), max(1, int(max_batch_size))),
    )
    _setup = setup
    _install_exit_hook()
    if capture_exceptions:
        _install_exception_hooks()

    if capture_logging is None:
        capture_logging = _env.env_value("ZHARP_CAPTURE_LOGGING")
        if _env.flag_of(capture_logging) is not None:
            capture_logging = _env.flag_of(capture_logging)
    captured = ""
    if capture_logging not in (None, False, ""):
        from ._handler import attach_to_root
        level = attach_to_root(capture_logging)
        captured = ", capturing logging from " + level

    _env.announce("Logging active — " + _env.describe_target(setup.service, setup.environment) + captured)


def is_active() -> bool:
    """Whether :func:`init` succeeded and logs are being sent."""
    return _setup is not None


def session_id() -> str:
    """The ID every log of this run carries, so one run's logs can be found together."""
    return _session_id


def reset_session_id() -> None:
    """Start a new session ID, e.g. per job in a long-running worker."""
    global _session_id
    _session_id = str(uuid.uuid4())


def set_context(context: Optional[Mapping[str, Any]]) -> None:
    """Extra fields added to every log's ``context``, e.g. a deployment's region. Replaces what was
    set before; pass ``{}`` to clear it."""
    global _extra_context
    _extra_context = _message.to_json_map(context)


def log(level: LevelLike, message: Any, metadata: Optional[Mapping[str, Any]] = None, *,
        exc: Optional[BaseException] = None, service: Optional[str] = None) -> None:
    """Log at ``level``. An exception — as the message, ``exc``, or a value in ``metadata`` — brings
    its type and traceback. ``service`` files this one log under another service than the one set
    in :func:`init`. Before :func:`init` (or after a failed one) logs are dropped."""
    setup = _setup
    if setup is None:
        return
    try:
        lvl = level_of(level)
        if exc is None and isinstance(message, BaseException):
            exc = message
        if exc is None:
            exc = _message.first_error(metadata)
        entry = build_entry(setup, lvl, message, metadata, exc, service, time.time())
        setup.pipeline.push(entry, urgent=lvl in (LogLevel.ERROR, LogLevel.FATAL))
    except Exception:
        # Logging never takes the app down.
        pass


def capture_exception(error: Optional[BaseException] = None, metadata: Optional[Mapping[str, Any]] = None,
                      *, fatal: bool = False) -> None:
    """Log an exception with its traceback, as an ``error`` (or ``fatal``). Without an argument,
    the one being handled — call it in an ``except`` block."""
    if error is None:
        error = sys.exc_info()[1]
        if error is None:
            return
    log(LogLevel.FATAL if fatal else LogLevel.ERROR, error, metadata, exc=error)


def flush(timeout: Optional[float] = 5.0) -> bool:
    """Send everything queued and wait up to ``timeout`` seconds for it. Normally not needed: logs
    go out every few seconds, errors at once, and everything when the program exits."""
    setup = _setup
    return setup.pipeline.flush(timeout) if setup is not None else True


def close(timeout: Optional[float] = 5.0) -> bool:
    """Send everything queued and stop. Logs after this are dropped until :func:`init` again."""
    global _setup
    setup = _setup
    _setup = None
    return setup.pipeline.close(timeout) if setup is not None else True


def build_entry(setup: _Setup, level: LogLevel, message: Any, metadata: Optional[Mapping[str, Any]],
                exc: Optional[BaseException], service: Optional[str], timestamp: float) -> Dict[str, Any]:
    """The entry as Zharp takes it — the same shape every Zharp SDK sends."""
    entry: Dict[str, Any] = {
        "service": service.strip() if service and service.strip() else setup.service,
        "level": level.value,
        "message": _message.to_message(message),
        "timestamp": _message.iso_now(timestamp),
    }
    # The tags the log viewer filters by, top level where the backend reads them; absent rather
    # than empty when not configured.
    if setup.environment:
        entry["environment"] = setup.environment
    if setup.app_version:
        entry["version"] = setup.app_version
    if setup.source:
        entry["source"] = setup.source

    meta: Dict[str, Any] = {}
    if exc is not None:
        # Under the keys the log viewer shows; a stack the caller gave wins.
        meta["name"] = _message.type_name(exc)
        meta["stack"] = _message.stack_of(exc)
    meta.update(_message.to_json_map(metadata))
    meta["sessionId"] = _session_id
    if setup.environment:
        meta["environment"] = setup.environment
    context = dict(_runtime())
    context.update(_extra_context)
    if setup.app_version:
        context["appVersion"] = setup.app_version
    meta["context"] = context
    entry["metadata"] = meta
    return entry


def _runtime() -> Dict[str, Any]:
    """What is known about where the program runs. Captured once — except the pid, which a fork
    changes."""
    global _runtime_context
    if _runtime_context is None:
        context: Dict[str, Any] = {}
        try:
            context["hostname"] = _env.env_value("HOSTNAME") or socket.gethostname()
        except Exception:
            pass
        context["pythonVersion"] = platform.python_version()
        context["platform"] = sys.platform
        try:
            context["osVersion"] = platform.platform()
        except Exception:
            pass
        _runtime_context = context
    return dict(_runtime_context, pid=str(os.getpid()))


def _install_exit_hook() -> None:
    global _exit_hook_installed
    if globals().get("_exit_hook_installed"):
        return
    _exit_hook_installed = True

    def send_what_is_left() -> None:
        setup = _setup
        if setup is not None:
            setup.pipeline.flush(_EXIT_FLUSH_TIMEOUT)

    atexit.register(send_what_is_left)


def _install_exception_hooks() -> None:
    """Uncaught exceptions are logged with their traceback and sent, then the hook that was there
    before runs — Python still prints the traceback and the program still ends as it would have."""
    global _hooks_installed, _previous_excepthook, _previous_threading_excepthook
    if _hooks_installed:
        return
    _hooks_installed = True
    _previous_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if exc_value is not None and not issubclass(exc_type, KeyboardInterrupt):
            _report(exc_value, LogLevel.FATAL, {"type": "uncaughtException"})
            setup = _setup
            if setup is not None:
                setup.pipeline.flush(_EXIT_FLUSH_TIMEOUT)
        (_previous_excepthook or sys.__excepthook__)(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    if hasattr(threading, "excepthook"):
        _previous_threading_excepthook = threading.excepthook

        def threading_excepthook(args):  # type: ignore[no-untyped-def]
            if args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
                thread = args.thread.name if args.thread is not None else None
                # A thread dying doesn't end the program: an error, not fatal.
                _report(args.exc_value, LogLevel.ERROR, {"type": "uncaughtException", "thread": thread})
            (_previous_threading_excepthook or threading.__excepthook__)(args)

        threading.excepthook = threading_excepthook


def _report(error: BaseException, level: LogLevel, metadata: Dict[str, Any]) -> None:
    # Reporting a crash must never crash again, or report itself.
    if getattr(_handling, "active", False):
        return
    _handling.active = True
    try:
        log(level, error, metadata, exc=error)
    finally:
        _handling.active = False


def _reset_for_tests() -> None:
    global _setup, _extra_context, _session_id
    _setup = None
    _extra_context = {}
    _session_id = str(uuid.uuid4())
