"""Thread-safe in-process pub/sub bus. Callbacks must never raise into publishers."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable

from aegis_velocity.core.events import Event

log = logging.getLogger(__name__)

Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._all: list[Handler] = []

    def subscribe(self, kind: str, handler: Handler) -> None:
        with self._lock:
            self._subs[kind].append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        with self._lock:
            self._all.append(handler)

    def publish(self, event: Event) -> None:
        kind = type(event).__name__
        with self._lock:
            handlers = list(self._subs.get(kind, ())) + list(self._all)
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                log.exception("bus handler failed for %s", kind)
