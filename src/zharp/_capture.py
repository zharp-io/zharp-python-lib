"""``capture_bodies``: request and response bodies on the HTTP spans of the instrumented web
frameworks (Django, Flask, FastAPI) and HTTP clients (requests, httpx), and secrets masked in
every span's URL.

Nothing changes what the app reads or writes. A request body is seen as the app reads it (the
input stream is teed), a response body as it is produced; only the first bytes up to the raw
limit are kept. What is kept depends on the body:

- text: the first bytes, decoded (and inflated when it was gzip/deflate encoded)
- a form upload (``multipart/form-data``): each text field, and each file as its name, type and
  size — file bytes are counted as they pass, never kept
- anything else (an image, a PDF, an event stream): only its size

The attributes are set on the request's span just before it ends.
"""

import logging
import re
import threading
import zlib
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor

from . import _bodies
from ._bodies import (
    BodySettings,
    body_attributes,
    described_attributes,
    file_label,
    is_multipart,
    is_textual,
    raw_limit,
    redact_query,
    redact_url,
    stringify,
    url_matches,
    wanted,
)

_log = logging.getLogger("zharp")

#: Fields kept from one form upload.
_MAX_FIELDS = 200


# ─── multipart/form-data ────────────────────────────────────────────────────────


class _Part:
    __slots__ = ("name", "filename", "type", "size", "text", "kept")

    def __init__(self, name: str, filename: Optional[str], content_type: Optional[str]):
        self.name = name
        self.filename = filename
        self.type = content_type if filename is not None else None
        self.size = 0
        self.text: list = []
        self.kept = 0


def _parse_part_headers(head: str) -> _Part:
    disposition = re.search(r"^content-disposition:(.*)$", head, re.I | re.M)
    disposition_text = disposition.group(1) if disposition else ""
    name_match = re.search(r'\bname="([^"]*)"', disposition_text, re.I) or re.search(
        r"\bname=([^;\s]+)", disposition_text, re.I
    )
    name = name_match.group(1) if name_match else ""
    filename_match = re.search(r'\bfilename="([^"]*)"', disposition_text, re.I)
    filename = filename_match.group(1) if filename_match else None
    star = re.search(r"\bfilename\*=(?:UTF-8'')?([^;\s]+)", disposition_text, re.I)
    if star:
        from urllib.parse import unquote

        try:
            filename = unquote(star.group(1), errors="strict")
        except (UnicodeDecodeError, ValueError):
            filename = star.group(1)
    content_type = re.search(r"^content-type:\s*(.*)$", head, re.I | re.M)
    return _Part(name, filename, content_type.group(1).strip() if content_type else None)


def boundary_of(content_type: Optional[str]) -> Optional[str]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type or "", re.I)
    return (match.group(1) or match.group(2)) if match else None


