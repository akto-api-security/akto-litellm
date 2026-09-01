#!/bin/bash
# Same gateway, authenticated with a virtual key that carries key_alias +
# metadata, so this agent lands in its own Akto collection with its own tags.
#   AGENT_KEY=sk-... source ./claude-as-agent.sh
# Create a key first - see "Passing user metadata" in the README.
set -a; . "$(dirname "${BASH_SOURCE[0]}")/.env"; set +a

: "${AGENT_KEY:?export AGENT_KEY=<virtual key from POST /key/generate>}"

export ANTHROPIC_BASE_URL="${LITELLM_URL:?set LITELLM_URL in .env}"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer ${AGENT_KEY}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-bedrock-claude-sonnet-4-5}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-bedrock-claude-haiku-4-5}"
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_API_KEY

echo "Claude Code -> ${ANTHROPIC_BASE_URL} (virtual key)"
