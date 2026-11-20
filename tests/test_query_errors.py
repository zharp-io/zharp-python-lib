"""Headers on request spans, and failed queries reported once with the app's call site."""

import os
from types import SimpleNamespace

from opentelemetry.trace import SpanKind, StatusCode

from zharp import _db_errors, _errors, _headers


# ─── headers ──────────────────────────────────────────────────────────────────


def test_headers_on_by_default_secrets_redacted(monkeypatch):
    for name in (_headers.SERVER_REQUEST, _headers.SERVER_RESPONSE, _headers.SANITIZE):
        monkeypatch.delenv(name, raising=False)
    _headers.apply(_headers.header_settings(None, None))
    assert os.environ[_headers.SERVER_REQUEST] == ".*"
    assert os.environ[_headers.SERVER_RESPONSE] == ".*"
    assert "authorization" in os.environ[_headers.SANITIZE].split(",")


def test_headers_keep_the_apps_own_settings(monkeypatch):
    monkeypatch.setenv(_headers.SERVER_REQUEST, "user-agent")
    monkeypatch.setenv(_headers.SANITIZE, "x-tenant")
    monkeypatch.delenv(_headers.SERVER_RESPONSE, raising=False)
    _headers.apply(_headers.header_settings({"redact": ["x-team"]}, None))
    assert os.environ[_headers.SERVER_REQUEST] == "user-agent"
    sanitize = os.environ[_headers.SANITIZE].split(",")
    assert sanitize[0] == "x-tenant" and "x-team" in sanitize and "cookie" in sanitize


def test_headers_off():
    assert _headers.header_settings(False, None) is None
    assert _headers.header_settings(None, "false") is None


# ─── what a database error may say ───────────────────────────────────────────


class FakeDbError(Exception):
    sqlstate = "23505"
    diag = SimpleNamespace(constraint_name="users_email_key")


def test_row_values_never_leave():
    nl = chr(10)
    exc = FakeDbError(
        'duplicate key value violates unique constraint "users_email_key"' + nl
        + "DETAIL:  Key (email)=(ada@example.com) already exists." + nl
        + "[SQL: INSERT INTO users (email) VALUES (%(e)s)]" + nl
        + "[parameters: {'e': 'ada@example.com'}]"
    )
    message = _errors.message_of(exc)
    assert "ada@example.com" not in message
    assert "users_email_key" in message and "[SQL: INSERT" in message
    assert "ada@example.com" not in _errors.exception_attributes(exc, True)["exception.stacktrace"]


def test_other_errors_untouched():
    nl = chr(10)
    exc = ValueError("DETAIL: kept" + nl + "second line")
    assert _errors.message_of(exc) == str(exc)


def test_query_attributes():
    attrs = _db_errors.query_attributes({"db.system": "postgresql", "db.statement": "INSERT 1", "db.name": "shop"}, FakeDbError("x"))
    assert attrs == {
        "db.system": "postgresql", "db.statement": "INSERT 1", "db.name": "shop",
        "db.error.code": "23505", "db.error.constraint": "users_email_key",
    }


# ─── call sites ──────────────────────────────────────────────────────────────


def test_call_site_starts_at_the_apps_frame():
    frames = [
        ("/srv/.venv/lib/python3.12/site-packages/opentelemetry/trace/__init__.py", 600, "use_span"),
        ("/srv/.venv/lib/python3.12/site-packages/sqlalchemy/engine/base.py", 1977, "_exec_single_context"),
        ("/srv/app/users.py", 24, "create"),
        (_db_errors.__file__, 1, "hold"),
        ("/srv/app/views.py", 44, "signup"),
        ("/srv/.venv/lib/python3.12/site-packages/django/core/handlers/base.py", 197, "_get_response"),
    ]
    assert [f[2] for f in _db_errors.app_frames(frames)] == ["create", "signup", "_get_response"]


# ─── one report per failure ──────────────────────────────────────────────────


class FakeSpan:
    """What the processor reads of a span, before and after it ends."""

    _ids = iter(range(1, 10_000))

    def __init__(self, attributes=None, kind=SpanKind.CLIENT, parent=True, trace_id=0xABC):
        self.attributes = dict(attributes or {})
        self.kind = kind
        self.name = "q"
        self.context = SimpleNamespace(trace_id=trace_id, span_id=next(self._ids), is_remote=False)
        self.parent = SimpleNamespace(trace_id=trace_id, span_id=1, is_remote=False) if parent else None
        self.status = SimpleNamespace(status_code=StatusCode.UNSET)

    def get_span_context(self):
        return self.context


def _fail_query(processor, exc, trace_id=0xABC):
    span = FakeSpan({"db.system": "postgresql", "db.statement": "INSERT INTO users"}, trace_id=trace_id)

    def create_user():
        return processor.hold(span, exc)

    assert create_user() is True
    span.status = SimpleNamespace(status_code=StatusCode.ERROR)
    processor.on_end(span)
    return span


def test_caught_failure_reported_at_request_end_with_call_site():
    sent = []
    processor = _db_errors.DbErrorSpanProcessor(lambda exc, stack, attrs, ctx: sent.append((exc, stack, attrs, ctx)))
    exc = FakeDbError("duplicate key")
    query = _fail_query(processor, exc)
    processor.on_end(FakeSpan(kind=SpanKind.SERVER, parent=False))
    assert len(sent) == 1
    reported, stack, attrs, ctx = sent[0]
    assert reported is exc
    assert "in create_user" in stack and stack.strip().endswith("FakeDbError: duplicate key")
    assert attrs["db.statement"] == "INSERT INTO users" and attrs["db.error.code"] == "23505"
    assert ctx.span_id == query.context.span_id


def test_escaped_failure_claimed_by_its_report_and_wrappers_count():
    sent = []
    processor = _db_errors.DbErrorSpanProcessor(lambda *a: sent.append(a))
    driver = FakeDbError("duplicate key")
    _fail_query(processor, driver)
    wrapper = Exception("IntegrityError")
    wrapper.orig = driver  # SQLAlchemy's shape
    assert processor.claim(wrapper)["db.statement"] == "INSERT INTO users"
    assert processor.claim(wrapper) is None
    processor.on_end(FakeSpan(kind=SpanKind.SERVER, parent=False))
    assert sent == []


def test_spans_that_are_not_queries_are_not_held():
    processor = _db_errors.DbErrorSpanProcessor(lambda *a: None)
    assert processor.hold(FakeSpan({"http.route": "/x"}), ValueError("x")) is False


def test_same_failure_seen_by_two_instrumentations_once():
    sent = []
    processor = _db_errors.DbErrorSpanProcessor(lambda *a: sent.append(a))
    driver = FakeDbError("dup")
    _fail_query(processor, driver)
    wrapper = Exception("IntegrityError")
    wrapper.orig = driver
    _fail_query(processor, wrapper)
    processor.force_flush()
    assert len(sent) == 1
