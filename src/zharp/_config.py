"""Options from init() and the environment, and the startup line."""

import os
import re
import sys
from dataclasses import dataclass
from typing import Optional

DEFAULT_ENDPOINT = "https://api.zharp.io/api/v1/otlp"

_TRUE = re.compile(r"^(true|1|yes|on)$", re.IGNORECASE)
_FALSE = re.compile(r"^(false|0|no|off|none)$", re.IGNORECASE)


@dataclass
class Options:
    key: str
    service: str
    environment: Optional[str]
    release: Optional[str]
    endpoint: str
    sample_rate: float
    #: The lowest ``logging`` level sent; None leaves ``logging`` alone.
    logging_level: Optional[int]
    capture_uncaught: bool
    instrument: bool
    #: False: logs only — no spans, no client/database instrumentation, nothing injected.
    tracing: bool = True


def env_value(name: str) -> Optional[str]:
    """An environment variable; None when unset or blank."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def pick(*values: Optional[str]) -> Optional[str]:
    """The first value that is non-blank text."""
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def flag_of(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    if _TRUE.match(value):
        return True
    if _FALSE.match(value):
        return False
    return None


def disabled_by_env() -> bool:
    """``ZHARP_DISABLED=true``: start nothing, whatever the code asks for."""
    return flag_of(env_value("ZHARP_DISABLED")) is True


def project_name() -> Optional[str]:
    """The app's name from the ``pyproject.toml`` above the script that was started, if any."""
    try:
        start = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else os.getcwd()
        directory = start if os.path.isdir(start) else os.path.dirname(start)
        for _ in range(8):
            candidate = os.path.join(directory, "pyproject.toml")
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as f:
                    text = f.read()
                section = re.search(r"^\[(project|tool\.poetry)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
                if section:
                    match = re.search(r"^name\s*=\s*[\"']([^\"']+)[\"']", section.group(2), re.M)
                    if match:
                        return match.group(1)
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    except Exception:
        pass
    return None


def logging_level_of(value) -> Optional[int]:
    """``"INFO"``, ``logging.INFO``, False/"off" (None: leave ``logging`` alone)."""
    import logging

    if value is None or value is False:
        return None
    if value is True:
        return logging.INFO
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if flag_of(text) is False:
        return None
    level = logging.getLevelName("WARNING" if text == "WARN" else text)
    return level if isinstance(level, int) else logging.INFO


def resolve(
    key: Optional[str],
    service: Optional[str],
    environment: Optional[str],
    release: Optional[str],
    endpoint: Optional[str],
    sample_rate: Optional[float],
    logging_level,
    capture_uncaught: bool,
    instrument: bool,
    tracing: Optional[bool] = None,
) -> Optional[Options]:
    """The options, explicit values first, then ``ZHARP_*``. None when there is no key."""
    resolved_key = pick(key, env_value("ZHARP_KEY"), env_value("ZHARP_API_KEY"))
    if not resolved_key:
        return None
    rate = sample_rate
    if rate is None:
        try:
            rate = float(env_value("ZHARP_SAMPLE_RATE") or 1.0)
        except ValueError:
            rate = 1.0
    if logging_level == "env":
        logging_level = env_value("ZHARP_LOGGING_LEVEL") or "INFO"
    return Options(
        key=resolved_key,
        service=pick(service, env_value("ZHARP_SERVICE"), env_value("OTEL_SERVICE_NAME"), project_name())
        or "unknown-service",
        environment=pick(environment, env_value("ZHARP_ENVIRONMENT"), env_value("ZHARP_ENV")),
        release=pick(release, env_value("ZHARP_RELEASE"), env_value("ZHARP_VERSION")),
        endpoint=(pick(endpoint, env_value("ZHARP_ENDPOINT")) or DEFAULT_ENDPOINT).rstrip("/"),
        sample_rate=min(1.0, max(0.0, rate)),
        logging_level=logging_level_of(logging_level),
        capture_uncaught=capture_uncaught,
        instrument=instrument,
        tracing=tracing_of(tracing),
    )


def tracing_of(value: Optional[bool]) -> bool:
    """``tracing=`` when passed, then ``ZHARP_TRACING``; on by default."""
    if value is not None:
        return bool(value)
    return flag_of(env_value("ZHARP_TRACING")) is not False


def announce(message: str) -> None:
    """One line on stderr, prefixed so it can be searched for."""
    line = "[Zharp] " + message + "\n"
    try:
        sys.stderr.write(line)
    except UnicodeEncodeError:
        try:
            sys.stderr.write(line.replace("—", "-"))
        except Exception:
            pass
    except Exception:
        pass
