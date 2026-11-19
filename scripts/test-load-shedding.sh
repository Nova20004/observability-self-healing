#!/usr/bin/env bash
# End-to-end proof of the graceful load-shedding runbook:
#   slow traffic -> HighLatencySpike -> webhook enables shedding (no restart) -> 503 + Retry-After
#   -> latency recovers -> alert resolves -> webhook restores traffic.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

DELAY_MS="${DELAY_MS:-400}"
WORKERS="${WORKERS:-6}"
WORKER_PIDS=()

stop_traffic() {
  for pid in "${WORKER_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  WORKER_PIDS=()
}
trap stop_traffic EXIT
require_env

step "1/6 Checking that the stack is healthy"
wait_stack_ready
[[ "$(json_get "$(curl -fsS "${APP_URL}/health")" 'd["load_shedding"]')" == "False" ]] || fail "load-shedding already active"
pass "stack ready, load-shedding inactive"

TEST_START_EPOCH="$(date +%s)"
STARTED_AT_BEFORE="$(container_started_at)"

step "2/6 Sending slow traffic: ${WORKERS} workers on /work?delay_ms=${DELAY_MS} (async sleep, zero CPU)"
for _ in $(seq 1 "$WORKERS"); do
  while true; do
    curl -s -o /dev/null --max-time 3 "${APP_URL}/work?delay_ms=${DELAY_MS}" || true
    sleep 0.2
  done &
  WORKER_PIDS+=("$!")
done
pass "slow traffic running"

step "3/6 Waiting for HighLatencySpike to fire (timeout 120s)"
wait_for "HighLatencySpike firing" 120 alert_is HighLatencySpike firing
pass "HighLatencySpike is FIRING after $(($(date +%s) - TEST_START_EPOCH))s (p95 > 250ms)"

step "4/6 Waiting for the load-shedding runbook (timeout 60s)"
wait_for "shed_load decision" 60 audit_exists HighLatencySpike shed_load "$TEST_START_EPOCH"
entry="$(latest_audit HighLatencySpike shed_load "$TEST_START_EPOCH")"
[[ "$(json_get "$entry" 'd["status"]')" == "success" ]] || fail "unexpected shed_load outcome: ${entry}"
pass "webhook enabled load-shedding: $(json_get "$entry" 'd["detail"]')"

headers="$(curl -s -o /dev/null -D - "${APP_URL}/work")"
grep -q '^HTTP/1.1 503' <<<"$headers" || fail "non-critical traffic is not shed: ${headers}"
grep -qi '^retry-after: ' <<<"$headers" || fail "503 without Retry-After header"
pass "non-critical traffic shed: HTTP 503 + $(grep -i '^retry-after' <<<"$headers" | tr -d '\r')"
http_ok "${APP_URL}/health" || fail "/health must stay responsive while shedding"
http_ok "${APP_URL}/metrics" || fail "/metrics must stay responsive while shedding"
pass "/health and /metrics stay responsive"
curl -fsS "${APP_URL}/metrics" | grep -q '^app_load_shedding_active 1.0' || fail "app_load_shedding_active != 1"
pass "app_load_shedding_active = 1"
[[ "$(container_started_at)" == "$STARTED_AT_BEFORE" ]] || fail "container was restarted by the latency runbook"
pass "no restart: active connections preserved (StartedAt unchanged)"

step "5/6 Stopping slow traffic and waiting for recovery (timeout 120s)"
stop_traffic
wait_for "HighLatencySpike resolved" 120 alert_is HighLatencySpike inactive
pass "HighLatencySpike resolved"
wait_for "restore_traffic decision" 60 audit_exists HighLatencySpike restore_traffic "$TEST_START_EPOCH"
restore="$(latest_audit HighLatencySpike restore_traffic "$TEST_START_EPOCH")"
[[ "$(json_get "$restore" 'd["status"]')" == "success" ]] || fail "unexpected restore outcome: ${restore}"
pass "webhook restored traffic: $(json_get "$restore" 'd["detail"]')"

step "6/6 Verifying the service is fully back"
[[ "$(curl -s -o /dev/null -w '%{http_code}' "${APP_URL}/work")" == "200" ]] || fail "/work still rejected"
[[ "$(json_get "$(curl -fsS "${APP_URL}/health")" 'd["load_shedding"]')" == "False" ]] || fail "load-shedding still active"
pass "/work answers 200 and load-shedding is off — degraded and recovered in $(($(date +%s) - TEST_START_EPOCH))s"

printf '\n%sLOAD-SHEDDING RUNBOOK VERIFIED%s\n' "$GREEN$BOLD" "$RESET"
