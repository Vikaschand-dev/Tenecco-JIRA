"""Tiny in-process pub/sub used to push webhook events to browsers over SSE.

For multiple server instances, replace with Redis pub/sub or similar.
"""

import asyncio
import json
from collections import defaultdict


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, cloud_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[cloud_id].add(q)
        return q

    def unsubscribe(self, cloud_id: str, q: asyncio.Queue) -> None:
        self._subscribers[cloud_id].discard(q)

    def publish(self, cloud_id: str, event: dict) -> None:
        data = json.dumps(event, default=str)
        for q in list(self._subscribers.get(cloud_id, ())):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow consumer; drop rather than block webhook handling


bus = EventBus()
