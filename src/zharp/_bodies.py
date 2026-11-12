"""Request and response bodies on HTTP spans (``capture_bodies``), and secrets in URLs.

A port of the rules every Zharp SDK shares (``@zharp/internal/bodies`` in the JavaScript SDKs),
so a payload looks the same whichever SDK sent it:

- field names stay; values under keys that look secret (``password``, ``token``,
  ``authorization``, ``card`` …) become ``[Filtered]``, and an object under such a key keeps its
  keys with every value filtered
- an inline image or other long base64 value becomes ``[image/png · 24 KB]``, a file in a form
  upload ``[file photo.png · image/png · 240 KB]``, and a body that isn't text at all
  ``[binary · application/pdf · 1.2 MB]`` or ``[event stream]``
- text is capped (32 KB by default), with ``http.*.body.truncated`` set when it was cut

The bodies travel as the span attributes ``http.request.body`` and ``http.response.body``.
"""

import json
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple, Union
from urllib.parse import unquote, urlsplit

DEFAULT_MAX_BODY = 32 * 1024
MAX_BODY = 128 * 1024
FILTERED = "[Filtered]"

REQUEST_BODY = "http.request.body"
REQUEST_TRUNCATED = "http.request.body.truncated"
REQUEST_TYPE = "http.request.header.content-type"
RESPONSE_BODY = "http.response.body"
RESPONSE_TRUNCATED = "http.response.body.truncated"
RESPONSE_TYPE = "http.response.header.content-type"

_ATTRS = {
    "request": (REQUEST_BODY, REQUEST_TRUNCATED, REQUEST_TYPE),
    "response": (RESPONSE_BODY, RESPONSE_TRUNCATED, RESPONSE_TYPE),
}

KeyPattern = Union[str, Pattern]


@dataclass
class CaptureBodies:
    """``zharp.init(capture_bodies=CaptureBodies(mode="all", redact_keys=["date_of_birth"]))``.

    :param mode: ``"errors"`` (default): requests that failed (status 400 and above, or no
        response). ``"all"``: every request.
    :param max_size: characters kept per body. Default 32768 (32 KB), at most 131072 (128 KB).
    :param redact_keys: more keys whose values become ``[Filtered]``, on top of the built-in ones.
        A string matches a key ignoring case, ``-`` and ``_``; a compiled regex is searched in the
        key as it is.
    :param urls: which requests have their bodies captured: a bare host (``"api.example.com"``)
        matches that host, a string with a scheme matches URLs starting with it, a compiled regex
        is searched in the full URL. Default: every request.
    """

    mode: str = "errors"
    max_size: int = DEFAULT_MAX_BODY
    redact_keys: Sequence[KeyPattern] = ()
    urls: Optional[Sequence[KeyPattern]] = None


@dataclass
class BodySettings:
    mode: str
    max_size: int
    extra_keys: List[KeyPattern] = field(default_factory=list)
    urls: Optional[List[KeyPattern]] = None


def body_settings(option: Any = None, env: Optional[str] = None) -> Optional[BodySettings]:
    """The setting as one shape, or None when bodies are off.

    ``option``: True, ``"errors"``, ``"all"``, a dict or a :class:`CaptureBodies`; ``env`` is
    ``ZHARP_CAPTURE_BODIES`` (``errors``, ``all``, ``off``), used when no option is passed.
    """
    value = option
    if value is None and env:
        text = env.strip().lower()
        if text in ("errors", "all"):
            value = text
        elif re.match(r"^(true|1|yes|on)$", text):
            value = True
    if value is None or value is False:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "off", "false", "no", "none", "0"):
            return None
        value = CaptureBodies(mode=text)
    elif value is True:
        value = CaptureBodies()
    elif isinstance(value, dict):
        known = ("mode", "max_size", "redact_keys", "urls")
        value = CaptureBodies(**{k: v for k, v in value.items() if k in known})
    if not isinstance(value, CaptureBodies):
        return None
    size = value.max_size
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
        size = min(int(math.floor(size)), MAX_BODY)
    else:
        size = DEFAULT_MAX_BODY
    return BodySettings(
        mode="all" if value.mode == "all" else "errors",
        max_size=size,
        extra_keys=list(value.redact_keys or ()),
        urls=list(value.urls) if value.urls is not None else None,
    )


