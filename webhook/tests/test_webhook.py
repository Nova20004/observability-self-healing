import json

import pytest
from fastapi.testclient import TestClient

import main

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeRestarter:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def restart(self, container: str) -> None:
        self.calls.append(container)
        if self.fail:
            raise main.RemediationError("docker API returned 500: boom")


def make_settings(tmp_path, **overrides):
    values = {
        "token": TOKEN,
        "cooldown_seconds": 120,
        "targets": {"app": "obs-selfheal-app-1"},
        "docker_api_url": "http://unused",
        "audit_log_path": tmp_path / "audit.jsonl",
        "dry_run": False,
    }
    values.update(overrides)
    return main.Settings(**values)


def alert_payload(status="firing", alertname="HighCPUUsage", service="app", remediation="restart"):
    labels = {"alertname": alertname, "service": service, "severity": "warning"}
    if remediation:
        labels["remediation"] = remediation
    return {
        "version": "4",
        "status": status,
        "receiver": "remediation-webhook",
        "alerts": [{"status": status, "labels": labels, "annotations": {}, "fingerprint": "abc123"}],
    }


@pytest.fixture
def restarter():
    return FakeRestarter()


@pytest.fixture
def client(tmp_path, restarter):
    return TestClient(main.create_app(make_settings(tmp_path), restarter))


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
def test_rejects_missing_or_invalid_token(client, restarter, headers):
    response = client.post("/alerts", json=alert_payload(), headers=headers)
    assert response.status_code == 401
    assert restarter.calls == []
    assert "webhook_auth_failures_total 1.0" in client.get("/metrics").text


def test_auth_is_checked_before_payload_validation(client):
    assert client.post("/alerts", content=b"not json").status_code == 401
    assert client.post("/alerts", content=b"not json", headers=AUTH).status_code == 422


def test_firing_alert_restarts_allowlisted_container(client, restarter, tmp_path):
    response = client.post("/alerts", json=alert_payload(), headers=AUTH)
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "success"
    assert restarter.calls == ["obs-selfheal-app-1"]

    record = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert record["event"] == "remediation_audit"
    assert record["alertname"] == "HighCPUUsage"
    assert record["target"] == "app"
    assert record["action"] == "restart"
    assert record["status"] == "success"
    assert record["timestamp"]
    assert 'remediation_actions_total{action="restart",alertname="HighCPUUsage",status="success",target="app"} 1.0' in (
        client.get("/metrics").text
    )


def test_outcome_counters_are_preinitialised_for_allowlisted_targets(client):
    body = client.get("/metrics").text
    assert 'remediation_outcomes_total{outcome="success",service="app"} 0.0' in body
    assert 'remediation_outcomes_total{outcome="skipped_cooldown",service="app"} 0.0' in body
    assert 'remediation_outcomes_total{outcome="escalated",service="app"} 0.0' in body
    assert 'remediation_circuit_breaker_open{service="app"} 0.0' in body


def test_cooldown_prevents_restart_loop(client, restarter):
    client.post("/alerts", json=alert_payload(), headers=AUTH)
    second = client.post("/alerts", json=alert_payload(), headers=AUTH).json()["results"][0]
    assert second["status"] == "skipped_cooldown"
    assert restarter.calls == ["obs-selfheal-app-1"]


def test_failed_restart_is_audited_and_releases_cooldown(tmp_path):
    failing = FakeRestarter(fail=True)
    client = TestClient(main.create_app(make_settings(tmp_path), failing))
    first = client.post("/alerts", json=alert_payload(), headers=AUTH).json()["results"][0]
    second = client.post("/alerts", json=alert_payload(), headers=AUTH).json()["results"][0]
    assert first["status"] == second["status"] == "failed"
    assert len(failing.calls) == 2


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (alert_payload(status="resolved"), "resolved"),
        (alert_payload(remediation=None), "skipped_no_action"),
        (alert_payload(service="database"), "skipped_not_allowed"),
    ],
)
def test_non_actionable_alerts_are_audited_without_restart(client, restarter, payload, expected):
    result = client.post("/alerts", json=payload, headers=AUTH).json()["results"][0]
    assert result["status"] == expected
    assert restarter.calls == []


