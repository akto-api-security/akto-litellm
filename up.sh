#!/bin/bash
# Bring up the gateway and verify the Akto hook loaded.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "no .env - copy .env.example and fill it in" >&2; exit 1; }
[ -f custom_hooks.py ] || {
  echo "fetching the Akto connector hook..."
  curl -fsSL -o custom_hooks.py \
    https://raw.githubusercontent.com/akto-api-security/akto/master/apps/mcp-endpoint-shield/litellm/custom_hooks.py
}

docker compose up -d
until curl -sf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do sleep 2; done

echo
echo "gateway ready on http://localhost:4000"
docker compose logs litellm 2>&1 | grep -E "GuardrailsHandler initialized" | tail -1
echo "  sync mode  : $(grep -E '^SYNC_MODE=' .env | cut -d= -f2-)"
echo "  akto host  : $(grep -E '^LITELLM_URL=' .env | cut -d= -f2- | sed 's#^https\?://##')"
echo
echo "  source ./claude-via-bedrock.sh && claude"
