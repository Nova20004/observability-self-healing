"""Authenticated remediation webhook for Alertmanager.

Flow per alert: verify bearer token -> dispatch on the `remediation` label (runbook) ->
resolve the target from an explicit allowlist -> run the runbook:

* restart:   cooldown -> sliding-window circuit breaker -> pre-mortem snapshot ->
             restart via the restart-only Docker proxy -> post-heal /health verification
* shed_load: enable graceful load-shedding on the target (disabled again when the alert resolves)

Every decision is appended to a hash-chained audit trail (file + stdout for Loki) and exported as metrics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from circuit_breaker import CircuitBreaker
from forensics import ForensicsCollector

SERVICE_NAME = "webhook"
MIN_TOKEN_LENGTH = 24
MAX_BODY_BYTES = 1_048_576
GENESIS_HASH = "0" * 64
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": SERVICE_NAME,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers[:] = []
        uv_logger.propagate = True


log = logging.getLogger("webhook")
audit_logger = logging.getLogger("webhook.audit")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_targets(raw: str) -> dict[str, str]:
    """Parse `service=container,service2=container2` into an allowlist."""
    targets: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        service, sep, container = item.partition("=")
        service, container = service.strip(), container.strip()
        if not sep or not service or not CONTAINER_NAME_RE.match(container):
            raise ValueError(f"invalid REMEDIATION_TARGETS entry: {item!r}")
        targets[service] = container
    return targets


def parse_url_map(raw: str, variable: str) -> dict[str, str]:
    """Parse `service=http://host:port/path,...`; only http(s) URLs are accepted."""
    urls: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        service, sep, url = item.partition("=")
        service, url = service.strip(), url.strip()
        if not sep or not service or not re.match(r"^https?://[^\s/]+", url):
            raise ValueError(f"invalid {variable} entry: {item!r}")
        urls[service] = url.rstrip("/")
    return urls


@dataclass(frozen=True)
class Settings:
    token: str
    cooldown_seconds: int
    targets: dict[str, str]
    docker_api_url: str
    audit_log_path: Path
    dry_run: bool
    restart_timeout_seconds: int = 10
    max_remediations_per_window: int = 2
    window_seconds: int = 600
    docker_ro_api_url: str | None = None
    health_urls: dict[str, str] = field(default_factory=dict)
    control_urls: dict[str, str] = field(default_factory=dict)
    heal_verify_timeout_seconds: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        token = env.get("WEBHOOK_TOKEN", "")
        token_file = env.get("WEBHOOK_TOKEN_FILE")
        if token_file:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"webhook token must be at least {MIN_TOKEN_LENGTH} characters")
        return cls(
            token=token,
            cooldown_seconds=int(env.get("REMEDIATION_COOLDOWN_SECONDS", "120")),
            targets=parse_targets(env.get("REMEDIATION_TARGETS", "")),
            docker_api_url=env.get("DOCKER_API_URL", "http://docker-proxy-restart:2375"),
            audit_log_path=Path(env.get("AUDIT_LOG_PATH", "/var/log/remediation/audit.jsonl")),
            dry_run=env.get("REMEDIATION_DRY_RUN", "false").lower() in {"1", "true", "yes"},
            restart_timeout_seconds=int(env.get("RESTART_TIMEOUT_SECONDS", "10")),
            max_remediations_per_window=int(env.get("MAX_REMEDIATIONS_PER_WINDOW", "2")),
            window_seconds=int(env.get("WINDOW_SECONDS", "600")),
            docker_ro_api_url=env.get("DOCKER_RO_API_URL") or None,
            health_urls=parse_url_map(env.get("REMEDIATION_HEALTH_URLS", ""), "REMEDIATION_HEALTH_URLS"),
            control_urls=parse_url_map(env.get("REMEDIATION_CONTROL_URLS", ""), "REMEDIATION_CONTROL_URLS"),
            heal_verify_timeout_seconds=int(env.get("HEAL_VERIFY_TIMEOUT_SECONDS", "30")),
        )


def _digest(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only JSONL audit trail; each record embeds the hash of the previous one."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._load_last_hash()

    def _lines(self) -> list[str]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as fh:
            return [line for line in fh if line.strip()]

    def _load_last_hash(self) -> str:
        lines = self._lines()
        return json.loads(lines[-1])["hash"] if lines else GENESIS_HASH

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = {**entry, "prev_hash": self._last_hash}
            record["hash"] = _digest(record)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last_hash = record["hash"]
        audit_logger.info("remediation audit", extra={"fields": record})
        return record

    def tail(self, limit: int) -> list[dict[str, Any]]:
        return [json.loads(line) for line in deque(self._lines(), maxlen=limit)]

    def verify(self) -> dict[str, Any]:
        previous = GENESIS_HASH
        lines = self._lines()
        for index, line in enumerate(lines):
            record = json.loads(line)
            claimed = record.pop("hash", None)
            if record.get("prev_hash") != previous or _digest(record) != claimed:
                return {"valid": False, "entries": len(lines), "first_invalid_index": index}
            previous = claimed
        return {"valid": True, "entries": len(lines), "first_invalid_index": None}


class CooldownTracker:
    def __init__(self, seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> float:
        """Reserve the slot and return 0, or return the remaining cooldown in seconds."""
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self._seconds:
                return self._seconds - (now - last)
            self._last[key] = now
            return 0.0

    def release(self, key: str) -> None:
        with self._lock:
            self._last.pop(key, None)


class ContainerRestarter(Protocol):
    def restart(self, container: str) -> None: ...


class AppProbe(Protocol):
    def wait_healthy(self, url: str, timeout_seconds: int) -> tuple[bool, float]: ...

    def set_load_shedding(self, control_url: str, enabled: bool) -> None: ...


class RemediationError(RuntimeError):
    pass


class DockerProxyRestarter:
    def __init__(self, base_url: str, stop_timeout_seconds: int) -> None:
        self._stop_timeout = stop_timeout_seconds
        self._client = httpx.Client(base_url=base_url, timeout=stop_timeout_seconds + 20)

    def restart(self, container: str) -> None:
        try:
            response = self._client.post(f"/containers/{container}/restart", params={"t": self._stop_timeout})
        except httpx.HTTPError as exc:
            raise RemediationError(f"docker API unreachable: {exc}") from exc
        if response.status_code != 204:
            raise RemediationError(f"docker API returned {response.status_code}: {response.text[:200]}")


class HttpAppProbe:
    def __init__(self, timeout_seconds: float = 3.0, poll_interval_seconds: float = 1.0) -> None:
        self._client = httpx.Client(timeout=timeout_seconds)
        self._poll = poll_interval_seconds

    def wait_healthy(self, url: str, timeout_seconds: int) -> tuple[bool, float]:
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            try:
                if self._client.get(url).status_code == 200:
                    return True, round(time.monotonic() - started, 3)
            except httpx.HTTPError:
                pass
            time.sleep(self._poll)
        return False, round(time.monotonic() - started, 3)

    def set_load_shedding(self, control_url: str, enabled: bool) -> None:
        try:
            response = self._client.post(f"{control_url}/shed-load", params={"enabled": str(enabled).lower()})
        except httpx.HTTPError as exc:
            raise RemediationError(f"control endpoint unreachable: {exc}") from exc
        if response.status_code != 200:
            raise RemediationError(f"control endpoint returned {response.status_code}: {response.text[:200]}")


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None  # noqa: N815 - Alertmanager wire format
    fingerprint: str | None = None


class AlertmanagerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    receiver: str | None = None
    alerts: list[Alert]


OUTCOMES = (
    "success",
    "failed",
    "unverified",
    "escalated",
    "skipped_cooldown",
    "skipped_not_allowed",
    "skipped_no_action",
    "resolved",
    "dry_run",
)


class Metrics:
    def __init__(self, targets: list[str]) -> None:
        self.registry = CollectorRegistry()
        self.actions = Counter(
            "remediation_actions_total",
            "Remediation decisions, by alert, target service, action and outcome.",
            ["alertname", "target", "action", "status"],
            registry=self.registry,
        )
        self.outcomes = Counter(
            "remediation_outcomes_total",
            "Remediation decisions by service and outcome, pre-initialised to 0 for increase().",
            ["service", "outcome"],
            registry=self.registry,
        )
        self.breaker_open = Gauge(
            "remediation_circuit_breaker_open",
            "1 while the service's circuit breaker is ESCALATED and automated restarts are refused.",
            ["service"],
            registry=self.registry,
        )
        for target in targets:
            self.breaker_open.labels(target).set(0)
            for outcome in OUTCOMES:
                self.outcomes.labels(target, outcome)
        self.auth_failures = Counter(
            "webhook_auth_failures_total",
            "Requests rejected because of a missing or invalid bearer token.",
            registry=self.registry,
        )
        self.last_success = Gauge(
            "remediation_last_success_timestamp_seconds",
            "Unix time of the last successful remediation, by target service.",
            ["target"],
            registry=self.registry,
        )


class Remediator:
    def __init__(
        self,
        settings: Settings,
        restarter: ContainerRestarter,
        audit: AuditLog,
        metrics: Metrics,
        cooldown: CooldownTracker,
        breaker: CircuitBreaker,
        forensics: ForensicsCollector,
        probe: AppProbe,
    ) -> None:
        self._settings = settings
        self._restarter = restarter
        self._audit = audit
        self._metrics = metrics
        self._cooldown = cooldown
        self._breaker = breaker
        self._forensics = forensics
        self._probe = probe

    def handle(self, alert: Alert) -> dict[str, Any]:
        labels = alert.labels
        service = labels.get("service", "")
        runbook = labels.get("remediation", "none")
        base = {
            "event": "remediation_audit",
            "timestamp": utc_now(),
            "alertname": labels.get("alertname", "unknown"),
            "severity": labels.get("severity", "unknown"),
            "target": service,
            "runbook": runbook,
            "alert_status": alert.status,
            "fingerprint": alert.fingerprint,
            "starts_at": alert.startsAt,
            "dry_run": self._settings.dry_run,
        }

        if runbook == "restart":
            if alert.status != "firing":
                return self._record(base, "none", "resolved", "alert resolved, no action required")
            return self._restart(alert, base)
        if runbook == "shed_load":
            return self._shed_load(base, enabled=alert.status == "firing")
        if alert.status != "firing":
            return self._record(base, "none", "resolved", "alert resolved, no action required")
        return self._record(base, "none", "skipped_no_action", "alert does not request remediation")

    def _restart(self, alert: Alert, base: dict[str, Any]) -> dict[str, Any]:
        service = base["target"]
        container = self._settings.targets.get(service)
        if container is None:
            return self._record(base, "restart", "skipped_not_allowed", f"service {service!r} is not in the allowlist")
        base["container"] = container

        if self._breaker.state(service) == "escalated":
            return self._record(
                base, "restart", "escalated", "circuit breaker open: restart refused, on-call escalated"
            )

        remaining = self._cooldown.try_acquire(container)
        if remaining > 0:
            base["cooldown_remaining_seconds"] = round(remaining, 1)
            return self._record(base, "restart", "skipped_cooldown", "restart suppressed by cooldown window")

        decision = self._breaker.acquire(service)
        if not decision.allowed:
            if decision.just_escalated:
                self._escalate(alert, base, container, decision.recent_actions)
            return self._record(
                base, "restart", "escalated", "circuit breaker open: restart refused, on-call escalated"
            )

        if self._settings.dry_run:
            return self._record(base, "restart", "dry_run", "dry-run mode, container not restarted")

        snapshot = self._capture(alert, base, container, reason="pre-restart")
        base["incident_id"] = snapshot["incident_id"]

        started = time.monotonic()
        try:
            self._restarter.restart(container)
        except Exception as exc:  # noqa: BLE001 - every failure must be audited
            self._cooldown.release(container)
            self._breaker.rollback(service)
            return self._record(base, "restart", "failed", str(exc))
        base["duration_seconds"] = round(time.monotonic() - started, 3)

        health_url = self._settings.health_urls.get(service)
        if health_url is None:
            base["post_heal_verified"] = None
        else:
            healthy, waited = self._probe.wait_healthy(health_url, self._settings.heal_verify_timeout_seconds)
            base["post_heal_verified"] = healthy
            base["post_heal_seconds"] = waited
            if not healthy:
                return self._record(
                    base, "restart", "unverified", f"{container} restarted but {health_url} not healthy"
                )

        self._metrics.last_success.labels(service).set(time.time())
        return self._record(base, "restart", "success", f"container {container} restarted")

    def _shed_load(self, base: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
        action = "shed_load" if enabled else "restore_traffic"
        service = base["target"]
        control_url = self._settings.control_urls.get(service)
        if control_url is None:
            return self._record(base, action, "skipped_not_allowed", f"service {service!r} has no control endpoint")
        if self._settings.dry_run:
            return self._record(base, action, "dry_run", "dry-run mode, load-shedding unchanged")
        try:
            self._probe.set_load_shedding(control_url, enabled)
        except Exception as exc:  # noqa: BLE001 - every failure must be audited
            return self._record(base, action, "failed", str(exc))
        state = "enabled" if enabled else "disabled"
        return self._record(base, action, "success", f"load-shedding {state} on {service}")

    def _capture(self, alert: Alert, base: dict[str, Any], container: str, reason: str) -> dict[str, Any]:
        return self._forensics.capture(
            alertname=base["alertname"],
            severity=base["severity"],
            target=base["target"],
            container=container,
            runbook=base["runbook"],
            labels=alert.labels,
            annotations=alert.annotations,
            reason=reason,
        )

    def _escalate(self, alert: Alert, base: dict[str, Any], container: str, recent: int) -> None:
        service = base["target"]
        snapshot = self._capture(alert, base, container, reason="circuit-breaker-escalation")
        base["incident_id"] = snapshot["incident_id"]
        self._metrics.breaker_open.labels(service).set(1)
        log.critical(
            "circuit breaker escalated: automated restarts disabled, human intervention required",
            extra={
                "fields": {
                    "event": "circuit_breaker_escalated",
                    "alertname": base["alertname"],
                    "target": service,
                    "container": container,
                    "incident_id": snapshot["incident_id"],
                    "recent_restarts": recent,
                    "max_restarts": self._breaker.max_actions,
                    "window_seconds": self._breaker.window_seconds,
                }
            },
        )

    def reset_breaker(self, service: str | None) -> list[str]:
        services = self._breaker.reset(service)
        for name in services:
            self._metrics.breaker_open.labels(name).set(0)
            self._cooldown.release(self._settings.targets.get(name, name))
            self._audit.append(
                {
                    "event": "remediation_audit",
                    "timestamp": utc_now(),
                    "alertname": "operator",
                    "target": name,
                    "action": "circuit_breaker_reset",
                    "status": "success",
                    "detail": "circuit breaker re-armed by operator",
                }
            )
        log.warning("circuit breaker reset", extra={"fields": {"event": "circuit_breaker_reset", "services": services}})
        return services

    def _record(self, base: dict[str, Any], action: str, outcome: str, detail: str) -> dict[str, Any]:
        target = base["target"] or "unknown"
        self._metrics.actions.labels(base["alertname"], target, action, outcome).inc()
        self._metrics.outcomes.labels(target, outcome).inc()
        return self._audit.append({**base, "action": action, "status": outcome, "detail": detail})


def create_app(
    settings: Settings | None = None,
    restarter: ContainerRestarter | None = None,
    probe: AppProbe | None = None,
    forensics: ForensicsCollector | None = None,
) -> FastAPI:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = settings or Settings.from_env()
    restarter = restarter or DockerProxyRestarter(settings.docker_api_url, settings.restart_timeout_seconds)
    probe = probe or HttpAppProbe()
    forensics = forensics or ForensicsCollector(settings.docker_ro_api_url)
    breaker = CircuitBreaker(settings.max_remediations_per_window, settings.window_seconds)
    metrics = Metrics(sorted(settings.targets))
    audit = AuditLog(settings.audit_log_path)
    remediator = Remediator(
        settings, restarter, audit, metrics, CooldownTracker(settings.cooldown_seconds), breaker, forensics, probe
    )
    expected_auth = f"Bearer {settings.token}".encode()

    app = FastAPI(title="remediation-webhook", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)

    def require_token(request: Request) -> None:
        provided = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(provided, expected_auth):
            metrics.auth_failures.inc()
            client = request.client.host if request.client else "unknown"
            log.warning(
                "rejected unauthenticated request", extra={"fields": {"event": "auth_failure", "client": client}}
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/alerts")
    async def receive_alerts(request: Request) -> dict[str, Any]:
        require_token(request)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="payload too large")
        try:
            payload = AlertmanagerPayload.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid Alertmanager payload"
            ) from exc

        results = [await run_in_threadpool(remediator.handle, alert) for alert in payload.alerts]
        return {
            "processed": len(results),
            "results": [{k: r.get(k) for k in ("alertname", "target", "action", "status")} for r in results],
        }

    @app.get("/audit")
    async def read_audit(request: Request, limit: int = Query(50, ge=1, le=1000)) -> dict[str, Any]:
        require_token(request)
        return {"entries": await run_in_threadpool(audit.tail, limit)}

    @app.get("/audit/verify")
    async def verify_audit(request: Request) -> dict[str, Any]:
        require_token(request)
        return await run_in_threadpool(audit.verify)

    @app.get("/incidents")
    async def read_incidents(request: Request, limit: int = Query(50, ge=1, le=50)) -> dict[str, Any]:
        require_token(request)
        return {"incidents": forensics.recent(limit)}

    @app.get("/circuit-breaker")
    async def read_breaker(request: Request) -> dict[str, Any]:
        require_token(request)
        return {"services": breaker.snapshot(sorted(settings.targets))}

    @app.post("/circuit-breaker/reset")
    async def reset_breaker(request: Request, service: str | None = Query(None, max_length=128)) -> dict[str, Any]:
        require_token(request)
        reset = await run_in_threadpool(remediator.reset_breaker, service)
        return {"reset": reset, "services": breaker.snapshot(sorted(settings.targets))}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "dry_run": settings.dry_run, "targets": sorted(settings.targets)}

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    log.info(
        "remediation webhook ready",
        extra={
            "fields": {
                "event": "startup",
                "targets": settings.targets,
                "cooldown_seconds": settings.cooldown_seconds,
                "max_remediations_per_window": settings.max_remediations_per_window,
                "window_seconds": settings.window_seconds,
                "forensics_enabled": settings.docker_ro_api_url is not None,
                "dry_run": settings.dry_run,
            }
        },
    )
    return app
