"""Request and response headers on incoming requests' spans.

OpenTelemetry's Django, Flask and FastAPI (ASGI/WSGI) instrumentations record headers only when
asked through environment variables; ``init()`` asks for all of them, server side, unless the app
already set those variables itself. Secret headers — authorization, cookies, API keys, tokens,
sessions — are recorded as ``[REDACTED]``: you see one was sent, never its value. The variables
are read when an instrumentation is set up, so this runs before ``instrument_installed``.
"""

import os
from typing import Any, List, Optional

SERVER_REQUEST = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST"
SERVER_RESPONSE = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE"
SANITIZE = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"

#: Header names (regular expressions, searched case-insensitively) whose values are never sent.
SECRET_HEADERS = (
    "authorization", "cookie", "password", "passwd", "secret", "token", "api-?key", "access-?key",
    "private-?key", "credential", "signature", "session", "csrf", "xsrf", "jwt", "^auth$", "^sid$",
)


def header_settings(option: Any, env: Optional[str]) -> Optional[List[str]]:
    """The secret-header patterns to use, or None when header capture is off.

    ``capture_headers=False`` (or ``ZHARP_CAPTURE_HEADERS=false``) turns it off;
    ``capture_headers={"redact": ["x-tenant"]}`` filters more names."""
    if option is False or (env or "").strip().lower() in ("false", "0", "no", "off"):
        return None
    extra = list(option.get("redact") or []) if isinstance(option, dict) else []
    return list(SECRET_HEADERS) + [str(p) for p in extra]


def apply(patterns: Optional[List[str]]) -> None:
    """Ask the server instrumentations for every header, secrets redacted. What the app set wins."""
    if patterns is None:
        return
    os.environ.setdefault(SERVER_REQUEST, ".*")
    os.environ.setdefault(SERVER_RESPONSE, ".*")
    if SANITIZE in os.environ:
        # The app's own list, plus Zharp's: a secret is never sent because a list was shorter.
        os.environ[SANITIZE] = ",".join([os.environ[SANITIZE]] + patterns)
    else:
        os.environ[SANITIZE] = ",".join(patterns)
