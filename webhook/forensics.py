"""Pre-mortem forensics: capture container state and recent logs before a destructive action.

Reads go through the read-only Docker socket proxy (GET only). Failures never block remediation:
the snapshot is still emitted with a `logs_error` explaining what could not be collected.
"""

from __future__ import annotations

import logging
import struct
import threading
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("webhook.forensics")

MAX_LINE_CHARS = 2000


def demux_docker_logs(raw: bytes) -> list[str]:
    """Decode the Docker log stream (multiplexed 8-byte frame headers when the container has no TTY)."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        chunks: list[bytes] = []
        offset = 0
        while offset + 8 <= len(raw):
            (size,) = struct.unpack(">I", raw[offset + 4 : offset + 8])
            chunks.append(raw[offset + 8 : offset + 8 + size])
            offset += 8 + size
        raw = b"".join(chunks)
    text = raw.decode("utf-8", errors="replace")
    return [line[:MAX_LINE_CHARS] for line in text.splitlines() if line.strip()]


class ForensicsCollector:
    def __init__(
        self,
        docker_ro_api_url: str | None,
        tail_lines: int = 50,
        buffer_size: int = 50,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._tail = tail_lines
        if client is None and docker_ro_api_url:
            client = httpx.Client(base_url=docker_ro_api_url, timeout=timeout_seconds)
        self._client = client
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()

    @staticmethod
    def _container_state(client: httpx.Client, container: str) -> dict[str, Any] | None:
        response = client.get(f"/containers/{container}/json")
        if response.status_code != 200:
            return None
        body = response.json()
        state = body.get("State", {})
        # Only lifecycle fields: Config.Env and mounts may contain sensitive data and are never copied.
        return {
            "status": state.get("Status"),
            "running": state.get("Running"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": body.get("RestartCount"),
        }

    def _logs(self, client: httpx.Client, container: str) -> list[str]:
        response = client.get(
            f"/containers/{container}/logs",
            params={"stdout": 1, "stderr": 1, "tail": self._tail, "timestamps": 1},
        )
        if response.status_code != 200:
            raise RuntimeError(f"docker API returned {response.status_code} for logs")
        return demux_docker_logs(response.content)[-self._tail :]

    def capture(
        self,
        *,
        alertname: str,
        severity: str,
        target: str,
        container: str,
        runbook: str,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        reason: str,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "event": "premortem_snapshot",
            "incident_id": uuid.uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "alertname": alertname,
            "severity": severity,
            "target": target,
            "container": container,
            "runbook": runbook,
            "reason": reason,
            "labels": dict(labels),
            "annotations": dict(annotations),
            "container_state": None,
            "logs": [],
            "log_lines": 0,
            "logs_error": None,
        }
        if self._client is None:
            snapshot["logs_error"] = "forensics disabled: DOCKER_RO_API_URL not configured"
        else:
            try:
                snapshot["container_state"] = self._container_state(self._client, container)
                snapshot["logs"] = self._logs(self._client, container)
                snapshot["log_lines"] = len(snapshot["logs"])
            except Exception as exc:  # noqa: BLE001 - forensics must never block remediation
                snapshot["logs_error"] = f"{type(exc).__name__}: {exc}"

        with self._lock:
            self._buffer.append(snapshot)
        log.warning("pre-mortem snapshot captured", extra={"fields": snapshot})
        return snapshot

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)[-limit:]
