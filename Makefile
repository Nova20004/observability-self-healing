SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
PYTHON ?= python3
VENV := .venv
VENV_BIN := $(VENV)/bin

# Keep in sync with docker-compose.yml and .github/workflows/ci.yml.
PROMETHEUS_IMAGE := prom/prometheus:v3.16.0
ALERTMANAGER_IMAGE := prom/alertmanager:v0.35.0
HADOLINT_IMAGE := hadolint/hadolint:v2.15.1
SHELLCHECK_IMAGE := koalaman/shellcheck:v0.11.0

.PHONY: help env up down clean ps logs demo demo-circuit-breaker demo-load-shedding test-remediation \
        incidents circuit-breaker circuit-breaker-reset status validate validate-compose validate-prometheus \
        validate-alertmanager lint-yaml lint-python lint-docker lint-shell test

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

env: .env ## Create .env with random secrets (never overwrites)

.env:
	@./scripts/init-env.sh

up: .env ## Build and start the stack, wait until healthy
	$(COMPOSE) up -d --build --wait --wait-timeout 180
	@$(MAKE) --no-print-directory status

down: ## Stop the stack and remove containers (keeps volumes)
	$(COMPOSE) down --remove-orphans

clean: ## Stop the stack and delete all volumes (metrics, logs, audit trail)
	$(COMPOSE) down --remove-orphans --volumes

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Follow logs of all services
	$(COMPOSE) logs -f --tail=100

test-remediation: ## Prove the restart runbook (pre-mortem, restart, post-heal check) on a running stack
	./scripts/test-remediation.sh

demo-load-shedding: ## Prove the latency runbook (graceful load-shedding, no restart) on a running stack
	./scripts/test-load-shedding.sh

demo-circuit-breaker: ## Prove the anti-flap circuit breaker escalates and can be re-armed (~3 min)
	./scripts/test-circuit-breaker.sh

demo: up test-remediation demo-load-shedding demo-circuit-breaker status ## Start the stack and prove every runbook end-to-end

incidents: ## Show the last pre-mortem snapshots (GET /incidents)
	@curl -fsS -H "Authorization: Bearer $$(grep -E '^WEBHOOK_TOKEN=' .env | cut -d= -f2-)" \
	  "http://127.0.0.1:8080/incidents?limit=$${LIMIT:-5}" | $(PYTHON) -m json.tool

circuit-breaker: ## Show circuit breaker state per service
	@curl -fsS -H "Authorization: Bearer $$(grep -E '^WEBHOOK_TOKEN=' .env | cut -d= -f2-)" \
	  http://127.0.0.1:8080/circuit-breaker | $(PYTHON) -m json.tool

circuit-breaker-reset: ## Re-arm the circuit breaker (SERVICE=app, empty = all)
	@curl -fsS -X POST -H "Authorization: Bearer $$(grep -E '^WEBHOOK_TOKEN=' .env | cut -d= -f2-)" \
	  "http://127.0.0.1:8080/circuit-breaker/reset$${SERVICE:+?service=$$SERVICE}" | $(PYTHON) -m json.tool

status: ## Print endpoints and current alert state
	@echo ""
	@echo "  Grafana       http://127.0.0.1:3000  (user: $$(grep -E '^GRAFANA_ADMIN_USER=' .env | cut -d= -f2-), password in .env)"
	@echo "  Prometheus    http://127.0.0.1:9090/alerts"
	@echo "  Alertmanager  http://127.0.0.1:9093"
	@echo "  App           http://127.0.0.1:8000/metrics"
	@echo "  Webhook       http://127.0.0.1:8080/health  (gmake incidents | circuit-breaker)"
	@echo ""
	@curl -fsS --max-time 3 http://127.0.0.1:9090/api/v1/alerts 2>/dev/null \
	  | $(PYTHON) -c 'import json,sys; a=json.load(sys.stdin)["data"]["alerts"]; print("  Active alerts: " + (", ".join(x["labels"]["alertname"] + "(" + x["state"] + ")" for x in a) or "none"))' \
	  || echo "  Prometheus not reachable"

# ------------------------------------------------------------------ validation

validate: validate-compose validate-prometheus validate-alertmanager lint-yaml lint-python lint-docker lint-shell test ## Run every static check and unit test

$(VENV_BIN)/activate: requirements-dev.txt app/requirements.txt webhook/requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/pip install --quiet --disable-pip-version-check -r requirements-dev.txt
	@touch $@

validate-compose: ## Validate docker-compose.yml (uses .env.example values if .env is absent)
	@if [[ -f .env ]]; then $(COMPOSE) config --quiet; \
	else $(COMPOSE) --env-file .env.example config --quiet; fi
	@echo "compose: OK"

validate-prometheus: ## promtool: check config, check rules, run rule unit tests
	docker run --rm --entrypoint promtool -v "$(CURDIR)/monitoring/prometheus:/etc/prometheus:ro" \
	  $(PROMETHEUS_IMAGE) check config /etc/prometheus/prometheus.yml
	docker run --rm --entrypoint promtool -v "$(CURDIR)/monitoring/prometheus:/etc/prometheus:ro" \
	  $(PROMETHEUS_IMAGE) check rules /etc/prometheus/alerts.yml
	docker run --rm --entrypoint promtool -v "$(CURDIR)/monitoring/prometheus:/etc/prometheus:ro" \
	  -w /etc/prometheus/tests $(PROMETHEUS_IMAGE) test rules alerts.test.yml

validate-alertmanager: ## amtool check-config
	docker run --rm --entrypoint amtool -v "$(CURDIR)/monitoring/alertmanager:/etc/alertmanager:ro" \
	  $(ALERTMANAGER_IMAGE) check-config /etc/alertmanager/alertmanager.yml

lint-yaml: $(VENV_BIN)/activate ## yamllint all YAML files
	$(VENV_BIN)/yamllint --strict .

lint-python: $(VENV_BIN)/activate ## ruff lint + format check
	$(VENV_BIN)/ruff check app webhook
	$(VENV_BIN)/ruff format --check app webhook

lint-docker: ## hadolint both Dockerfiles
	docker run --rm -i $(HADOLINT_IMAGE) < app/Dockerfile
	docker run --rm -i $(HADOLINT_IMAGE) < webhook/Dockerfile
	@echo "hadolint: OK"

lint-shell: ## shellcheck scripts (follows `source scripts/lib.sh`)
	docker run --rm -v "$(CURDIR):/mnt:ro" -w /mnt $(SHELLCHECK_IMAGE) -x scripts/*.sh
	@echo "shellcheck: OK"

test: $(VENV_BIN)/activate ## Run unit tests for app and webhook
	cd app && ../$(VENV_BIN)/python -m pytest -q
	cd webhook && ../$(VENV_BIN)/python -m pytest -q
