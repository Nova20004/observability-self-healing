#!/usr/bin/env bash
# Creates .env from .env.example with freshly generated secrets. Never overwrites an existing .env.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists, leaving it untouched."
  exit 0
fi

random_hex() {
  openssl rand -hex "$1" 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex($1))"
}

umask 077
sed \
  -e "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(random_hex 16)|" \
  -e "s|^WEBHOOK_TOKEN=.*|WEBHOOK_TOKEN=$(random_hex 32)|" \
  .env.example > .env

echo "Created .env with random GRAFANA_ADMIN_PASSWORD and WEBHOOK_TOKEN (mode 600)."
