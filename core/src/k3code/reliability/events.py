"""Reliability event type, forwarded through the router's ``on_event`` callback.

The gateway can listen to these through the normal event stream later; the
``kind`` strings are ``reliability.*`` and ``net.state``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Event kinds emitted by the reliability layer.
PAUSED = "reliability.paused"
RESUMED = "reliability.resumed"
PARKED = "reliability.parked"
UNPARKED = "reliability.unparked"
INTERRUPTED_TOOL = "reliability.interrupted_tool"
BUDGET_EXCEEDED = "reliability.budget_exceeded"
LOOP_DETECTED = "reliability.loop_detected"
LOOP_NOTE_INJECTED = "reliability.loop_note_injected"
NEEDS_INPUT = "reliability.needs_input"
NET_STATE = "net.state"


@dataclass
class ReliabilityEvent:
    """One reliability-layer event (dict payload carries the details)."""

    kind: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: Monotonic-ish wall timestamp for consumers that want to log ordering.
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"event": self.kind, "detail": self.detail, **self.data}


EventSink = Callable[[ReliabilityEvent], None]


class EventEmitter:
    """Collects callbacks; swallows subscriber errors (they must never stop the loop)."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def add(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def emit(self, kind: str, detail: str = "", **data: Any) -> ReliabilityEvent:
        event = ReliabilityEvent(kind=kind, detail=detail, data=data)
        for sink in list(self._sinks):
            with contextlib.suppress(Exception):
                sink(event)
        return event
