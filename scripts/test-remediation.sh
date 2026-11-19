#!/usr/bin/env bash
# End-to-end proof of the restart runbook:
#   CPU stress -> Prometheus alert -> Alertmanager -> authenticated webhook -> pre-mortem snapshot
#   -> container restart -> post-heal /health verification -> hash-chained audit -> Loki -> alert resolved.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

STRESS_SECONDS="${STRESS_SECONDS:-180}"
ALERT_TIMEOUT="${ALERT_TIMEOUT:-120}"
REMEDIATION_TIMEOUT="${REMEDIATION_TIMEOUT:-90}"

cleanup() { curl -fsS -X DELETE "${APP_URL}/stress" >/dev/null 2>&1 || true; }
trap cleanup EXIT
require_env

step "1/8 Checking that the stack is healthy"
wait_stack_ready
pass "app, webhook, prometheus and loki are ready"

step "2/8 Verifying webhook authentication and re-arming the circuit breaker"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"status":"firing","alerts":[]}' "${WEBHOOK_URL}/alerts")"
[[ "$code" == "401" ]] || fail "unauthenticated request returned HTTP ${code}, expected 401"
pass "unauthenticated request rejected with HTTP 401"
webhook_post "/circuit-breaker/reset?service=app" >/dev/null
pass "circuit breaker for 'app' is CLOSED"

step "3/8 Generating baseline traffic and a sample error log"
for _ in $(seq 1 20); do curl -fsS -o /dev/null "${APP_URL}/"; done
curl -s -o /dev/null "${APP_URL}/fail"
pass "20 requests + 1 simulated failure sent"

TEST_START_EPOCH="$(date +%s)"
STARTED_AT_BEFORE="$(container_started_at)"
info "app container started at ${STARTED_AT_BEFORE}"

step "4/8 Triggering controlled CPU stress (${STRESS_SECONDS}s, capped at 1 core)"
curl -fsS -X POST "${APP_URL}/stress?seconds=${STRESS_SECONDS}" >/dev/null || fail "could not start stress (already running?)"
pass "stress started"

step "5/8 Waiting for HighCPUUsage to fire (timeout ${ALERT_TIMEOUT}s)"
wait_for "HighCPUUsage firing" "$ALERT_TIMEOUT" alert_is HighCPUUsage firing
pass "HighCPUUsage is FIRING after $(($(date +%s) - TEST_START_EPOCH))s"

step "6/8 Waiting for the restart runbook (timeout ${REMEDIATION_TIMEOUT}s)"
wait_for "restart decision" "$REMEDIATION_TIMEOUT" audit_exists HighCPUUsage restart "$TEST_START_EPOCH"
entry="$(latest_audit HighCPUUsage restart "$TEST_START_EPOCH")"
status="$(json_get "$entry" 'd["status"]')"
case "$status" in
  success) pass "webhook restarted the container: ${entry}" ;;
  skipped_cooldown) fail "remediation suppressed by cooldown (a previous run is too recent); retry later: ${entry}" ;;
  *) fail "unexpected remediation outcome '${status}': ${entry}" ;;
esac
[[ "$(json_get "$entry" 'd.get("post_heal_verified")')" == "True" ]] || fail "post-heal /health verification missing"
pass "post-heal verification: /health returned 200 after $(json_get "$entry" 'd["post_heal_seconds"]')s"

STARTED_AT_AFTER="$(container_started_at)"
[[ "$STARTED_AT_AFTER" != "$STARTED_AT_BEFORE" ]] || fail "container StartedAt did not change"
pass "container restarted (StartedAt ${STARTED_AT_BEFORE} -> ${STARTED_AT_AFTER})"
[[ "$(json_get "$(curl -fsS "${APP_URL}/health")" 'd["stress_active"]')" == "False" ]] || fail "stress still active"
pass "CPU stress cleared by the restart"

step "7/8 Verifying the pre-mortem snapshot"
incident_id="$(json_get "$entry" 'd["incident_id"]')"
incident="$(webhook_get "/incidents" | INCIDENT="$incident_id" python3 -c '
import json, os, sys
print(json.dumps(next((i for i in json.load(sys.stdin)["incidents"] if i["incident_id"] == os.environ["INCIDENT"]), {})))')"
[[ "$(json_get "$incident" 'd.get("reason")')" == "pre-restart" ]] || fail "snapshot ${incident_id} not found in GET /incidents"
lines="$(json_get "$incident" 'd["log_lines"]')"
((lines > 0)) || fail "snapshot has no log lines: $(json_get "$incident" 'd["logs_error"]')"
pass "snapshot ${incident_id}: ${lines} log lines, container state=$(json_get "$incident" 'd["container_state"]["status"]') captured BEFORE restart"
info "last captured line: $(json_get "$incident" 'd["logs"][-1][:140]')"
wait_for "premortem_snapshot in Loki" 60 loki_has '{service="webhook", event="premortem_snapshot"}' "$TEST_START_EPOCH"
pass "snapshot ingested by Loki ({service=\"webhook\", event=\"premortem_snapshot\"})"

step "8/8 Verifying the audit trail and recovery"
verify="$(webhook_get "/audit/verify")"
[[ "$(json_get "$verify" 'd["valid"]')" == "True" ]] || fail "audit hash chain is invalid: ${verify}"
pass "audit hash chain intact: ${verify}"
wait_for "audit entry in Loki" 60 loki_has \
  '{service="webhook", event="remediation_audit"} | json | alertname="HighCPUUsage" | status="success"' "$TEST_START_EPOCH"
pass "audit entry ingested by Loki"
wait_for "success counter scraped by Prometheus" 30 prom_equals \
  'sum(remediation_outcomes_total{service="app",outcome="success"}) >= bool 1' 1
pass "remediation_outcomes_total{service=\"app\",outcome=\"success\"} = $(prom_value 'sum(remediation_outcomes_total{service="app",outcome="success"})')"
wait_for "HighCPUUsage resolved" 120 alert_is HighCPUUsage inactive
pass "HighCPUUsage resolved — service self-healed in $(($(date +%s) - TEST_START_EPOCH))s end-to-end"

printf '\n%sRESTART RUNBOOK VERIFIED%s\n' "$GREEN$BOLD" "$RESET"
