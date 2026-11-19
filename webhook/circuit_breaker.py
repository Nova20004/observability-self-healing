"""Sliding-window anti-flap circuit breaker for destructive remediations.

CLOSED: restarts allowed while fewer than `max_actions` happened in the last `window_seconds`.
ESCALATED: the budget was exceeded; every further restart is refused until an operator resets it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class BreakerDecision:
    allowed: bool
    state: BreakerState
    recent_actions: int
    just_escalated: bool = False


class CircuitBreaker:
    def __init__(self, max_actions: int, window_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        if max_actions < 1 or window_seconds < 1:
            raise ValueError("max_actions and window_seconds must be >= 1")
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._history: dict[str, deque[float]] = {}
        self._state: dict[str, BreakerState] = {}

    def _prune(self, service: str, now: float) -> deque[float]:
        history = self._history.setdefault(service, deque())
        while history and now - history[0] >= self.window_seconds:
            history.popleft()
        return history

    def acquire(self, service: str) -> BreakerDecision:
        """Atomically check the budget and, if allowed, record the action."""
        with self._lock:
            now = self._clock()
            history = self._prune(service, now)
            if self._state.get(service) == BreakerState.ESCALATED:
                return BreakerDecision(False, BreakerState.ESCALATED, len(history))
            if len(history) >= self.max_actions:
                self._state[service] = BreakerState.ESCALATED
                return BreakerDecision(False, BreakerState.ESCALATED, len(history), just_escalated=True)
            history.append(now)
            return BreakerDecision(True, BreakerState.CLOSED, len(history))

    def rollback(self, service: str) -> None:
        """Forget the last recorded action (the restart did not actually happen)."""
        with self._lock:
            history = self._history.get(service)
            if history:
                history.pop()

    def reset(self, service: str | None = None) -> list[str]:
        with self._lock:
            services = [service] if service else sorted(set(self._history) | set(self._state))
            for name in services:
                self._history.pop(name, None)
                self._state.pop(name, None)
            return services

    def state(self, service: str) -> BreakerState:
        with self._lock:
            return self._state.get(service, BreakerState.CLOSED)

    def snapshot(self, services: list[str]) -> dict[str, dict[str, object]]:
        with self._lock:
            now = self._clock()
            return {
                name: {
                    "state": str(self._state.get(name, BreakerState.CLOSED)),
                    "recent_actions": len(self._prune(name, now)),
                    "max_actions": self.max_actions,
                    "window_seconds": self.window_seconds,
                }
                for name in sorted(set(services) | set(self._state))
            }
