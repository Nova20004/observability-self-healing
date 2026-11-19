#!/usr/bin/env bash
# End-to-end proof of the anti-flap circuit breaker:
#   a fault that keeps coming back -> restart #1, restart #2 -> 3rd attempt refused -> ESCALATED
#   (gauge = 1, critical log, pre-mortem snapshot, RemediationCircuitBreakerOpen alert) -> operator reset.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh

ITERATION_TIMEOUT="${ITERATION_TIMEOUT:-150}"

cleanup() { curl -fsS -X DELETE "${APP_URL}/stress" >/dev/null 2>&1 || true; }
trap cleanup EXIT
require_env

step "1/5 Re-arming the breaker"
wait_stack_ready
breaker="$(webhook_post "/circuit-breaker/reset?service=app")"
max="$(json_get "$breaker" 'd["services"]["app"]["max_actions"]')"
window="$(json_get "$breaker" 'd["services"]["app"]["window_seconds"]')"
pass "breaker CLOSED: budget ${max} restarts per ${window}s"
TEST_START_EPOCH="$(date +%s)"

step "2/5 Simulating a recurring fault until the breaker trips"
last_seen="$TEST_START_EPOCH"
outcomes=()
for attempt in $(seq 1 $((max + 2))); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${APP_URL}/stress?seconds=240")"
  [[ "$code" == "202" || "$code" == "409" ]] || fail "could not start stress (HTTP ${code})"
  info "attempt ${attempt}: CPU fault active, waiting for the webhook decision"
  wait_for "restart decision #${attempt}" "$ITERATION_TIMEOUT" \
    audit_exists HighCPUUsage restart "$last_seen" skipped_cooldown
  entry="$(latest_audit HighCPUUsage restart "$last_seen" skipped_cooldown)"
  status="$(json_get "$entry" 'd["status"]')"
  last_seen="$(json_get "$entry" 'd["timestamp"]')"
  outcomes+=("$status")
  pass "attempt ${attempt}: ${status} ($(json_get "$entry" 'd["detail"]'))"
  if [[ "$status" == "escalated" ]]; then
    break
  fi
  [[ "$status" == "success" ]] || fail "unexpected outcome: ${entry}"
  wait_for "app healthy" 60 http_ok "${APP_URL}/health"
  STARTED_AT_LAST_RESTART="$(container_started_at)"
done
expected="$(printf 'success %.0s' $(seq 1 "$max"))escalated"
[[ "${outcomes[*]}" == "$expected" ]] || fail "expected '${expected}', got '${outcomes[*]}'"
pass "breaker tripped after exactly ${max} restarts: ${outcomes[*]}"
sleep 5
[[ "$(container_started_at)" == "$STARTED_AT_LAST_RESTART" ]] || fail "container restarted despite the open breaker"
pass "no restart after escalation (no crash loop)"

step "3/5 Verifying escalation signals"
[[ "$(json_get "$(webhook_get /circuit-breaker)" 'd["services"]["app"]["state"]')" == "escalated" ]] \
  || fail "breaker state is not ESCALATED"
pass "GET /circuit-breaker: app = ESCALATED"
wait_for "breaker gauge in Prometheus" 30 prom_equals 'remediation_circuit_breaker_open{service="app"}' 1
pass "remediation_circuit_breaker_open{service=\"app\"} = 1"
pass "remediation_outcomes_total{service=\"app\",outcome=\"escalated\"} = $(prom_value 'remediation_outcomes_total{service="app",outcome="escalated"}')"
incident="$(webhook_get "/incidents" | python3 -c '
import json, sys
print(json.dumps([i for i in json.load(sys.stdin)["incidents"] if i["reason"] == "circuit-breaker-escalation"][-1]))')"
pass "escalation snapshot $(json_get "$incident" 'd["incident_id"]') with $(json_get "$incident" 'd["log_lines"]') log lines for on-call"
wait_for "circuit_breaker_escalated in Loki" 60 loki_has \
  '{service="webhook", event="circuit_breaker_escalated", level="CRITICAL"}' "$TEST_START_EPOCH"
pass "CRITICAL log event=\"circuit_breaker_escalated\" ingested by Loki"
wait_for "RemediationCircuitBreakerOpen firing" 60 alert_is RemediationCircuitBreakerOpen firing
pass "Prometheus alert RemediationCircuitBreakerOpen is FIRING (page on-call)"

step "4/5 Operator fixes the root cause and re-arms the breaker"
curl -fsS -X DELETE "${APP_URL}/stress" >/dev/null
pass "root cause removed (stress stopped)"
reset="$(webhook_post "/circuit-breaker/reset?service=app")"
[[ "$(json_get "$reset" 'd["services"]["app"]["state"]')" == "closed" ]] || fail "reset failed: ${reset}"
pass "POST /circuit-breaker/reset -> app = CLOSED"
wait_for "breaker gauge back to 0" 30 prom_equals 'remediation_circuit_breaker_open{service="app"}' 0
pass "remediation_circuit_breaker_open{service=\"app\"} = 0"

step "5/5 Waiting for alerts to clear"
wait_for "HighCPUUsage resolved" 120 alert_is HighCPUUsage inactive
wait_for "RemediationCircuitBreakerOpen resolved" 60 alert_is RemediationCircuitBreakerOpen inactive
verify="$(webhook_get "/audit/verify")"
[[ "$(json_get "$verify" 'd["valid"]')" == "True" ]] || fail "audit hash chain is invalid: ${verify}"
pass "alerts cleared, audit chain intact: ${verify}"

printf '\n%sCIRCUIT BREAKER VERIFIED%s\n' "$GREEN$BOLD" "$RESET"