def test_dry_run_never_restarts(tmp_path, restarter):
    client = TestClient(main.create_app(make_settings(tmp_path, dry_run=True), restarter))
    result = client.post("/alerts", json=alert_payload(), headers=AUTH).json()["results"][0]
    assert result["status"] == "dry_run"
    assert restarter.calls == []


def test_audit_chain_detects_tampering(client, tmp_path):
    for status in ("firing", "resolved", "firing"):
        client.post("/alerts", json=alert_payload(status=status), headers=AUTH)
    assert client.get("/audit/verify", headers=AUTH).json() == {
        "valid": True,
        "entries": 3,
        "first_invalid_index": None,
    }
    assert len(client.get("/audit", params={"limit": 2}, headers=AUTH).json()["entries"]) == 2

    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"status":"resolved"', '"status":"success"')
    path.write_text("\n".join(lines) + "\n")
    assert client.get("/audit/verify", headers=AUTH).json()["first_invalid_index"] == 1


def test_audit_chain_survives_restart(tmp_path, restarter):
    settings = make_settings(tmp_path)
    TestClient(main.create_app(settings, restarter)).post(
        "/alerts", json=alert_payload(status="resolved"), headers=AUTH
    )
    client = TestClient(main.create_app(settings, restarter))
    client.post("/alerts", json=alert_payload(status="resolved"), headers=AUTH)
    assert client.get("/audit/verify", headers=AUTH).json()["valid"] is True


def test_audit_endpoints_require_token(client):
    assert client.get("/audit").status_code == 401
    assert client.get("/audit/verify").status_code == 401


def test_settings_reject_weak_token():
    with pytest.raises(ValueError, match="at least"):
        main.Settings.from_env({"WEBHOOK_TOKEN": "change-me"})


def test_settings_read_token_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    settings = main.Settings.from_env(
        {"WEBHOOK_TOKEN_FILE": str(token_file), "REMEDIATION_TARGETS": "app=obs-selfheal-app-1"}
    )
    assert settings.token == TOKEN
    assert settings.targets == {"app": "obs-selfheal-app-1"}


@pytest.mark.parametrize("raw", ["app", "app=", "app=../etc", "=container"])
def test_parse_targets_rejects_invalid_entries(raw):
    with pytest.raises(ValueError):
        main.parse_targets(raw)


class FakeProbe:
    def __init__(self, healthy: bool = True, fail_control: bool = False) -> None:
        self.healthy = healthy
        self.fail_control = fail_control
        self.health_checks: list[str] = []
        self.shedding_calls: list[tuple[str, bool]] = []

    def wait_healthy(self, url: str, timeout_seconds: int) -> tuple[bool, float]:
        self.health_checks.append(url)
        return self.healthy, 0.1

    def set_load_shedding(self, control_url: str, enabled: bool) -> None:
        self.shedding_calls.append((control_url, enabled))
        if self.fail_control:
            raise main.RemediationError("control endpoint returned 500: boom")


def runbook_client(tmp_path, restarter, probe=None, **overrides):
    settings = make_settings(
        tmp_path,
        cooldown_seconds=0,
        health_urls={"app": "http://app:8000/health"},
        control_urls={"app": "http://app:8001"},
        **overrides,
    )
    return TestClient(main.create_app(settings, restarter, probe or FakeProbe()))


def fire(client, **kwargs):
    return client.post("/alerts", json=alert_payload(**kwargs), headers=AUTH).json()["results"][0]


def test_restart_runbook_captures_premortem_then_verifies_health(tmp_path, restarter):
    probe = FakeProbe()
    client = runbook_client(tmp_path, restarter, probe)
    assert fire(client)["status"] == "success"
    assert probe.health_checks == ["http://app:8000/health"]

    incidents = client.get("/incidents", headers=AUTH).json()["incidents"]
    assert len(incidents) == 1
    assert incidents[0]["event"] == "premortem_snapshot"
    assert incidents[0]["reason"] == "pre-restart"
    assert incidents[0]["labels"]["alertname"] == "HighCPUUsage"

    record = client.get("/audit", headers=AUTH).json()["entries"][-1]
    assert record["incident_id"] == incidents[0]["incident_id"]
    assert record["post_heal_verified"] is True


def test_restart_without_healthy_recovery_is_unverified(tmp_path, restarter):
    client = runbook_client(tmp_path, restarter, FakeProbe(healthy=False))
    assert fire(client)["status"] == "unverified"
    assert restarter.calls == ["obs-selfheal-app-1"]


