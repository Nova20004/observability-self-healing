import struct

import httpx

from forensics import ForensicsCollector, demux_docker_logs


def frame(stream: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxI", stream, len(payload)) + payload


def capture(collector: ForensicsCollector) -> dict:
    return collector.capture(
        alertname="HighCPUUsage",
        severity="warning",
        target="app",
        container="obs-selfheal-app-1",
        runbook="restart",
        labels={"alertname": "HighCPUUsage", "service": "app"},
        annotations={"summary": "app CPU usage is high"},
        reason="pre-restart",
    )


def docker_transport(logs: bytes, logs_status: int = 200, inspect_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/logs"):
            assert request.url.params["tail"] == "50"
            return httpx.Response(logs_status, content=logs)
        if request.url.path.endswith("/json"):
            return httpx.Response(
                inspect_status,
                json={
                    "State": {"Status": "running", "Running": True, "ExitCode": 0, "OOMKilled": False},
                    "RestartCount": 0,
                    "Config": {"Env": ["SECRET=must-not-leak"]},
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_demux_multiplexed_stream():
    raw = frame(1, b"line one\nline two\n") + frame(2, b"an error\n")
    assert demux_docker_logs(raw) == ["line one", "line two", "an error"]


def test_demux_plain_stream_and_truncation():
    assert demux_docker_logs(b"plain\n\n" + b"x" * 5000) == ["plain", "x" * 2000]


def test_capture_collects_state_and_last_logs():
    lines = b"".join(frame(1, f"log {i}\n".encode()) for i in range(80))
    client = httpx.Client(base_url="http://proxy", transport=docker_transport(lines))
    snapshot = capture(ForensicsCollector("http://proxy", client=client))

    assert snapshot["event"] == "premortem_snapshot"
    assert snapshot["alertname"] == "HighCPUUsage"
    assert snapshot["severity"] == "warning"
    assert snapshot["target"] == "app"
    assert snapshot["annotations"] == {"summary": "app CPU usage is high"}
    assert snapshot["log_lines"] == 50
    assert snapshot["logs"][-1] == "log 79"
    assert snapshot["container_state"]["status"] == "running"
    assert "must-not-leak" not in str(snapshot)
    assert snapshot["logs_error"] is None


def test_capture_falls_back_when_container_is_gone():
    client = httpx.Client(base_url="http://proxy", transport=docker_transport(b"", logs_status=404, inspect_status=404))
    snapshot = capture(ForensicsCollector("http://proxy", client=client))
    assert snapshot["logs"] == []
    assert snapshot["container_state"] is None
    assert "404" in snapshot["logs_error"]


def test_capture_survives_unreachable_proxy():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(base_url="http://proxy", transport=httpx.MockTransport(boom))
    snapshot = capture(ForensicsCollector("http://proxy", client=client))
    assert snapshot["logs_error"].startswith("ConnectError")


def test_capture_without_proxy_is_explicit():
    snapshot = capture(ForensicsCollector(None))
    assert "not configured" in snapshot["logs_error"]


def test_ring_buffer_keeps_last_snapshots():
    collector = ForensicsCollector(None, buffer_size=3)
    ids = [capture(collector)["incident_id"] for _ in range(5)]
    assert [s["incident_id"] for s in collector.recent(50)] == ids[-3:]
    assert len(collector.recent(1)) == 1
