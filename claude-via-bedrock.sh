#!/bin/bash
# Point Claude Code at the LiteLLM gateway, Bedrock backend.
#   source ./claude-via-bedrock.sh
# Reads secrets from .env at runtime - nothing is hardcoded here.
set -a; . "$(dirname "${BASH_SOURCE[0]}")/.env"; set +a

export ANTHROPIC_BASE_URL="${LITELLM_URL:?set LITELLM_URL in .env}"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer ${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY in .env}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-bedrock-claude-sonnet-4-5}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-bedrock-claude-haiku-4-5}"
export ANTHROPIC_SMALL_FAST_MODEL="$ANTHROPIC_DEFAULT_HAIKU_MODEL"

# Critical: these must stay unset. Either one replaces Claude Code's own
# Authorization header, which the gateway may be forwarding upstream.
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_API_KEY

echo "Claude Code -> ${ANTHROPIC_BASE_URL} -> Bedrock (${AWS_REGION_NAME:-?})"
echo "model: ${ANTHROPIC_MODEL}   akto collection: ${ANTHROPIC_BASE_URL#https://}"
