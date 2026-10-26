"""Settings from the environment, and the startup line."""

import os
import re
import sys
from typing import Optional

_TRUE = re.compile(r"^(true|1|yes|on)$", re.IGNORECASE)
_FALSE = re.compile(r"^(false|0|no|off|none)$", re.IGNORECASE)


def env_value(name: str) -> Optional[str]:
    """An environment variable; None when unset or blank."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def pick(*values: Optional[str]) -> Optional[str]:
    """The first value that is non-blank text — explicit option, then each fallback in turn."""
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
    """``ZHARP_DISABLED=true`` (or 1, yes, on): start nothing, whatever the code asks for."""
    return flag_of(env_value("ZHARP_DISABLED")) is True


def project_name() -> Optional[str]:
    """The app's own name, from the ``pyproject.toml`` above the script that was started — the
    name a service gets when none is configured. None when there's none to read."""
    try:
        start = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else os.getcwd()
        directory = start if os.path.isdir(start) else os.path.dirname(start)
        for _ in range(8):
            candidate = os.path.join(directory, "pyproject.toml")
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as f:
                    text = f.read()
                # The name under [project] (or [tool.poetry]), without a TOML parser: 3.8 has none.
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
        # Not run from a file (an interactive shell, a frozen app) or not readable: no name.
        pass
    return None


def describe_target(service: str, environment: Optional[str]) -> str:
    """``service "checkout-api", environment "production"`` — what the startup line reports."""
    env = 'environment "{}"'.format(environment) if environment else "no environment set"
    return 'service "{}", {}'.format(service, env)


def announce(message: str) -> None:
    """Startup output, on stderr. One prefix, so it can be searched for — and left out of capture."""
    line = "[Zharp] " + message + "\n"
    try:
        sys.stderr.write(line)
    except UnicodeEncodeError:
        # A console that can't show the dash (an old Windows code page) gets a plain one.
        try:
            sys.stderr.write(line.replace("—", "-"))
        except Exception:
            pass
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
