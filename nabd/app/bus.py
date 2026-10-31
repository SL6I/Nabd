"""In-process event bus. Every step Nabd takes becomes an event; the War Room
receives them over Server-Sent Events and new viewers get a replay."""

import asyncio
import itertools
import time
from collections import deque


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=1500)
        self._seq = itertools.count(1)
        self.latest_telemetry: dict | None = None

    def publish(self, type_: str, **data) -> dict:
        event = {"seq": next(self._seq), "ts": time.time(), "type": type_, **data}
        if type_ == "telemetry":
            self.latest_telemetry = event
        else:
            self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    def history(self, incident_id: str | None = None) -> list[dict]:
        if incident_id is None:
            return list(self._history)
        return [e for e in self._history if e.get("incident") == incident_id]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


bus = Bus()