def raw_limit(settings: BodySettings) -> int:
    """Raw text read before shortening and capping: inline images shrink to a few words."""
    return max(64 * 1024, settings.max_size * 2)


def wanted(settings: BodySettings, status: Optional[int]) -> bool:
    """Whether a request with this outcome has its bodies kept. A missing status means no response."""
    return settings.mode == "all" or not status or status >= 400


# ─── content types ──────────────────────────────────────────────────────────────


def _mime(content_type: Optional[str]) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def is_event_stream(content_type: Optional[str]) -> bool:
    return _mime(content_type) == "text/event-stream"


def is_multipart(content_type: Optional[str]) -> bool:
    return _mime(content_type) == "multipart/form-data"


_TEXT_APPLICATION = re.compile(r"^application/(json|xml|x-www-form-urlencoded|graphql|javascript|x-ndjson|ld\+json)$")


def is_textual(content_type: Optional[str]) -> bool:
    """Whether a body of this type is text worth showing. An unknown type is given the benefit of the doubt."""
    kind = _mime(content_type)
    if not kind:
        return True
    if kind == "text/event-stream":
        return False
    return (
        kind.startswith("text/")
        or kind.endswith("+json")
        or kind.endswith("+xml")
        or bool(_TEXT_APPLICATION.match(kind))
    )


def _is_form(content_type: Optional[str]) -> bool:
    return _mime(content_type) == "application/x-www-form-urlencoded"


def _js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


def format_size(size: int) -> str:
    """``240 KB``, ``1.4 MB``."""
    if size < 1024:
        return "{} B".format(size)
    if size < 1024 * 1024:
        return "{} KB".format(_js_round(size / 1024))
    exact = Decimal(size / (1024 * 1024)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return "{} MB".format(exact)


def file_label(name: Optional[str], content_type: Optional[str], size: Optional[int], partial: bool = False) -> str:
    """How a file in a form upload is shown: ``[file photo.png · image/png · 240 KB]``."""
    parts = ["file " + (name or "blob")]
    if content_type:
        parts.append(content_type)
    if size is not None:
        parts.append(format_size(size) + ("+" if partial else ""))
    return "[" + " · ".join(parts) + "]"


def describe_body(content_type: Optional[str], size: Optional[int] = None, partial: bool = False) -> str:
    """What a body that isn't text says instead: ``[binary · image/png · 24 KB]``, ``[event stream]``."""
    if is_event_stream(content_type):
        return "[event stream]"
    parts = ["binary"]
    kind = _mime(content_type)
    if kind:
        parts.append(kind)
    if size is not None and size > 0:
        parts.append(format_size(size) + ("+" if partial else ""))
    return "[" + " · ".join(parts) + "]"


# ─── redaction ──────────────────────────────────────────────────────────────────

#: Built-in keys whose values never leave the process, compared without case, ``-`` or ``_``.
_CONTAINS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey", "privatekey",
    "authorization", "cookie", "creditcard", "cardnumber", "cvv", "cvc", "ssn", "iban", "sessionid",
    "signature", "credential",
)
_EXACT = ("auth", "pin", "pwd", "otp", "card", "pan", "jwt", "sid", "codeverifier")
#: Also secret in a query string, where they are API keys and signed-URL signatures.
_URL_EXACT = ("key", "sig", "code", "state", "session")

_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def _normalize(key: str) -> str:
    return _NOT_ALNUM.sub("", key.lower())


def is_secret_key(key: str, extra: Sequence[KeyPattern] = ()) -> bool:
    k = _normalize(key)
    if not k:
        return False
    if k in _EXACT or any(c in k for c in _CONTAINS):
        return True
    for pattern in extra:
        if isinstance(pattern, str):
            if _normalize(pattern) == k:
                return True
        elif pattern.search(key):
            return True
    return False


_DATA_URI = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(?:;[\w=.+-]+)*;base64,([A-Za-z0-9+/=\s]*)\Z", re.ASCII)
_BASE64 = re.compile(r"^[A-Za-z0-9+/_-]{256,}={0,2}\Z")
_HAS_B64_SIGN = re.compile(r"[0-9+/]")


