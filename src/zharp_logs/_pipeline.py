"""One running configuration's queue and the thread that sends it."""

import collections
import os
import threading
import time
import weakref
from typing import Any, Deque, Dict, List, Optional

from ._transport import Transport

#: Logs kept while they wait to be sent. When the network is down for long, the oldest go first.
MAX_QUEUED = 500


class Pipeline:
    """Logging only adds to the queue; every request is made on one background thread, never the
    caller's. The thread is a daemon, so it never keeps a finished program running — what's left is
    sent at exit instead."""

    def __init__(self, transport: Transport, flush_interval: float, max_batch_size: int) -> None:
        self.transport = transport
        self.flush_interval = flush_interval
        self.max_batch_size = max_batch_size
        self._queue: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_QUEUED)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._urgent = False
        self._closed = False
        self._sending = 0
        self._thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        if hasattr(os, "register_at_fork"):
            # A fork can happen while another thread holds the lock; the child would wait on it
            # forever. It gets fresh ones, and starts its own sender on its first log.
            ref = weakref.ref(self)
            os.register_at_fork(after_in_child=lambda: ref() and ref()._after_fork())

    def _after_fork(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._thread = None
        self._sending = 0
        self._pid = os.getpid()

    def push(self, entry: Dict[str, Any], urgent: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._queue.append(entry)  # a full deque drops its oldest
            if urgent:
                self._urgent = True
            self._ensure_thread()
            self._wake.notify()

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Send everything queued; wait up to ``timeout`` seconds (forever when None) for it to be
        out. Returns whether it was."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            if not self._queue and not self._sending:
                return True
            self._urgent = True
            self._ensure_thread()
            self._wake.notify()
            while self._queue or self._sending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._wake.wait(remaining if remaining is not None else 0.5)
            return True

    def close(self, timeout: Optional[float] = None) -> bool:
        sent = self.flush(timeout)
        with self._lock:
            self._closed = True
            self._wake.notify_all()
        return sent

    def drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._queue)
            self._queue.clear()
            return entries

    def _ensure_thread(self) -> None:
        """Start the sender if it isn't running — including in a child process after a fork (a
        gunicorn or Celery worker), where the parent's thread doesn't exist. Call holding the lock."""
        if self._pid != os.getpid():
            self._pid = os.getpid()
            self._thread = None
            self._sending = 0
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="zharp-logs", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                # Asleep until there's something to send.
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed and not self._queue:
                    return
                # Then wait out the interval, or less when something urgent arrives.
                deadline = time.monotonic() + self.flush_interval
                while not self._urgent and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._wake.wait(remaining)
                batch = []
                while self._queue and len(batch) < self.max_batch_size:
                    batch.append(self._queue.popleft())
                if not self._queue:
                    self._urgent = False
                if not batch:
                    continue
                self._sending += 1
            try:
                self.transport.send(batch)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._sending -= 1
                    self._wake.notify_all()
