import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client
    main.stress.stop()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_exposes_request_and_process_metrics(client):
    client.get("/")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",path="/",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "app_stress_active" in body


def test_unknown_route_uses_bounded_label(client):
    client.get("/does-not-exist")
    assert 'path="unmatched",status="404"' in client.get("/metrics").text


def test_fail_returns_500(client):
    assert client.get("/fail").status_code == 500


def test_stress_lifecycle(client):
    started = client.post("/stress", params={"seconds": 30})
    assert started.status_code == 202
    assert main.stress.active

    conflict = client.post("/stress", params={"seconds": 30})
    assert conflict.status_code == 409

    stopped = client.delete("/stress")
    assert stopped.json() == {"status": "stopped", "was_active": True}
    time.sleep(0.1)
    assert not main.stress.active


@pytest.mark.parametrize("seconds", [0, main.STRESS_MAX_SECONDS + 1])
def test_stress_duration_is_bounded(client, seconds):
    assert client.post("/stress", params={"seconds": seconds}).status_code == 422


@pytest.fixture
def control():
    with TestClient(main.control_app) as test_client:
        yield test_client
    main.shedder.set(False)


def test_work_simulates_latency_without_cpu(client):
    started = time.perf_counter()
    response = client.get("/work", params={"delay_ms": 200})
    assert response.status_code == 200
    assert time.perf_counter() - started >= 0.2
    assert 'http_request_duration_seconds_count{method="GET",path="/work"}' in client.get("/metrics").text


@pytest.mark.parametrize("delay_ms", [-1, 5001])
def test_work_delay_is_bounded(client, delay_ms):
    assert client.get("/work", params={"delay_ms": delay_ms}).status_code == 422


def test_load_shedding_rejects_business_traffic_but_keeps_probes(client, control):
    assert control.post("/shed-load", params={"enabled": "true"}).json() == {"enabled": True, "changed": True}

    shed = client.get("/work")
    assert shed.status_code == 503
    assert shed.headers["Retry-After"] == "10"
    assert client.get("/").status_code == 503

    assert client.get("/health").json()["load_shedding"] is True
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "app_load_shedding_active 1.0" in metrics.text
    assert 'http_requests_total{method="GET",path="/work",status="503"}' in metrics.text

    assert control.post("/shed-load", params={"enabled": "false"}).json() == {"enabled": False, "changed": True}
    assert client.get("/work").status_code == 200
    assert "app_load_shedding_active 0.0" in client.get("/metrics").text


def test_shed_load_toggle_is_idempotent(control):
    control.post("/shed-load", params={"enabled": "true"})
    assert control.post("/shed-load", params={"enabled": "true"}).json()["changed"] is False
    assert control.get("/shed-load").json() == {"enabled": True}


def test_control_api_is_not_exposed_on_public_app(client):
    assert client.post("/shed-load", params={"enabled": "true"}).status_code == 404