def test_circuit_breaker_escalates_after_budget(tmp_path, restarter):
    client = runbook_client(tmp_path, restarter, max_remediations_per_window=2, window_seconds=600)
    assert [fire(client)["status"] for _ in range(4)] == ["success", "success", "escalated", "escalated"]
    assert restarter.calls == ["obs-selfheal-app-1"] * 2

    body = client.get("/metrics").text
    assert 'remediation_circuit_breaker_open{service="app"} 1.0' in body
    assert 'remediation_outcomes_total{outcome="escalated",service="app"} 2.0' in body
    assert client.get("/circuit-breaker", headers=AUTH).json()["services"]["app"]["state"] == "escalated"

    reasons = [i["reason"] for i in client.get("/incidents", headers=AUTH).json()["incidents"]]
    assert reasons == ["pre-restart", "pre-restart", "circuit-breaker-escalation"]


def test_circuit_breaker_reset_rearms_restarts(tmp_path, restarter):
    client = runbook_client(tmp_path, restarter, max_remediations_per_window=1)
    fire(client)
    assert fire(client)["status"] == "escalated"

    assert client.post("/circuit-breaker/reset").status_code == 401
    reset = client.post("/circuit-breaker/reset", params={"service": "app"}, headers=AUTH).json()
    assert reset["reset"] == ["app"]
    assert reset["services"]["app"]["state"] == "closed"
    assert 'remediation_circuit_breaker_open{service="app"} 0.0' in client.get("/metrics").text
    assert fire(client)["status"] == "success"
    assert client.get("/audit/verify", headers=AUTH).json()["valid"] is True


def test_failed_restarts_do_not_consume_breaker_budget(tmp_path):
    failing = FakeRestarter(fail=True)
    client = runbook_client(tmp_path, failing, max_remediations_per_window=1)
    assert [fire(client)["status"] for _ in range(3)] == ["failed"] * 3


def test_latency_runbook_sheds_load_and_restores_on_resolve(tmp_path, restarter):
    probe = FakeProbe()
    client = runbook_client(tmp_path, restarter, probe)
    shed = fire(client, alertname="HighLatencySpike", remediation="shed_load")
    restore = fire(client, status="resolved", alertname="HighLatencySpike", remediation="shed_load")

    assert (shed["action"], shed["status"]) == ("shed_load", "success")
    assert (restore["action"], restore["status"]) == ("restore_traffic", "success")
    assert probe.shedding_calls == [("http://app:8001", True), ("http://app:8001", False)]
    assert restarter.calls == []
    assert client.get("/incidents", headers=AUTH).json()["incidents"] == []


def test_latency_runbook_failure_is_audited(tmp_path, restarter):
    client = runbook_client(tmp_path, restarter, FakeProbe(fail_control=True))
    assert fire(client, alertname="HighLatencySpike", remediation="shed_load")["status"] == "failed"


def test_latency_runbook_requires_control_endpoint(client):
    assert fire(client, alertname="HighLatencySpike", remediation="shed_load")["status"] == "skipped_not_allowed"


def test_incidents_endpoint_requires_token(client):
    assert client.get("/incidents").status_code == 401
    assert client.get("/circuit-breaker").status_code == 401


def test_settings_parse_runbook_endpoints():
    settings = main.Settings.from_env(
        {
            "WEBHOOK_TOKEN": TOKEN,
            "MAX_REMEDIATIONS_PER_WINDOW": "3",
            "WINDOW_SECONDS": "300",
            "REMEDIATION_HEALTH_URLS": "app=http://app:8000/health",
            "REMEDIATION_CONTROL_URLS": "app=http://app:8001/",
            "DOCKER_RO_API_URL": "http://docker-proxy-ro:2375",
        }
    )
    assert (settings.max_remediations_per_window, settings.window_seconds) == (3, 300)
    assert settings.control_urls == {"app": "http://app:8001"}
    assert settings.docker_ro_api_url == "http://docker-proxy-ro:2375"


@pytest.mark.parametrize("raw", ["app=ftp://x", "app", "=http://x"])
def test_parse_url_map_rejects_invalid_entries(raw):
    with pytest.raises(ValueError):
        main.parse_url_map(raw, "REMEDIATION_CONTROL_URLS")