def shorten(value: str) -> str:
    """A long base64 value as a short description; other strings as they are."""
    if len(value) < 256:
        return value
    uri = _DATA_URI.match(value)
    if uri:
        return "[{} · {}]".format(uri.group(1) or "data", format_size(len(uri.group(2)) * 3 // 4))
    if _BASE64.match(value) and _HAS_B64_SIGN.search(value):
        return "[base64 · {}]".format(format_size(len(value) * 3 // 4))
    return value


def _filter_all(value: Any, depth: int) -> Any:
    """Every value under ``value`` filtered, its keys kept."""
    if depth > 20:
        return FILTERED
    if value is None or value == "":
        return value
    if isinstance(value, list):
        return [_filter_all(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _filter_all(v, depth + 1) for k, v in value.items()}
    return FILTERED


def redact_value(value: Any, extra: Sequence[KeyPattern], depth: int = 0) -> Any:
    if isinstance(value, str):
        return shorten(value)
    if depth > 20 or value is None or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        return [redact_value(v, extra, depth + 1) for v in value]
    return {
        k: (_filter_all(v, depth + 1) if is_secret_key(k, extra) else redact_value(v, extra, depth + 1))
        for k, v in value.items()
    }


_RUNS = re.compile(r'"((?:data:[\w.+-]+/[\w.+-]+(?:;[\w=.+-]+)*;base64,)?[A-Za-z0-9+/_=-]{256,})("?)', re.ASCII)
_PAIRS = re.compile(r'("([^"\\]{1,100})"\s*:\s*)("(?:[^"\\]|\\.)*"?|-?\d[\d.eE+-]*|true|false)', re.ASCII)


def _redact_loose(text: str, extra: Sequence[KeyPattern]) -> str:
    """Text that didn't parse: secret ``"key": value`` pairs filtered, long base64 runs shortened."""

    def run(match: "re.Match") -> str:
        raw, close = match.group(1), match.group(2)
        short = shorten(raw)
        if short == raw:
            return match.group(0)
        return '"' + short + '"' if close else '"' + short[:-1] + '+]"'

    def pair(match: "re.Match") -> str:
        return match.group(1) + '"' + FILTERED + '"' if is_secret_key(match.group(2), extra) else match.group(0)

    return _PAIRS.sub(pair, _RUNS.sub(run, text))


def _decode_component(text: str) -> str:
    """``decodeURIComponent`` with ``+`` as a space; raises where it would."""
    return unquote(text.replace("+", " "), errors="strict")


def _redact_form(text: str, extra: Sequence[KeyPattern]) -> str:
    out = []
    for pair in text.split("&"):
        eq = pair.find("=")
        if eq < 0:
            out.append(pair)
            continue
        key = pair[:eq]
        try:
            key = _decode_component(key)
        except (UnicodeDecodeError, ValueError):
            pass
        if is_secret_key(key, extra):
            out.append(pair[:eq] + "=" + FILTERED)
            continue
        value = pair[eq + 1 :]
        out.append(pair[:eq] + "=" + _shorten_encoded(value) if len(value) >= 256 else pair)
    return "&".join(out)


def _shorten_encoded(value: str) -> str:
    try:
        decoded = _decode_component(value)
    except (UnicodeDecodeError, ValueError):
        return value
    short = shorten(decoded)
    return value if short == decoded else short


# ─── JSON as JavaScript writes it ──────────────────────────────────────────────


class _NotJson(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise _NotJson(name)


def _js_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):
        return "null"
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" not in text:
        return text
    mantissa, exponent = text.split("e")
    exp = int(exponent)
    if -7 < exp < 21:
        return format(Decimal(text), "f")
    return "{}e{}{}".format(mantissa, "+" if exp > 0 else "-", abs(exp))


def stringify(value: Any) -> str:
    """``JSON.stringify(value)``: no spaces, numbers and escapes as JavaScript writes them."""
    if value is None:
        return "null"
    if isinstance(value, (bool, int, float)):
        return _js_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + stringify(v) for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stringify(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


#: What ``String.prototype.trim`` removes.
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _utf16_slice(text: str, limit: int) -> str:
    """The first ``limit`` UTF-16 units, as JavaScript's ``slice`` counts (a split pair is dropped)."""
    if len(text) <= limit // 2:
        return text
    count = 0
    for index, ch in enumerate(text):
        count += 2 if ord(ch) > 0xFFFF else 1
        if count > limit:
            return text[:index]
    return text


# ─── the attribute value ───────────────────────────────────────────────────────


def format_body(
    text: Optional[str], content_type: Optional[str], settings: BodySettings, cut: bool = False
) -> Optional[Tuple[str, bool]]:
    """A body as it is sent: redacted, then cut to ``max_size``. ``cut`` says the text was already
    cut short where it was read. None for an empty body. A ``multipart/form-data`` body is passed
    as the JSON of its fields (files already described). Returns (value, truncated)."""
    if text is None:
        return None
    body = str(text)
    trimmed = body.strip(_JS_SPACE)
    if not trimmed:
        return None

    if _is_form(content_type):
        body = _redact_form(body, settings.extra_keys)
    else:
        parsed: Any = None
        ok = False
        if not cut and trimmed[0] in "{[":
            try:
                parsed = json.loads(trimmed, parse_constant=_reject_constant)
                ok = True
            except (ValueError, RecursionError):
                pass
        body = stringify(redact_value(parsed, settings.extra_keys)) if ok else _redact_loose(body, settings.extra_keys)

    length = _utf16_len(body)
    truncated = cut or length > settings.max_size
    return (_utf16_slice(body, settings.max_size) if length > settings.max_size else body), truncated


def body_attributes(
    side: str, text: Optional[str], content_type: Optional[str], settings: BodySettings, cut: bool = False
) -> Dict[str, Any]:
    """The span attributes for one side of a request (``"request"`` or ``"response"``)."""
    if not is_textual(content_type) and not is_multipart(content_type):
        return {}
    formatted = format_body(text, content_type, settings, cut)
    if formatted is None:
        return {}
    value, truncated = formatted
    body_key, truncated_key, type_key = _ATTRS[side]
    attributes: Dict[str, Any] = {body_key: value}
    if truncated:
        attributes[truncated_key] = True
    if content_type:
        attributes[type_key] = content_type
    return attributes


def described_attributes(
    side: str, content_type: Optional[str], size: Optional[int] = None, partial: bool = False
) -> Dict[str, Any]:
    """The attributes for a body that isn't text: what it was, not what it said."""
    body_key, _, type_key = _ATTRS[side]
    attributes: Dict[str, Any] = {body_key: describe_body(content_type, size, partial)}
    if content_type:
        attributes[type_key] = content_type
    return attributes


# ─── URLs ───────────────────────────────────────────────────────────────────────

_USERINFO = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/?#@]*@", re.IGNORECASE)


def redact_url(url: str, extra: Sequence[KeyPattern] = ()) -> str:
    """A URL as it is recorded: secret-looking query values (``token``, ``key``, ``sig``,
    ``password`` …) become ``[Filtered]`` and ``user:password@`` is dropped."""
    if not url:
        return url
    out = _USERINFO.sub(r"\1", url, count=1)
    q = out.find("?")
    if q < 0:
        return out
    hash_at = out.find("#", q)
    query = out[q + 1 : hash_at] if hash_at >= 0 else out[q + 1 :]
    redacted = redact_query(query, extra)
    if redacted != query:
        out = out[: q + 1] + redacted + (out[hash_at:] if hash_at >= 0 else "")
    return out


def redact_query(query: str, extra: Sequence[KeyPattern] = ()) -> str:
    """A query string (without ``?``) with secret-looking values filtered."""
    if not query:
        return query
    out = []
    for pair in query.split("&"):
        eq = pair.find("=")
        if eq < 0:
            out.append(pair)
            continue
        key = pair[:eq]
        try:
            key = _decode_component(key)
        except (UnicodeDecodeError, ValueError):
            pass
        secret = is_secret_key(key, extra) or _normalize(key) in _URL_EXACT
        out.append(pair[:eq] + "=" + FILTERED if secret and eq < len(pair) - 1 else pair)
    return "&".join(out)


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}


def url_matches(url: str, patterns: Sequence[KeyPattern]) -> bool:
    """A bare host (with or without a port) matches that host; a string with a scheme matches as
    a prefix; a compiled regex is searched in the URL."""
    host = hostname = ""
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        port = parts.port
        host = hostname if port is None or _DEFAULT_PORTS.get(parts.scheme.lower()) == port else "{}:{}".format(hostname, port)
    except ValueError:
        pass
    for pattern in patterns:
        if not isinstance(pattern, str):
            if pattern.search(url):
                return True
            continue
        if not pattern:
            continue
        if "://" in pattern:
            if url.startswith(pattern):
                return True
        elif pattern in (host, hostname):
            return True
    return False
