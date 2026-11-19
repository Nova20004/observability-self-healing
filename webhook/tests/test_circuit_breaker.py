import pytest

from circuit_breaker import BreakerState, CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def breaker(clock):
    return CircuitBreaker(max_actions=2, window_seconds=600, clock=clock)


def test_allows_budget_then_escalates(breaker):
    assert breaker.acquire("app").allowed
    assert breaker.acquire("app").allowed
    third = breaker.acquire("app")
    assert not third.allowed
    assert third.just_escalated
    assert third.state == BreakerState.ESCALATED
    assert breaker.state("app") == BreakerState.ESCALATED


def test_escalated_stays_open_even_after_window(breaker, clock):
    for _ in range(3):
        breaker.acquire("app")
    clock.now += 10_000
    decision = breaker.acquire("app")
    assert not decision.allowed
    assert not decision.just_escalated


def test_sliding_window_forgets_old_actions(breaker, clock):
    breaker.acquire("app")
    clock.now += 400
    breaker.acquire("app")
    clock.now += 201  # first action is now outside the 600s window
    assert breaker.acquire("app").allowed
    assert breaker.state("app") == BreakerState.CLOSED


def test_services_are_isolated(breaker):
    for _ in range(3):
        breaker.acquire("app")
    assert breaker.acquire("api").allowed


def test_reset_rearms_breaker(breaker):
    for _ in range(3):
        breaker.acquire("app")
    assert breaker.reset("app") == ["app"]
    assert breaker.state("app") == BreakerState.CLOSED
    assert breaker.acquire("app").allowed


def test_rollback_does_not_consume_budget(breaker):
    breaker.acquire("app")
    breaker.rollback("app")
    breaker.acquire("app")
    assert breaker.acquire("app").allowed


def test_snapshot_reports_state(breaker):
    breaker.acquire("app")
    assert breaker.snapshot(["app"]) == {
        "app": {"state": "closed", "recent_actions": 1, "max_actions": 2, "window_seconds": 600}
    }


def test_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        CircuitBreaker(max_actions=0, window_seconds=600)
