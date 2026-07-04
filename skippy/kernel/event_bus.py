"""
Skippy Kernel Event Bus

Provides publish/subscribe messaging between kernel components.
"""

from collections import defaultdict
from typing import Any, Callable


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._history: list[tuple[str, Any]] = []

    def subscribe(
        self,
        event: str,
        callback: Callable[..., Any],
    ) -> None:
        """Register a callback for an event."""
        self._subscribers[event].append(callback)

    def unsubscribe(
        self,
        event: str,
        callback: Callable[..., Any],
    ) -> None:
        """Remove a callback."""
        if callback in self._subscribers[event]:
            self._subscribers[event].remove(callback)

    def publish(
        self,
        event: str,
        payload: Any = None,
    ) -> None:
        """Publish an event."""

        self._history.append((event, payload))

        for callback in self._subscribers[event]:
            callback(payload)

    def history(self) -> list[tuple[str, Any]]:
        """Return event history."""
        return self._history.copy()

    def clear(self) -> None:
        """Clear stored history."""
        self._history.clear()


if __name__ == "__main__":
    bus = EventBus()

    def logger(payload):
        print("EVENT:", payload)

    bus.subscribe("mission.started", logger)
    bus.subscribe("mission.completed", logger)

    bus.publish("mission.started", {"mission": "Evaluate BTC"})

    bus.publish("mission.completed", {"status": "SUCCESS"})

    print()
    print(bus.history())
