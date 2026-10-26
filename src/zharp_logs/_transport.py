"""Sends batches of log entries to ``{endpoint}/agent/logs`` as ``{"logs": [...]}``."""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from ._version import SDK_NAME

DEFAULT_ENDPOINT = "https://api.zharp.io/api/v1"

#: Largest body one request carries. The backend accepts 10 MB; staying far below it keeps one slow
#: upload from holding everything behind it. An entry larger than this on its own is still sent
#: alone rather than dropped.
MAX_REQUEST_BYTES = 1_000_000

_TIMEOUT_SECONDS = 15


def normalize_endpoint(endpoint: Any) -> str:
    """Trim a configured endpoint so paths can be appended to it."""
    value = str(endpoint).strip() if endpoint is not None and str(endpoint).strip() else DEFAULT_ENDPOINT
    return value.rstrip("/")


class Transport:
    def __init__(self, api_key: str, endpoint: str, retry_delay: float = 1.0) -> None:
        self.api_key = api_key
        self.url = endpoint + "/agent/logs"
        #: The first retry waits twice this, the second four times.
        self.retry_delay = retry_delay

    def send(self, entries: List[Dict[str, Any]]) -> bool:
        """Send entries, split into bodies by size. Returns whether every body was taken or refused
        for good (a 4xx) — False when the network or the server failed throughout."""
        delivered = True
        for body in split_by_size(entries, MAX_REQUEST_BYTES):
            if not self._send_body(body):
                delivered = False
        return delivered

    def _send_body(self, body: bytes) -> bool:
        """Three attempts in all. Only network errors and 5xx responses are retried — a 4xx won't be
        fixed by sending the same thing again."""
        for attempt in range(3):
            try:
                if self.post(body) < 500:
                    return True
            except Exception:
                # Network failure or timeout — fall through to retry.
                pass
            if attempt < 2:
                time.sleep(self.retry_delay * (2 << attempt))
        return False

    def post(self, body: bytes) -> int:
        """One POST; returns the status code."""
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                # The key goes in a header, never the URL: URLs end up in proxy logs.
                "X-API-KEY": self.api_key,
                "X-Zharp-SDK": SDK_NAME,
                "User-Agent": SDK_NAME,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                response.read()
                return int(response.status)
        except urllib.error.HTTPError as error:
            # 4xx and 5xx arrive as an exception; the status is what matters.
            try:
                error.read()
            finally:
                error.close()
            return int(error.code)


def split_by_size(entries: List[Dict[str, Any]], limit: int) -> List[bytes]:
    """``{"logs": [...]}`` bodies of at most ``limit`` bytes each. An entry too large to fit even
    alone still gets a body of its own: whether it can be delivered is for the network to decide."""
    overhead = len(b'{"logs":[]}')
    bodies: List[bytes] = []
    group: List[bytes] = []
    size = overhead
    for entry in entries:
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        entry_size = len(encoded) + 1  # +1 for the separating comma
        if group and size + entry_size > limit:
            bodies.append(b'{"logs":[' + b",".join(group) + b"]}")
            group = []
            size = overhead
        group.append(encoded)
        size += entry_size
    if group:
        bodies.append(b'{"logs":[' + b",".join(group) + b"]}")
    return bodies
