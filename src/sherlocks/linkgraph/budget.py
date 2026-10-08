"""Reply time budgets: every step of a chat reply has a time limit and a fallback.

``Budget`` tracks the whole reply's deadline and each step's time. ``run`` gives a step
its own limit (never past the reply's deadline): over it, the step's fallback is used
now, and - when ``on_late`` is given - its real result is handed over when it arrives,
to be sent as a follow-up message. Partial beats late: a 30-second spinner is a failure.

Steps run on a small shared thread pool, so a slow model call never holds the reply.
Every step's time and every model call are counted for the turn's timing record.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="reply-step")
_LATE = object()


class CountingLlm:
    """The model client, counting its calls (a turn should make at most two on most turns)."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self.calls = 0
        self._lock = threading.Lock()

    def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.calls += 1
        return self._llm.generate_structured(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)


class Budget:
    def __init__(self, total_s: float) -> None:
        self.started = time.monotonic()
        self.deadline = self.started + total_s
        self.timing: dict[str, int] = {}
        self.late: list[str] = []

    def left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def record(self, step: str, since: float) -> None:
        self.timing[step] = self.timing.get(step, 0) + int((time.monotonic() - since) * 1000)

    def run(self, step: str, fn: Callable[[], Any], seconds: float, *, fallback: Any = None,
            on_late: Callable[[Any], None] | None = None) -> Any:
        """``fn()`` within ``seconds`` (and the reply's deadline); else ``fallback`` now
        and ``on_late(result)`` when ``fn`` finishes."""
        start = time.monotonic()
        limit = min(seconds, self.left())
        future: Future = _POOL.submit(fn)
        try:
            return future.result(timeout=max(0.05, limit))
        except FutureTimeout:
            self.late.append(step)
            logger.info("Reply step %s over its %.1fs budget - using the fallback", step, limit)
            if on_late is not None:
                future.add_done_callback(lambda f: _deliver(f, on_late, step))
            return fallback
        finally:
            self.record(step, start)

    def submit(self, fn: Callable[[], Any]) -> Future:
        """Start a step now and collect it later (``collect``) - for steps that run alongside."""
        return _POOL.submit(fn)

    def collect(self, step: str, future: Future, seconds: float, *, fallback: Any = None,
                on_late: Callable[[Any], None] | None = None, started: float | None = None) -> Any:
        start = started or time.monotonic()
        try:
            return future.result(timeout=max(0.05, min(seconds, self.left())))
        except FutureTimeout:
            self.late.append(step)
            if on_late is not None:
                future.add_done_callback(lambda f: _deliver(f, on_late, step))
            return fallback
        finally:
            self.record(step, start)

    def stream(self, step: str, make: Callable[[], Iterator[dict[str, Any]]], seconds: float,
               on_late: Callable[[dict[str, Any] | None], None] | None = None) -> Iterator[dict[str, Any]]:
        """Relay a generator's events while within budget. Over budget, stop relaying;
        the generator keeps running, and its ``final`` event goes to ``on_late``."""
        start = time.monotonic()
        end = time.monotonic() + min(seconds, self.left())
        q: queue.Queue = queue.Queue()
        relaying = threading.Event()
        relaying.set()

        def pump() -> None:
            final = None
            try:
                for event in make():
                    if event.get("type") == "final":
                        final = event
                    if relaying.is_set():
                        q.put(event)
            except Exception as exc:  # noqa: BLE001 - the reply carries on without it
                logger.info("Reply step %s failed: %s", step, exc)
            finally:
                if relaying.is_set():
                    q.put(_LATE)
                elif on_late is not None:
                    try:
                        on_late(final)
                    except Exception:  # noqa: BLE001
                        logger.exception("Late result of %s could not be delivered", step)

        _POOL.submit(pump)
        try:
            while True:
                wait = end - time.monotonic()
                if wait <= 0:
                    relaying.clear()
                    self.late.append(step)
                    logger.info("Reply step %s over its budget - its result will follow", step)
                    # It may have finished just now: a final already queued is handed over too.
                    while True:
                        try:
                            event = q.get_nowait()
                        except queue.Empty:
                            break
                        if isinstance(event, dict) and event.get("type") == "final" and on_late is not None:
                            _POOL.submit(on_late, event)
                    return
                try:
                    event = q.get(timeout=wait)
                except queue.Empty:
                    continue
                if event is _LATE:
                    return
                yield event
        finally:
            self.record(step, start)


def _deliver(future: Future, on_late: Callable[[Any], None], step: str) -> None:
    try:
        result = future.result()
    except Exception as exc:  # noqa: BLE001
        logger.info("Late step %s failed: %s", step, exc)
        return
    try:
        on_late(result)
    except Exception:  # noqa: BLE001
        logger.exception("Late result of %s could not be delivered", step)
