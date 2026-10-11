"""In-process publish/subscribe for Server-Sent Events. Safe to publish from worker threads."""

import asyncio
import json
import threading


class EventBus:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self, incident_id=None):
        queue = asyncio.Queue(maxsize=500)
        sub = (queue, asyncio.get_running_loop(), incident_id)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            self._subs.discard(sub)

    def publish(self, kind, incident_id=None, **data):
        message = json.dumps({"type": kind, "incident_id": incident_id, **data}, default=str)
        with self._lock:
            subs = list(self._subs)
        for queue, loop, wanted in subs:
            if wanted is not None and incident_id is not None and wanted != incident_id:
                continue
            try:
                loop.call_soon_threadsafe(self._put, queue, message)
            except RuntimeError:  # loop closed
                self.unsubscribe((queue, loop, wanted))

    @staticmethod
    def _put(queue, message):
        if queue.full():  # slow client: drop oldest rather than block publishers
            queue.get_nowait()
        queue.put_nowait(message)


bus = EventBus()