class Multipart:
    """Reads a form upload as it streams past: text fields kept (up to ``max_text`` each), files counted."""

    def __init__(self, boundary: str, max_text: int):
        self._buf = b""
        self._state = "start"
        self._first = ("--" + boundary).encode("latin-1")
        self._delimiter = ("\r\n--" + boundary).encode("latin-1")
        self._part: Optional[_Part] = None
        self._count = 0
        self._max_text = max_text
        self.fields: "OrderedDict[str, Any]" = OrderedDict()
        self.cut = False

    def push(self, chunk: bytes) -> None:
        if self._state == "done":
            return
        self._buf = self._buf + chunk if self._buf else bytes(chunk)
        while True:
            if self._state == "start":
                i = self._buf.find(self._first)
                if i < 0:
                    if len(self._buf) > 64 * 1024:
                        self._state = "done"
                    return
                self._buf = self._buf[i + len(self._first) :]
                self._state = "after"
            if self._state == "after":
                if len(self._buf) < 2:
                    return
                if self._buf[:2] == b"--":
                    self._state = "done"
                    self._buf = b""
                    return
                if self._buf[:2] == b"\r\n":
                    self._buf = self._buf[2:]
                self._state = "headers"
            if self._state == "headers":
                i = self._buf.find(b"\r\n\r\n")
                if i < 0:
                    if len(self._buf) > 16 * 1024:
                        self._state = "done"
                    return
                self._part = _parse_part_headers(self._buf[:i].decode("utf-8", "replace"))
                self._buf = self._buf[i + 4 :]
                self._state = "body"
            if self._state == "body":
                i = self._buf.find(self._delimiter)
                if i >= 0:
                    self._consume(self._buf[:i])
                    self._finish(False)
                    self._buf = self._buf[i + len(self._delimiter) :]
                    self._state = "after"
                    if self._count >= _MAX_FIELDS:
                        self._state = "done"
                        self.cut = True
                        return
                    continue
                # Keep what could be the start of the delimiter; the rest belongs to this part.
                keep = len(self._delimiter) - 1
                if len(self._buf) > keep:
                    self._consume(self._buf[: len(self._buf) - keep])
                    self._buf = self._buf[len(self._buf) - keep :]
                return
            return

    def _consume(self, data: bytes) -> None:
        part = self._part
        if part is None or not data:
            return
        part.size += len(data)
        if part.filename is not None:
            return  # a file: counted, never kept
        room = self._max_text - part.kept
        if room > 0:
            piece = data[:room]
            part.text.append(piece)
            part.kept += len(piece)

    def _finish(self, partial: bool) -> None:
        part = self._part
        self._part = None
        if part is None:
            return
        if part.filename is not None:
            value: Any = file_label(part.filename, part.type, part.size, partial)
        else:
            value = _utf8(b"".join(part.text))
            if part.size > part.kept or partial:
                self.cut = True
        existing = self.fields.get(part.name)
        if part.name not in self.fields:
            self.fields[part.name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            self.fields[part.name] = [existing, value]
        self._count += 1

    def result(self):
        """(json of the fields so far, cut) — including a part the stream stopped inside."""
        if self._part is not None:
            self._consume(self._buf if self._state == "body" else b"")
            self._buf = b""
            self._finish(True)
            self.cut = True
        return stringify(self.fields), self.cut


# ─── taps ───────────────────────────────────────────────────────────────────────


def _utf8(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    # A multi-byte character split at the cap decodes as U+FFFD; drop it.
    return text[:-1] if text.endswith("�") else text


def _inflate(raw: bytes, encoding: Optional[str]) -> Optional[bytes]:
    """The bytes, inflated if they were compressed (a cut-off stream inflates up to where it
    stops); None when they can't be read."""
    enc = (encoding or "").strip().lower()
    if not enc or enc == "identity":
        return raw
    try:
        if enc in ("gzip", "x-gzip"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompressobj(zlib.MAX_WBITS).decompress(raw)
            except zlib.error:
                return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)
        if enc == "br":
            try:
                import brotli  # type: ignore[import-not-found]
            except ImportError:
                return None
            return brotli.decompress(raw)
    except Exception:
        return None
    return None


class Tap:
    """Keeps what passes through one side of a request, depending on what kind of body it is."""

    def __init__(self, settings: BodySettings, type_of: Callable[[], Optional[str]]):
        self._settings = settings
        self._type_of = type_of
        self._chunks: list = []
        self._kept = 0
        #: Every byte seen, kept or not.
        self.total = 0
        self.cut = False
        self._mode: Optional[str] = None
        self.multipart: Optional[Multipart] = None

    def add(self, chunk: Any) -> None:
        if chunk is None:
            return
        if isinstance(chunk, str):
            data = chunk.encode("utf-8")
        elif isinstance(chunk, (bytes, bytearray, memoryview)):
            data = bytes(chunk)
        else:
            return
        if not data:
            return
        self.total += len(data)
        if self._mode is None:
            kind = self._type_of()
            boundary = boundary_of(kind) if is_multipart(kind) else None
            if boundary:
                self._mode = "multipart"
                self.multipart = Multipart(boundary, self._settings.max_size)
            else:
                self._mode = "text" if is_textual(kind) else "count"
        if self._mode == "multipart":
            self.multipart.push(data)  # type: ignore[union-attr]
            return
        if self._mode == "count" or self.cut:
            return
        room = raw_limit(self._settings) - self._kept
        if len(data) > room:
            self.cut = True
            data = data[: max(0, room)]
        if data:
            self._chunks.append(data)
            self._kept += len(data)

    def text(self, encoding: Optional[str]) -> Optional[str]:
        raw = _inflate(b"".join(self._chunks), encoding)
        return None if raw is None else _utf8(raw)


def side_attributes(
    side: str, tap: Optional[Tap], content_type: Optional[str], encoding: Optional[str], settings: BodySettings
) -> Dict[str, Any]:
    """The attributes for one side, whatever kind of body it was."""
    if tap is None or not tap.total:
        return {}
    if tap.multipart is not None:
        fields, cut = tap.multipart.result()
        return body_attributes(side, fields, content_type, settings, cut)
    if not is_textual(content_type):
        return described_attributes(side, content_type, tap.total)
    text = tap.text(encoding)
    if text is None:
        return described_attributes(side, content_type, tap.total)
    return body_attributes(side, text, content_type, settings, tap.cut)


def unread_attributes(side: str, content_type: Optional[str], size: Optional[int]) -> Dict[str, Any]:
    """A body that was never read (a streamed response the app reads after the span ends): text
    is ``[stream · application/json · 60 B]``, anything else described as usual."""
    if is_textual(content_type):
        parts = ["stream"]
        kind = (content_type or "").split(";")[0].strip().lower()
        if kind:
            parts.append(kind)
        if size:
            parts.append(_bodies.format_size(size))
        attributes: Dict[str, Any] = {_bodies.REQUEST_BODY if side == "request" else _bodies.RESPONSE_BODY: "[" + " · ".join(parts) + "]"}
        if content_type:
            attributes[_bodies.REQUEST_TYPE if side == "request" else _bodies.RESPONSE_TYPE] = content_type
        return attributes
    return described_attributes(side, content_type, size)


def bytes_attributes(
    side: str, data: Any, content_type: Optional[str], encoding: Optional[str], settings: BodySettings
) -> Dict[str, Any]:
    """The attributes for a body held in memory (bytes or text)."""
    tap = Tap(settings, lambda: content_type)
    tap.add(data)
    return side_attributes(side, tap, content_type, encoding, settings)


class TeeInput:
    """A WSGI input (or Django request) stream that keeps a copy of what the app reads from it."""

    def __init__(self, stream: Any, tap: Tap):
        self._stream = stream
        self._tap = tap

    def read(self, *args: Any) -> Any:
        data = self._stream.read(*args)
        self._safe_add(data)
        return data

    def readline(self, *args: Any) -> Any:
        data = self._stream.readline(*args)
        self._safe_add(data)
        return data

    def read1(self, *args: Any) -> Any:
        data = self._stream.read1(*args)
        self._safe_add(data)
        return data

    def readinto(self, buffer: Any) -> Any:
        # Werkzeug's LimitedStream (Flask) reads this way.
        count = self._stream.readinto(buffer)
        if count:
            self._safe_add(bytes(memoryview(buffer)[:count]))
        return count

    def readinto1(self, buffer: Any) -> Any:
        count = self._stream.readinto1(buffer)
        if count:
            self._safe_add(bytes(memoryview(buffer)[:count]))
        return count

    def readlines(self, *args: Any) -> Any:
        lines = self._stream.readlines(*args)
        for line in lines:
            self._safe_add(line)
        return lines

    def __iter__(self):
        for line in self._stream:
            self._safe_add(line)
            yield line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _safe_add(self, data: Any) -> None:
        try:
            self._tap.add(data)
        except Exception:
            pass

    def drain(self, content_length: Any, settings: BodySettings) -> None:
        """After the app is done: read what it left unread, up to the raw limit, so a request it
        refused before reading still shows its body. Only with a known length, never past it."""
        try:
            length = int(content_length or 0)
        except (TypeError, ValueError):
            return
        remaining = min(length - self._tap.total, raw_limit(settings) - self._tap.total)
        while remaining > 0:
            data = self._stream.read(min(remaining, 64 * 1024))
            if not data:
                return
            self._safe_add(data)
            remaining -= len(data)


# ─── pending request bodies and URLs, applied as spans end ─────────────────────

_URL_KEYS = ("url.full", "http.url", "http.target")
_STATUS_KEYS = ("http.response.status_code", "http.status_code")


class _Pending:
    """Request bodies of outgoing calls that haven't been answered yet: if the call fails with no
    response, the span still gets its request body when it ends."""

    def __init__(self, limit: int = 1000):
        self._items: "OrderedDict[int, Callable[[], Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()
        self._limit = limit

    def add(self, span: Any, make: Callable[[], Dict[str, Any]]) -> None:
        span_id = _span_id(span)
        if span_id is None:
            return
        with self._lock:
            self._items[span_id] = make
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def pop(self, span: Any) -> Optional[Callable[[], Dict[str, Any]]]:
        span_id = _span_id(span)
        if span_id is None:
            return None
        with self._lock:
            return self._items.pop(span_id, None)


def _span_id(span: Any) -> Optional[int]:
    try:
        context = span.get_span_context()
        return context.span_id if context and context.is_valid else None
    except Exception:
        return None


PENDING = _Pending()


def _url_changes(attributes: Any) -> Dict[str, Any]:
    changes: Dict[str, Any] = {}
    if not attributes:
        return changes
    for key in _URL_KEYS:
        value = attributes.get(key)
        if isinstance(value, str):
            redacted = redact_url(value)
            if redacted != value:
                changes[key] = redacted
    query = attributes.get("url.query")
    if isinstance(query, str):
        bare = query[1:] if query.startswith("?") else query
        redacted = redact_query(bare)
        if redacted != bare:
            changes["url.query"] = ("?" if query.startswith("?") else "") + redacted
    return changes


class UrlRedactionSpanProcessor(SpanProcessor):
    """Secrets in URLs never leave the process: secret-looking query values (``token``, ``key``,
    ``sig``, ``password`` …) in a span's URL attributes become ``[Filtered]`` and
    ``user:password@`` is dropped — on every span, whatever made it, with or without
    ``capture_bodies``. Also gives a failed outgoing call its pending request body."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        try:
            changes = _url_changes(getattr(span, "attributes", None))
            if changes:
                span.set_attributes(changes)
        except Exception:
            pass

    def _on_ending(self, span: Any) -> None:
        # Attributes set after the span started (some instrumentations add the target late). The
        # SDK has frozen them by now, so they are written to its attribute map directly.
        try:
            attributes = getattr(span, "_attributes", None)
            if attributes is None:
                return
            changes = _url_changes(attributes)
            make = PENDING.pop(span)
            if make is not None and not any(attributes.get(key) for key in _STATUS_KEYS):
                try:
                    changes.update(make())
                except Exception:
                    pass
            if not changes:
                return
            immutable = getattr(attributes, "_immutable", False)
            try:
                attributes._immutable = False
                for key, value in changes.items():
                    attributes[key] = value
            finally:
                attributes._immutable = immutable
        except Exception:
            pass

    def on_end(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ─── servers ────────────────────────────────────────────────────────────────────


def _allowed(settings: BodySettings, url: str) -> bool:
    return settings.urls is None or url_matches(url, settings.urls)


def _set(span: Any, attributes: Dict[str, Any]) -> None:
    if attributes and span is not None and span.is_recording():
        span.set_attributes(attributes)


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


_ENVIRON_TEE = "zharp.body.tee"


def _wsgi_url(environ: Dict[str, Any]) -> str:
    try:
        from wsgiref.util import request_uri

        return request_uri(environ)
    except Exception:
        return ""


def flask_hooks(settings: BodySettings):
    """(request_hook for the Flask instrumentation, and the ``request_finished`` receiver)."""

    def request_hook(span: Any, environ: Dict[str, Any]) -> None:
        try:
            if not _allowed(settings, _wsgi_url(environ)):
                return
            stream = environ.get("wsgi.input")
            if stream is None or isinstance(stream, TeeInput):
                return
            tap = Tap(settings, lambda: environ.get("CONTENT_TYPE"))
            tee = TeeInput(stream, tap)
            environ["wsgi.input"] = tee
            environ[_ENVIRON_TEE] = tee
        except Exception:
            pass

    def request_finished(sender: Any, response: Any = None, **_extra: Any) -> None:
        try:
            import flask

            environ = flask.request.environ
            tee = environ.get(_ENVIRON_TEE)
            if tee is None or response is None:
                return
            span = environ.get("opentelemetry-flask.span_key") or trace.get_current_span()
            status = getattr(response, "status_code", None)
            if not wanted(settings, status):
                return
            streamed = bool(getattr(response, "is_streamed", False) or getattr(response, "direct_passthrough", False))
            if not streamed:
                tee.drain(environ.get("CONTENT_LENGTH"), settings)
            attributes = side_attributes(
                "request", tee._tap, environ.get("CONTENT_TYPE"), environ.get("HTTP_CONTENT_ENCODING"), settings
            )
            headers = getattr(response, "headers", None)
            content_type = _header(headers, "Content-Type")
            if streamed:
                attributes.update(unread_attributes("response", content_type, _length(_header(headers, "Content-Length"))))
            else:
                attributes.update(
                    bytes_attributes("response", response.get_data(), content_type, _header(headers, "Content-Encoding"), settings)
                )
            _set(span, attributes)
        except Exception as error:
            _log.debug("Zharp: could not capture a Flask body: %s", error)

    return request_hook, request_finished


def _length(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _django_url(request: Any) -> str:
    try:
        host = request.META.get("HTTP_HOST") or request.META.get("SERVER_NAME") or "localhost"
        return "{}://{}{}".format(request.scheme, host, request.get_full_path())
    except Exception:
        return ""


def django_hooks(settings: BodySettings):
    """(request_hook, response_hook) for the Django instrumentation; WSGI and ASGI alike."""

    def request_hook(span: Any, request: Any) -> None:
        try:
            if not _allowed(settings, _django_url(request)):
                return
            stream = getattr(request, "_stream", None)
            if stream is None or isinstance(stream, TeeInput):
                return
            tap = Tap(settings, lambda: request.META.get("CONTENT_TYPE"))
            tee = TeeInput(stream, tap)
            request._stream = tee
            request._zharp_tee = tee
        except Exception:
            pass

    def response_hook(span: Any, request: Any, response: Any) -> None:
        try:
            tee = getattr(request, "_zharp_tee", None)
            if tee is None:
                return
            if not wanted(settings, getattr(response, "status_code", None)):
                return
            streaming = bool(getattr(response, "streaming", False))
            if not streaming:
                tee.drain(request.META.get("CONTENT_LENGTH"), settings)
            attributes = side_attributes(
                "request", tee._tap, request.META.get("CONTENT_TYPE"), request.META.get("HTTP_CONTENT_ENCODING"), settings
            )
            content_type = response.get("Content-Type")
            if streaming:
                attributes.update(unread_attributes("response", content_type, _length(response.get("Content-Length"))))
            else:
                attributes.update(
                    bytes_attributes("response", response.content, content_type, response.get("Content-Encoding"), settings)
                )
            _set(span, attributes)
        except Exception as error:
            _log.debug("Zharp: could not capture a Django body: %s", error)

    return request_hook, response_hook


def _asgi_url(scope: Dict[str, Any]) -> str:
    try:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        host = headers.get("host")
        if not host:
            server = scope.get("server") or ("localhost", None)
            host = server[0] + (":{}".format(server[1]) if server[1] else "")
        path = scope.get("raw_path") or scope.get("path", "/")
        if isinstance(path, bytes):
            path = path.decode("latin-1")
        query = scope.get("query_string") or b""
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        return "{}://{}{}{}".format(scope.get("scheme", "http"), host, path, "?" + query if query else "")
    except Exception:
        return ""


class AsgiBodies:
    """ASGI middleware inside the OpenTelemetry one (so the server span is current and still
    open): tees what the app receives and what it sends, and sets the attributes before the last
    body message goes out — that message is what ends the span."""

    def __init__(self, app: Any, settings: BodySettings):
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        settings = self._settings
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        span = trace.get_current_span()
        if not span.is_recording() or not _allowed(settings, _asgi_url(scope)):
            await self.app(scope, receive, send)
            return

        request_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        request_tap = Tap(settings, lambda: request_headers.get("content-type"))
        state: Dict[str, Any] = {"status": None, "headers": {}, "tap": None, "done": False}

        def finish() -> None:
            if state["done"]:
                return
            state["done"] = True
            try:
                if not wanted(settings, state["status"]):
                    return
                attributes = side_attributes(
                    "request",
                    request_tap,
                    request_headers.get("content-type"),
                    request_headers.get("content-encoding"),
                    settings,
                )
                headers = state["headers"]
                attributes.update(
                    side_attributes("response", state["tap"], headers.get("content-type"), headers.get("content-encoding"), settings)
                )
                _set(span, attributes)
            except Exception as error:
                _log.debug("Zharp: could not capture an ASGI body: %s", error)

        async def tee_receive() -> Any:
            message = await receive()
            try:
                if message.get("type") == "http.request":
                    request_tap.add(message.get("body", b""))
            except Exception:
                pass
            return message

        async def tee_send(message: Dict[str, Any]) -> None:
            try:
                kind = message.get("type")
                if kind == "http.response.start":
                    state["status"] = message.get("status")
                    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message.get("headers") or []}
                    state["headers"] = headers
                    state["tap"] = Tap(settings, lambda: headers.get("content-type"))
                elif kind == "http.response.body" and state["tap"] is not None:
                    state["tap"].add(message.get("body", b""))
                    if not message.get("more_body", False):
                        finish()
            except Exception:
                pass
            await send(message)

        try:
            await self.app(scope, tee_receive, tee_send)
        except BaseException:
            finish()  # no response from the app: the request body is still kept
            raise


def patch_fastapi(settings: BodySettings) -> None:
    """Put :class:`AsgiBodies` just inside FastAPI's ``ServerErrorMiddleware``, which is where
    the FastAPI instrumentation takes the app it wraps — so it ends up inside the span."""
    try:
        from fastapi import FastAPI
        from starlette.middleware.errors import ServerErrorMiddleware
    except ImportError:
        return
    original = FastAPI.build_middleware_stack
    if getattr(original, "_zharp", False):
        return

    def build_middleware_stack(self: Any) -> Any:
        stack = original(self)
        try:
            if isinstance(stack, ServerErrorMiddleware) and not isinstance(stack.app, AsgiBodies):
                stack.app = AsgiBodies(stack.app, settings)
        except Exception:
            pass
        return stack

    build_middleware_stack._zharp = True  # type: ignore[attr-defined]
    FastAPI.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]


# ─── clients ────────────────────────────────────────────────────────────────────


def _request_body_attributes(body: Any, content_type: Optional[str], encoding: Optional[str], settings: BodySettings):
    """An outgoing request body as it is held in memory; anything streamed is described."""
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray, memoryview, str)):
        return bytes_attributes("request", body, content_type, encoding, settings)
    return unread_attributes("request", content_type, None)


def requests_hooks(settings: BodySettings):
    """(request_hook, response_hook) for the requests instrumentation."""

    def request_attributes(request: Any) -> Dict[str, Any]:
        headers = getattr(request, "headers", None)
        return _request_body_attributes(
            getattr(request, "body", None),
            _header(headers, "Content-Type"),
            _header(headers, "Content-Encoding"),
            settings,
        )

    def request_hook(span: Any, request: Any) -> None:
        try:
            if _allowed(settings, str(getattr(request, "url", ""))):
                PENDING.add(span, lambda: request_attributes(request))
        except Exception:
            pass

    def response_hook(span: Any, request: Any, response: Any) -> None:
        try:
            PENDING.pop(span)
            if not _allowed(settings, str(getattr(request, "url", ""))):
                return
            if not wanted(settings, getattr(response, "status_code", None)):
                return
            attributes = request_attributes(request)
            headers = getattr(response, "headers", None)
            content_type = _header(headers, "Content-Type")
            content = getattr(response, "_content", False)
            if getattr(response, "_content_consumed", False) and isinstance(content, bytes):
                # Already read by requests (stream=False), and already decoded.
                attributes.update(bytes_attributes("response", content, content_type, None, settings))
            else:
                # stream=True: the app hasn't read it yet, and it isn't read for it.
                attributes.update(unread_attributes("response", content_type, _length(_header(headers, "Content-Length"))))
            _set(span, attributes)
        except Exception as error:
            _log.debug("Zharp: could not capture a requests body: %s", error)

    return request_hook, response_hook


def _httpx_request_attributes(info: Any, settings: BodySettings) -> Dict[str, Any]:
    import httpx

    headers = getattr(info, "headers", None)
    content_type = _header(headers, "content-type")
    stream = getattr(info, "stream", None)
    if stream is None:
        return {}
    if isinstance(stream, httpx.ByteStream):
        return bytes_attributes("request", b"".join(stream), content_type, _header(headers, "content-encoding"), settings)
    fields = getattr(stream, "fields", None)
    if fields is not None and is_multipart(content_type):
        summary: "OrderedDict[str, Any]" = OrderedDict()
        for item in fields:
            name = getattr(item, "name", "")
            if hasattr(item, "filename"):
                size = None
                try:
                    size = item.get_length()
                except Exception:
                    pass
                file_type = None
                try:
                    file_type = item.headers.get("Content-Type")
                except Exception:
                    pass
                value: Any = file_label(item.filename, file_type, size)
            else:
                raw = getattr(item, "value", "")
                value = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if name not in summary:
                summary[name] = value
            elif isinstance(summary[name], list):
                summary[name].append(value)
            else:
                summary[name] = [summary[name], value]
        return body_attributes("request", stringify(summary), content_type, settings)
    return unread_attributes("request", content_type, _length(_header(headers, "content-length")))


def httpx_hooks(settings: BodySettings):
    """Sync and async (request_hook, response_hook) for the httpx instrumentation."""

    def url_of(info: Any) -> str:
        return str(getattr(info, "url", ""))

    def request_hook(span: Any, request: Any) -> None:
        try:
            if _allowed(settings, url_of(request)):
                PENDING.add(span, lambda: _httpx_request_attributes(request, settings))
        except Exception:
            pass

    def response_hook(span: Any, request: Any, response: Any) -> None:
        try:
            import httpx

            PENDING.pop(span)
            if not _allowed(settings, url_of(request)):
                return
            if not wanted(settings, getattr(response, "status_code", None)):
                return
            attributes = _httpx_request_attributes(request, settings)
            headers = getattr(response, "headers", None)
            content_type = _header(headers, "content-type")
            stream = getattr(response, "stream", None)
            if isinstance(stream, httpx.ByteStream):
                attributes.update(
                    bytes_attributes("response", b"".join(stream), content_type, _header(headers, "content-encoding"), settings)
                )
            else:
                # Not read yet (the span ends before the app reads it), and not read for it.
                attributes.update(unread_attributes("response", content_type, _length(_header(headers, "content-length"))))
            _set(span, attributes)
        except Exception as error:
            _log.debug("Zharp: could not capture an httpx body: %s", error)

    async def async_request_hook(span: Any, request: Any) -> None:
        request_hook(span, request)

    async def async_response_hook(span: Any, request: Any, response: Any) -> None:
        response_hook(span, request, response)

    return request_hook, response_hook, async_request_hook, async_response_hook


def instrument_options(name: str, settings: Optional[BodySettings]) -> Dict[str, Any]:
    """The instrumentor arguments that add body capture, for one library."""
    if settings is None:
        return {}
    if name == "flask":
        request_hook, request_finished = flask_hooks(settings)
        try:
            from flask import request_finished as signal

            signal.connect(request_finished, weak=False)
        except Exception as error:
            _log.debug("Zharp: no Flask request_finished signal: %s", error)
            return {}
        return {"request_hook": request_hook}
    if name == "django":
        request_hook, response_hook = django_hooks(settings)
        return {"request_hook": request_hook, "response_hook": response_hook}
    if name == "fastapi":
        patch_fastapi(settings)
        return {}
    if name == "requests":
        request_hook, response_hook = requests_hooks(settings)
        return {"request_hook": request_hook, "response_hook": response_hook}
    if name == "httpx":
        request_hook, response_hook, async_request_hook, async_response_hook = httpx_hooks(settings)
        return {
            "request_hook": request_hook,
            "response_hook": response_hook,
            "async_request_hook": async_request_hook,
            "async_response_hook": async_response_hook,
        }
    return {}


__all__ = ["UrlRedactionSpanProcessor", "instrument_options", "PENDING", "_bodies"]
