"""Demo service monitored by the observability stack.

Public API (port 8000): business routes, /health, /metrics, a bounded CPU stress endpoint
and an async latency simulator, all with Prometheus metrics and structured JSON logs.
Control API (port 8001, never published to the host): toggles graceful load-shedding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_NAME = os.environ.get("SERVICE_NAME", "app")
STRESS_MAX_SECONDS = int(os.environ.get("STRESS_MAX_SECONDS", "300"))
SHED_RETRY_AFTER_SECONDS = int(os.environ.get("SHED_RETRY_AFTER_SECONDS", "10"))
PUBLIC_PORT = int(os.environ.get("PORT", "8000"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8001"))
# Never shed: probes, telemetry and the chaos control used to trigger demos.
SHED_EXEMPT_PATHS = frozenset({"/health", "/metrics", "/stress"})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
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


configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("app")

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests processed, by method, route template and status code.",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency, by method and route template.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
STRESS_ACTIVE = Gauge("app_stress_active", "1 while a synthetic CPU stress is running.")
STRESS_RUNS = Counter("app_stress_runs_total", "Synthetic CPU stress runs started.")
SHEDDING_ACTIVE = Gauge("app_load_shedding_active", "1 while non-critical traffic is shed with HTTP 503.")
SHED_REQUESTS = Counter("app_shed_requests_total", "Requests rejected by load-shedding.")


class StressController:
    """Burns at most one CPU core for a bounded duration in a single daemon thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._until = 0.0

    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic()) if self.active else 0.0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, seconds: int) -> bool:
        with self._lock:
            if self.active:
                return False
            self._stop.clear()
            self._until = time.monotonic() + seconds
            self._thread = threading.Thread(target=self._burn, name="cpu-stress", daemon=True)
            self._thread.start()
            STRESS_RUNS.inc()
            return True

    def stop(self) -> bool:
        with self._lock:
            was_active = self.active
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        return was_active

    def _burn(self) -> None:
        STRESS_ACTIVE.set(1)
        log.warning("cpu stress started", extra={"fields": {"event": "stress_started"}})
        try:
            while not self._stop.is_set() and time.monotonic() < self._until:
                sum(i * i for i in range(20_000))
        finally:
            STRESS_ACTIVE.set(0)
            log.warning("cpu stress finished", extra={"fields": {"event": "stress_finished"}})


stress = StressController()


class LoadShedder:
    def __init__(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set(self, enabled: bool) -> bool:
        changed = enabled != self._enabled
        self._enabled = enabled
        SHEDDING_ACTIVE.set(1 if enabled else 0)
        if changed:
            log.warning(
                "load-shedding %s",
                "enabled" if enabled else "disabled",
                extra={"fields": {"event": "load_shedding_changed", "enabled": enabled}},
            )
        return changed


shedder = LoadShedder()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("service started", extra={"fields": {"event": "startup"}})
    yield
    stress.stop()
    log.info("service stopping", extra={"fields": {"event": "shutdown"}})


app = FastAPI(title="demo-app", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        return route.path
    known = {getattr(r, "path", None) for r in app.routes}
    return request.url.path if request.url.path in known else "unmatched"


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    shed = shedder.enabled and request.url.path not in SHED_EXEMPT_PATHS
    try:
        if shed:
            SHED_REQUESTS.inc()
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return JSONResponse(
                status_code=status_code,
                content={"status": "shedding", "detail": "load-shedding active, retry later"},
                headers={"Retry-After": str(SHED_RETRY_AFTER_SECONDS)},
            )
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        path = _route_path(request)
        if path != "/metrics":
            elapsed = time.perf_counter() - started
            REQUESTS.labels(request.method, path, str(status_code)).inc()
            LATENCY.labels(request.method, path).observe(elapsed)
            if path != "/health":
                level = logging.WARNING if shed else logging.ERROR if status_code >= 500 else logging.INFO
                log.log(
                    level,
                    "request handled",
                    extra={
                        "fields": {
                            "event": "http_request",
                            "method": request.method,
                            "path": path,
                            "status": status_code,
                            "duration_ms": round(elapsed * 1000, 2),
                            "shed": shed,
                        }
                    },
                )


@app.get("/")
async def index() -> dict[str, str]:
    await asyncio.sleep(random.uniform(0.005, 0.05))  # noqa: S311
    return {"service": SERVICE_NAME, "status": "ok"}


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "stress_active": stress.active, "load_shedding": shedder.enabled}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/stress", status_code=status.HTTP_202_ACCEPTED)
async def start_stress(
    seconds: int = Query(30, ge=1, le=STRESS_MAX_SECONDS, description="Duration of the CPU burn."),
) -> JSONResponse:
    if not stress.start(seconds):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"status": "already_running", "remaining_seconds": round(stress.remaining(), 1)},
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "started", "seconds": seconds},
    )


@app.delete("/stress")
async def stop_stress() -> dict[str, object]:
    return {"status": "stopped", "was_active": await asyncio.to_thread(stress.stop)}


@app.get("/fail")
async def fail() -> JSONResponse:
    log.error("simulated dependency failure", extra={"fields": {"event": "simulated_failure"}})
    return JSONResponse(status_code=500, content={"status": "error", "detail": "simulated failure"})


@app.get("/work")
async def work(
    delay_ms: int = Query(0, ge=0, le=5000, description="Simulated downstream latency (async sleep, no CPU)."),
) -> dict[str, object]:
    await asyncio.sleep(delay_ms / 1000)
    return {"status": "ok", "delay_ms": delay_ms}


control_app = FastAPI(title="demo-app-control", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)


@control_app.get("/shed-load")
async def get_shed_load() -> dict[str, bool]:
    return {"enabled": shedder.enabled}


@control_app.post("/shed-load")
async def set_shed_load(enabled: bool = Query(..., description="true to shed non-critical traffic")) -> dict[str, bool]:
    changed = shedder.set(enabled)
    return {"enabled": shedder.enabled, "changed": changed}


async def serve() -> None:
    """Run the public and control APIs in one process so they share in-memory state."""
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PUBLIC_PORT, access_log=False, log_config=None)),
        uvicorn.Server(
            uvicorn.Config(control_app, host="0.0.0.0", port=CONTROL_PORT, access_log=False, log_config=None)
        ),
    ]
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(serve())
