#!/usr/bin/env bash
# Shared helpers for the end-to-end demo scripts. Source it; do not execute it.

APP_URL="${APP_URL:-http://127.0.0.1:8000}"
WEBHOOK_URL="${WEBHOOK_URL:-http://127.0.0.1:8080}"
PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
LOKI_URL="${LOKI_URL:-http://127.0.0.1:3100}"
COMPOSE="${COMPOSE:-docker compose}"

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; BOLD=""; RESET=""
fi

step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
pass() { printf '  %sPASS%s %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '  %s....%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %sFAIL%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

require_env() {
  [[ -f .env ]] || fail ".env not found: run 'gmake env' first"
  WEBHOOK_TOKEN="$(grep -E '^WEBHOOK_TOKEN=' .env | cut -d= -f2-)"
  [[ -n "$WEBHOOK_TOKEN" ]] || fail "WEBHOOK_TOKEN is empty in .env"
  for cmd in curl python3 docker; do
    command -v "$cmd" >/dev/null || fail "missing dependency: $cmd"
  done
}

# wait_for <description> <timeout-seconds> <command...>
wait_for() {
  local description="$1" timeout="$2"
  shift 2
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    ((SECONDS < deadline)) || fail "timed out after ${timeout}s waiting for: ${description}"
    sleep 3
  done
}

http_ok() { curl -fs -o /dev/null --max-time 3 "$1"; }

wait_stack_ready() {
  wait_for "app /health" 60 http_ok "${APP_URL}/health"
  wait_for "webhook /health" 60 http_ok "${WEBHOOK_URL}/health"
  wait_for "prometheus readiness" 60 http_ok "${PROM_URL}/-/ready"
  wait_for "loki readiness" 90 http_ok "${LOKI_URL}/ready"
}

webhook_get() { curl -fsS --max-time 5 -H "Authorization: Bearer ${WEBHOOK_TOKEN}" "${WEBHOOK_URL}$1"; }
webhook_post() { curl -fsS --max-time 10 -X POST -H "Authorization: Bearer ${WEBHOOK_TOKEN}" "${WEBHOOK_URL}$1"; }

# json_get <json> <python expression on `d`>
json_get() { python3 -c 'import json, sys; d = json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"; }

container_started_at() {
  docker inspect --format '{{.State.StartedAt}}' "$($COMPOSE ps -q app)"
}

# alert_state <alertname>: prints firing | pending | inactive for service="app".
alert_state() {
  curl -fsS --max-time 5 "${PROM_URL}/api/v1/alerts" | ALERT="$1" python3 -c '
import json, os, sys
alerts = json.load(sys.stdin)["data"]["alerts"]
states = [a["state"] for a in alerts if a["labels"].get("alertname") == os.environ["ALERT"] and a["labels"].get("service") == "app"]
print("firing" if "firing" in states else ("pending" if "pending" in states else "inactive"))'
}

alert_is() { [[ "$(alert_state "$1")" == "$2" ]]; }

# latest_audit <alertname> <action> <after-iso-or-epoch> [excluded-status]: newest matching audit entry as JSON.
latest_audit() {
  webhook_get "/audit?limit=200" | ALERT="$1" ACTION="$2" AFTER="$3" EXCLUDE="${4:-}" python3 -c '
import json, os, sys
from datetime import datetime
after = os.environ["AFTER"]
after_ts = float(after) if after.replace(".", "", 1).isdigit() else datetime.fromisoformat(after).timestamp()
entries = [
    e for e in json.load(sys.stdin)["entries"]
    if e.get("alertname") == os.environ["ALERT"] and e.get("action") == os.environ["ACTION"]
    and e.get("status") != os.environ["EXCLUDE"]
    and datetime.fromisoformat(e["timestamp"]).timestamp() > after_ts
]
if entries:
    print(json.dumps(entries[-1]))'
}

audit_exists() { [[ -n "$(latest_audit "$@")" ]]; }

# loki_count <logql> <start-epoch>
loki_count() {
  curl -fsS --max-time 5 -G "${LOKI_URL}/loki/api/v1/query_range" \
    --data-urlencode "query=$1" --data-urlencode "start=${2}000000000" --data-urlencode "limit=50" \
    | python3 -c 'import json, sys; print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))'
}

loki_has() { (($(loki_count "$1" "$2") > 0)); }

# prom_value <promql>: first sample value, or "none".
prom_value() {
  curl -fsS --max-time 5 -G "${PROM_URL}/api/v1/query" --data-urlencode "query=$1" \
    | python3 -c 'import json, sys; r = json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")'
}

prom_equals() { [[ "$(prom_value "$1")" == "$2" ]]; }
