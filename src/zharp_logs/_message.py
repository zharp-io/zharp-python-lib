"""Turning what callers log into the JSON Zharp takes."""

import dataclasses
import datetime
import decimal
import enum
import json
import traceback
import uuid
from typing import Any, Dict, Mapping, Optional

#: Nested values deeper than this become text, so a structure that contains itself can't hang.
_MAX_DEPTH = 12

#: Frames from this package are left out of a stack found for the caller.
_OWN_FRAMES = ("zharp_logs/", "zharp_logs\\")


def to_message(value: Any) -> str:
    """A log message as text.

    Callers pass whatever they have — an exception, a dict, None from a field that wasn't set. The
    backend refuses an entry whose message isn't non-empty text, so every path coerces here.
    """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, BaseException):
        text = describe(value)
    elif isinstance(value, (dict, list, tuple, set, frozenset)):
        text = json.dumps(to_json(value), ensure_ascii=False, separators=(",", ":"))
    else:
        try:
            text = str(value)
        except Exception:
            text = repr(value)
    return text if text.strip() else "(empty message)"


def type_name(error: BaseException) -> str:
    """``ValueError`` for a built-in, ``requests.exceptions.HTTPError`` for anything else."""
    cls = type(error)
    module = cls.__module__
    name = getattr(cls, "__qualname__", cls.__name__)
    return name if module in ("builtins", "__main__") else "{}.{}".format(module, name)


def describe(error: BaseException) -> str:
    """``ValueError: card declined`` — the type people recognise, then what it says."""
    name = type(error).__name__
    try:
        text = str(error)
    except Exception:
        text = ""
    return "{}: {}".format(name, text) if text.strip() else name


def stack_of(error: BaseException) -> str:
    """Where the error was raised, as Python prints it, causes included. An error that was made but
    never raised has no traceback; then it's where it was logged, from the caller's code."""
    if error.__traceback__ is not None:
        return "".join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()
    return caller_stack(describe(error))


def caller_stack(headline: str) -> str:
    frames = [f for f in traceback.format_stack() if not any(own in f for own in _OWN_FRAMES)]
    return ("Traceback (most recent call last):\n" + "".join(frames) + headline).rstrip()


def error_fields(error: BaseException) -> Dict[str, Any]:
    """An error as plain fields: its type, its text and where it was raised."""
    return {"name": type_name(error), "message": describe(error), "stack": stack_of(error)}


def first_error(metadata: Optional[Mapping[str, Any]]) -> Optional[BaseException]:
    if not metadata:
        return None
    for value in metadata.values():
        if isinstance(value, BaseException):
            return value
    return None


def to_json_map(metadata: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Metadata as plain JSON values, copied now so a later change to the caller's dict doesn't
    change a log already waiting to be sent. Values JSON can't hold become text."""
    if not metadata:
        return {}
    try:
        items = list(metadata.items())
    except Exception:
        return {"metadata": _safe_str(metadata)}
    return {str(key): to_json(value) for key, value in items if key is not None}


def to_json(value: Any, depth: int = 0) -> Any:
    """A value as something ``json.dumps`` writes without help."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN and infinity aren't JSON.
        return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if depth > _MAX_DEPTH:
        return _safe_str(value)
    if isinstance(value, BaseException):
        return error_fields(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            return value.isoformat(timespec="milliseconds") + "Z"
        return value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, enum.Enum):
        return to_json(value.value, depth + 1)
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(k): to_json(v, depth + 1) for k, v in list(value.items()) if k is not None}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json(item, depth + 1) for item in list(value)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return to_json(dataclasses.asdict(value), depth + 1)
        except Exception:
            pass
    return _safe_str(value)


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return "<{}>".format(type(value).__name__)


def iso_now(timestamp: float) -> str:
    """``2026-10-26T09:15:02.123Z``"""
    moment = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).replace(tzinfo=None)
    return moment.isoformat(timespec="milliseconds") + "Z"
