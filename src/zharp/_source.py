"""The code around an error's lines, read on the app's own server and sent with the error.

Python runs its source files, so the lines are right there: Zharp shows exactly the code that ran,
with no repository link and nothing to upload. Only the app's own frames (not installed packages,
not Python's library, never Zharp's or OpenTelemetry's), the innermost five, five lines either side,
each line capped. ``capture_source_context=False`` (or ``ZHARP_CAPTURE_SOURCE_CONTEXT=false``) turns
it off.
"""

import json
import linecache
import os
from typing import Iterable, List, Optional, Tuple

ATTRIBUTE = "zharp.source_context"
_AROUND = 5
_MAX_FRAMES = 5
_MAX_LINE = 300
_MAX_BYTES = 12_000

#: Set by init(): False when the app turned it off.
enabled = True

Frame = Tuple[str, int]


def _cut(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= _MAX_LINE else text[: _MAX_LINE - 1] + "…"


def _is_app(filename: str) -> bool:
    from ._db_errors import _LIBRARY, _never_app

    return bool(filename) and not filename.startswith("<") and not _never_app(filename) and not any(lib in filename for lib in _LIBRARY)


def context_of(frames: Iterable[Frame]) -> Optional[str]:
    """The JSON for an error's frames (innermost first): [{file, line, pre, code, post}]."""
    if not enabled:
        return None
    out: List[dict] = []
    seen = set()
    for filename, line in frames:
        if len(out) >= _MAX_FRAMES:
            break
        if not line or (filename, line) in seen or not _is_app(filename):
            continue
        seen.add((filename, line))
        try:
            code = linecache.getline(filename, line)
            if not code:
                continue
            pre = [_cut(linecache.getline(filename, n)) for n in range(max(1, line - _AROUND), line)]
            post = []
            for n in range(line + 1, line + 1 + _AROUND):
                text = linecache.getline(filename, n)
                if not text:
                    break
                post.append(_cut(text))
        except Exception:
            continue
        out.append({"file": filename, "line": line, "pre": pre, "code": _cut(code), "post": post})
    while out:
        text = json.dumps(out, ensure_ascii=False)
        if len(text.encode("utf-8")) <= _MAX_BYTES:
            return text
        out.pop()
    return None


def of_exception(exc: BaseException) -> Optional[str]:
    """The code around an exception's traceback, innermost frame first."""
    frames: List[Frame] = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_lineno))
        tb = tb.tb_next
    return context_of(reversed(frames))


def set_enabled(on: bool) -> None:
    global enabled
    enabled = bool(on)


def env_enabled(value: Optional[bool]) -> bool:
    if value is not None:
        return bool(value)
    return (os.environ.get("ZHARP_CAPTURE_SOURCE_CONTEXT") or "").strip().lower() not in ("false", "0", "no", "off")
