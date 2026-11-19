# Observability & Self-Healing Stack

[![CI](https://github.com/Nova20004/observability-self-healing/actions/workflows/ci.yml/badge.svg)](https://github.com/Nova20004/observability-self-healing/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Prometheus](https://img.shields.io/badge/Prometheus-v3.16.0-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-13.3.1-F46800?logo=grafana&logoColor=white)
![Loki](https://img.shields.io/badge/Loki-3.6.22-F2CC0C?logo=grafana&logoColor=black)
![Alertmanager](https://img.shields.io/badge/Alertmanager-v0.35.0-E6522C?logo=prometheus&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![RAM](https://img.shields.io/badge/RAM-~520%20MB-success)

A laptop-friendly, cloud-free reference implementation of **metrics + logs + alerting + automated remediation**
with the guard-rails a production on-call team expects:

- **Multi-scenario runbooks.** A CPU fault or a dead service triggers a restart; a latency spike triggers graceful load-shedding, and nothing gets killed.
- **Pre-mortem forensics.** The container's state and last 50 log lines are captured *before* it is restarted, so the evidence survives the fix.
- **Sliding-window circuit breaker.** After 2 restarts in 10 minutes, the system stops restarting and pages a human instead of crash-looping.
- **Tamper-evident audit trail.** Every decision is hash-chained, shipped to Loki, and visible in Grafana.

Measured on an 8-core arm64 MacBook (Docker Desktop):

| Scenario | Detection | Action | Fully recovered |
|---|---|---|---|
| CPU saturation → restart | alert fires after ~38 s | restart + `/health` 200 in ~1 s | ~58 s |
| Latency spike → load-shedding | alert fires after ~35 s | shedding on, no restart | ~81 s (traffic restored) |
| Recurring fault → circuit breaker | 3rd restart refused | ESCALATED + on-call alert | operator reset |

## Architecture

```text
                    ┌──────────────────────────── observability network ────────────────────────────┐
                    │                                                                                │
 POST /stress   ┌───┴────────────────┐ scrape /metrics ┌──────────────┐  alerts  ┌──────────────┐     │
 GET  /work ──► │ app                │◄────────────────│  Prometheus  │─────────►│ Alertmanager │     │
                │ :8000 public API   │                 │  alerts.yml  │          └──────┬───────┘     │
                │ :8001 control API ◄┼─── POST /shed-load ─────────┐  └──┬────────▲───┘    │ Bearer token
                │      (not published)                         │     │        │        ▼             │
                └───┬────────────▲───┘                         │     │   node-exporter ┌────────────┴──┐
                    │ stdout     │ restart                     └─────┼──────────────── │ webhook :8080 │
                    ▼            │                                   │                 │ runbooks      │
              ┌──────────┐  ┌────┴─────────────┐  logs + state (GET) │                 │ breaker       │
              │ Promtail │  │ docker-proxy-    │  ┌──────────────────┴──┐              │ forensics     │
              └────┬─────┘  │ restart          │  │ docker-proxy-ro     │◄─────────────│ audit chain   │
                   │        │ restart/stop/kill│  │ GET only            │              └──────┬────────┘
                   ▼        └────────┬─────────┘  └─────────┬───────────┘                     │ stdout
              ┌──────────┐           │  internal networks    │                                 │
              │   Loki   │◄──────────┼───────────────────────┼──────── Promtail ◄──────────────┘
              └────┬─────┘           ▼                       ▼
                   │          /var/run/docker.sock (ro) ◄────┘
              ┌────┴─────┐
              │ Grafana  │  provisioned datasources + "Service Overview & Self-Healing" dashboard
              └──────────┘
```

All host ports bind to `127.0.0.1`. The app's control API (`:8001`) is never published, so only containers on the
Docker network can toggle load-shedding. A `loadgen` container sends ~1 req/s so dashboards always show live traffic.

## Quickstart

Requirements: Docker with Compose v2, GNU make, `curl`, `python3`.

> **macOS:** use `gmake` (`brew install make`). Plain `make` invokes the Xcode Command Line Tools installer when they are missing.

```bash
gmake env     # 1. generate .env with random Grafana password + webhook token (gitignored, mode 600)
gmake up      # 2. build and start the stack, block until every healthcheck passes
gmake demo    # 3. prove every runbook end-to-end: restart, load-shedding, circuit breaker (~8 min)
gmake down    #    stop everything (volumes kept; `gmake clean` deletes them)
```

Grafana: <http://127.0.0.1:3000> (user `admin`, password in `.env`). The **Service Overview & Self-Healing** dashboard is the home page.

| Command | Purpose |
|---|---|
| `gmake validate` | compose config, `promtool check config/rules`, `promtool test rules`, `amtool check-config`, yamllint, ruff, hadolint, shellcheck, pytest |
| `gmake test-remediation` | Restart runbook: pre-mortem → restart → post-heal `/health` verification |
| `gmake demo-load-shedding` | Latency runbook: shedding on while firing, traffic restored on resolve |
| `gmake demo-circuit-breaker` | Recurring fault → 2 restarts → ESCALATED → operator reset (~3 min) |
| `gmake incidents` | Last pre-mortem snapshots (`LIMIT=5`) |
| `gmake circuit-breaker` / `gmake circuit-breaker-reset SERVICE=app` | Inspect / re-arm the breaker |
| `gmake logs` / `gmake ps` / `gmake status` | Operate the stack |

Validation tools run in pinned containers: nothing else needs to be installed.

## Multi-scenario runbooks

The `remediation` label on an alert rule selects the runbook. Alerts without it are audited and never acted on.

| Alert | Condition | Severity | `remediation` | Runbook |
|---|---|---|---|---|
| `ServiceDown` | `up{job="app"} == 0` for 20 s | critical | `restart` | pre-mortem snapshot → restart → post-heal `/health` 200 |
| `HighCPUUsage` | `rate(process_cpu_seconds_total[30s]) > 0.5` for 15 s | warning | `restart` | pre-mortem snapshot → restart → post-heal `/health` 200 |
| `HighLatencySpike` | p95 of `http_request_duration_seconds` > 250 ms for 15 s | warning | `shed_load` | `POST :8001/shed-load?enabled=true`; `enabled=false` when resolved |
| `RemediationCircuitBreakerOpen` | `remediation_circuit_breaker_open == 1` | critical | — | page a human (route it to Slack/PagerDuty) |

Alertmanager inhibition prevents conflicting actions: `ServiceDown` suppresses CPU and latency alerts for the same service,
and `HighCPUUsage` suppresses `HighLatencySpike`, because a restart supersedes shedding when CPU explains the latency.

**Load-shedding** returns `503` with `Retry-After: 10` on business routes, while `/health`, `/metrics` and `/stress` keep answering.
`app_load_shedding_active` exposes the state. Shedding toggles in-process state only: no connection is dropped and the container keeps running.
Simulate latency with `GET /work?delay_ms=400`, which uses an async sleep and burns zero CPU.

## How a restart works

```text
alert ─► token check ─► allowlist ─► breaker ESCALATED? ──yes──► refuse (outcome=escalated)
                                          │ no
                                          ▼
                                    cooldown (30 s debounce) ──active──► skipped_cooldown
                                          │
                                          ▼
                                    window budget left? ──no──► ESCALATE: snapshot + CRITICAL log + gauge=1
                                          │ yes
                                          ▼
                       pre-mortem snapshot (state + last 50 log lines via docker-proxy-ro)
                                          ▼
                       POST /containers/<name>/restart via docker-proxy-restart
                                          ▼
                       poll http://app:8000/health until 200 (30 s) ──fail──► outcome=unverified
                                          ▼
                       outcome=success ─► hash-chained audit (file + Loki) + metrics
```

### Pre-mortem forensics (`webhook/forensics.py`)

Before any restart, and again when the breaker escalates, the webhook records:

- the UTC timestamp, alert name, severity, target service, runbook and reason, plus the full alert labels and annotations;
- the container lifecycle state (status, exit code, `OOMKilled`, health, restart count). `Config.Env` and mounts are never copied;
- the **last 50 log lines** (stdout + stderr, demultiplexed from the Docker stream). If the container is gone or the proxy fails, the snapshot is still emitted with `logs_error`. Forensics never blocks remediation.

Each snapshot is emitted as one JSON log line with `event="premortem_snapshot"` (Promtail turns `event` into a Loki label).
The last 50 snapshots are kept in a ring buffer at `GET /incidents` (Bearer-protected). Audit records carry the matching `incident_id`.

```bash
gmake incidents LIMIT=1
# Loki: {service="webhook", event=~"premortem_snapshot|circuit_breaker_escalated"} | json
```

### Circuit breaker (`webhook/circuit_breaker.py`)

```text
             restart allowed (< MAX_REMEDIATIONS_PER_WINDOW in WINDOW_SECONDS)
            ┌──────────┐
            ▼          │
        ┌────────┐ ────┘          budget exhausted          ┌─────────────┐
        │ CLOSED │ ─────────────────────────────────────►   │  ESCALATED  │ ── every restart refused
        └────────┘                                          └─────────────┘
            ▲          POST /circuit-breaker/reset (operator)       │
            └───────────────────────────────────────────────────────┘
```

- Sliding window per service: default **2 restarts per 600 s** (`MAX_REMEDIATIONS_PER_WINDOW`, `WINDOW_SECONDS` in `.env`).
- On escalation, the webhook captures a pre-mortem snapshot, sets `remediation_circuit_breaker_open{service="app"} = 1`, emits a
  `CRITICAL` log with `event="circuit_breaker_escalated"`, and increments `remediation_outcomes_total{service="app",outcome="escalated"}`. The
  `RemediationCircuitBreakerOpen` alert then fires.
- ESCALATED does **not** time out: a human must confirm the root cause is fixed (`gmake circuit-breaker-reset SERVICE=app`).
- Failed restart attempts are rolled back and do not consume budget. The 30 s cooldown remains as a debounce against duplicate notifications.
- Breaker state lives in memory and is re-armed when the webhook restarts. See the production notes below.

### Audit trail

Every decision (`success`, `unverified`, `failed`, `escalated`, `skipped_cooldown`, `skipped_not_allowed`, `skipped_no_action`,
`resolved`, `dry_run`, `circuit_breaker_reset`) is appended to `/var/log/remediation/audit.jsonl` (named volume, `fsync`ed) and logged to stdout:

```json
{"timestamp":"2026-11-19T13:38:17.156+00:00","event":"remediation_audit","alertname":"HighCPUUsage","runbook":"restart",
 "target":"app","container":"obs-selfheal-app-1","action":"restart","status":"success","incident_id":"257ba625…",
 "post_heal_verified":true,"post_heal_seconds":1.018,"prev_hash":"e4558999…","hash":"f4cde4a0…"}
```

Each record embeds the SHA-256 of the previous one. `GET /audit/verify` detects any edit or deletion; `GET /audit?limit=50` lists recent entries.
`REMEDIATION_DRY_RUN=true` exercises the whole pipeline without acting.

## Security model

| Component | Docker API access |
|---|---|
| `docker-proxy-restart` (webhook) | `POST /containers/<name>/(restart\|stop\|kill)` only. `CONTAINERS`, `EXEC`, `IMAGES`, `VOLUMES` = 0 |
| `docker-proxy-ro` (webhook forensics, promtail discovery) | `GET` on containers (inspect + logs) and networks. `POST` = 0 |

Verified on the running stack: through the restart proxy, `containers/create`, `exec`, `logs`, `images` and `volumes` all return **403**;
through the read-only proxy, logs return 200 and any `POST` returns 403.

Two deliberate deviations from the usual proxy recipes:
- `tecnativa/docker-socket-proxy` has no `LOGS` switch. Logs live under `GET /containers/{id}/logs` and are covered by `CONTAINERS=1` on the GET-only proxy.
- The restart proxy keeps `CONTAINERS=0`. Its haproxy rule allows every path under `/containers` once `CONTAINERS=1`, so with `POST=1` it would also expose `containers/create` and `exec`.

Other controls:
- No secrets in git. Secrets reach containers as Compose secrets (`/run/secrets/*`), not environment variables, so the GET-only proxy cannot leak them through container inspect.
- All containers set `no-new-privileges`. App, webhook, loadgen and node-exporter also drop all capabilities.
  The app, loadgen and node-exporter run with a read-only rootfs. The webhook cannot, because Compose refuses to inject environment-sourced secrets into a read-only rootfs; it runs as uid 10001 instead.
- Grafana: anonymous access and sign-up disabled, default `admin/admin` rejected.

## Resource budget (Mac-safe)

No `pid: host`, no `network_mode: host`, no host root bind-mounts. Every service has `mem_limit` and `cpus`, and Go services set
`GOMEMLIMIT` so their garbage collector stays below the cap instead of being OOM-killed.

| Service | mem_limit | Measured | Service | mem_limit | Measured |
|---|---|---|---|---|---|
| grafana | 192m | ~125 MiB | loki | 96m | ~44 MiB |
| prometheus | 160m | ~124 MiB | docker-proxy-ro | 48m | ~30 MiB |
| app | 96m | ~63 MiB | alertmanager | 48m | ~26 MiB |
| webhook | 64m | ~45 MiB | docker-proxy-restart | 32m | ~23 MiB |
| promtail | 48m | ~17 MiB | loadgen / node-exporter | 24m each | ~14 / ~11 MiB |

**≈520 MiB measured working set; 832 MiB hard ceiling.** CPU stress is capped at one core and 300 s.

## macOS (local) vs Linux VPS

| | macOS Docker Desktop (default) | Linux VPS |
|---|---|---|
| node-exporter scope | The Docker Desktop **Linux VM** (CPU, memory, load), which is what actually constrains the containers | Container view by default. For true host metrics, add `pid: host` plus `/proc`, `/sys` and `/` read-only mounts with `--path.rootfs=/host` in a Linux-only override file |
| Docker socket | Provided by Docker Desktop at `/var/run/docker.sock` | Same path. Consider rootless Docker; the proxies are unchanged |
| Exposure | `127.0.0.1` only | Keep `127.0.0.1` and put a reverse proxy with TLS + SSO in front of Grafana. Never publish `:8080` or `:8001` |
| Make | `gmake` (Homebrew) | `make` |
| Sizing | Defaults above | Raise retention (`--storage.tsdb.retention.time`, Loki `retention_period`) and memory caps together |

Example Linux-only override, kept out of the default file because those mounts stall Docker Desktop:

```yaml
# docker-compose.linux.yml — docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d
services:
  node-exporter:
    pid: host
    command: ["--path.rootfs=/host"]
    volumes: ["/:/host:ro,rslave"]
```

## Company adaptation guide

**Monitor your own service.** Expose Prometheus metrics, add a scrape job with a `service` label, and label the container `logging: promtail`.

**Make it self-healing.** Add `remediation: restart` or `remediation: shed_load` to the rule, then allowlist the service in the webhook environment:

```yaml
REMEDIATION_TARGETS: app=obs-selfheal-app-1,billing-api=billing-api-1
REMEDIATION_HEALTH_URLS: app=http://app:8000/health,billing-api=http://billing-api:9000/healthz
REMEDIATION_CONTROL_URLS: app=http://app:8001
```

**Page humans as well.** Add receivers in `monitoring/alertmanager/alertmanager.yml` and use `continue: true` so that remediation and notification both happen:

```yaml
routes:
  - receiver: remediation-webhook
    matchers: ['remediation=~"restart|shed_load"']
    continue: true
  - receiver: pagerduty
    matchers: ['alertname="RemediationCircuitBreakerOpen"']
  - receiver: slack-oncall
    matchers: ['severity=~"critical|warning"']
receivers:
  - name: slack-oncall
    slack_configs:
      - api_url_file: /run/secrets/slack_webhook_url
        channel: "#oncall"
        send_resolved: true
  - name: pagerduty
    pagerduty_configs:
      - routing_key_file: /run/secrets/pagerduty_routing_key
```

**Production equivalents.**

| Concern | This repo | Production |
|---|---|---|
| Restart | Docker API via restart-only proxy | Kubernetes `rollout restart` with a namespaced RBAC role limited to `patch deployments` |
| Load-shedding | In-process flag on a private port | Ingress or service-mesh rate limits / priority shedding (Envoy, NGINX) |
| Breaker & cooldown state | In memory | Shared store (Redis) so that replicas agree and state survives restarts |
| Forensics | Last 50 lines in the snapshot + Loki | Also attach heap or thread dumps, and ship to an incident tool |
| Audit trail | Hash-chained JSONL + Loki | WORM storage (S3 Object Lock) or a SIEM |
| Log shipping | Promtail (feature-frozen upstream) | Grafana Alloy, using the same Loki labels |
| Intervals | 5 s scrape/eval for the demo | 15–30 s scrape, `for:` of 2–5 min |

## Repository layout

```text
app/                 FastAPI service: /, /work, /health, /metrics, /stress, /fail (:8000) + /shed-load (:8001)
webhook/             main.py (auth, runbook dispatch, audit), circuit_breaker.py, forensics.py + tests
monitoring/          Prometheus (+ promtool unit tests), Alertmanager, Loki, Promtail, Grafana provisioning
scripts/             init-env.sh, lib.sh, test-remediation.sh, test-load-shedding.sh, test-circuit-breaker.sh
.github/             CI (lint, promtool/amtool, pytest, Trivy) and Dependabot
```

## Versions

| Component | Version | Notes |
|---|---|---|
| Prometheus | `prom/prometheus:v3.16.0` | also provides `promtool` |
| Alertmanager | `prom/alertmanager:v0.35.0` | also provides `amtool` |
| Grafana | `grafana/grafana:13.3.1` | dashboards and datasources provisioned as code |
| Loki | `grafana/loki:3.6.22` | single binary, TSDB schema v13, 72 h retention |
| Promtail | `grafana/promtail:3.6.11` | Docker discovery through the GET-only proxy |
| node-exporter | `prom/node-exporter:v1.12.1` | no host mounts by default |
| Docker socket proxy | `tecnativa/docker-socket-proxy:v0.5.0` | restart-only + GET-only instances |
| Python base image | `python:3.13.18-slim-trixie` | app and webhook, non-root |
| FastAPI / Uvicorn | `0.165.0` / `0.54.0` | |
| prometheus-client / httpx | `0.26.0` / `0.28.1` | |
| CI tooling | hadolint `v2.15.1`, Trivy `0.76.2`, shellcheck, yamllint `1.38.0`, ruff `0.17.5`, pytest `9.2.0` | Actions pinned by commit SHA |

## License

[MIT](LICENSE)
