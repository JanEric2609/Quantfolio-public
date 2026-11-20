#!/bin/bash
# QuantFolio Healthcheck Script
# Probes all critical components: database, Redis, local LLM, cloud LLM backends
# Exit code: 0 = all green, 1 = any critical component down
# Usage: ./healthcheck.sh [verbose]

set -euo pipefail

VERBOSE="${1:-}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Component status tracking
RESULTS_JSON="{\"timestamp\":\"$TIMESTAMP\",\"checks\":{}}"
ALL_OK=true

# Configuration
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-quantfolio}"
DB_USER="${DB_USER:-postgres}"
REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
LLM_LOCAL_URL="${LLM_LOCAL_URL:-http://10.0.0.22:8080}"
APP_URL="${APP_URL:-http://localhost:8000}"

# Helper function to check a component
check_component() {
    local name=$1
    local check_cmd=$2
    local required=${3:-true}  # true = critical, false = optional

    if [ -n "$VERBOSE" ]; then
        echo -n "Checking $name... "
    fi

    if eval "$check_cmd" &>/dev/null; then
        STATUS="ok"
        if [ -n "$VERBOSE" ]; then
            echo "✓"
        fi
    else
        STATUS="down"
        if [ -n "$VERBOSE" ]; then
            echo "✗"
        fi
        if [ "$required" = "true" ]; then
            ALL_OK=false
        fi
    fi

    # Add to JSON
    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq ".checks.\"$name\" = \"$STATUS\"")
}

# ====================================================================
# 1. Database
# ====================================================================

check_component "database" \
    "psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -c 'SELECT 1' >/dev/null 2>&1" \
    "true"

# ====================================================================
# 2. Redis (optional)
# ====================================================================

check_component "redis" \
    "redis-cli -u $REDIS_URL ping | grep -q PONG" \
    "false"

# ====================================================================
# 3. Local LLM (llama.cpp)
# ====================================================================

check_component "llm_local" \
    "curl -sf $LLM_LOCAL_URL/health >/dev/null 2>&1" \
    "true"

# ====================================================================
# 4. FastAPI App
# ====================================================================

check_component "app_api" \
    "curl -sf $APP_URL/health >/dev/null 2>&1" \
    "true"

# ====================================================================
# 5. Cloud Backends (Anthropic)
# ====================================================================

check_component "llm_anthropic" \
    "curl -sf https://api.anthropic.com -H 'User-Agent: quantfolio' -m 2 >/dev/null 2>&1" \
    "false"

# ====================================================================
# 6. Cloud Backends (OpenAI)
# ====================================================================

check_component "llm_openai" \
    "curl -sf https://api.openai.com -H 'User-Agent: quantfolio' -m 2 >/dev/null 2>&1" \
    "false"

# ====================================================================
# 7. Detailed Health (if available)
# ====================================================================

if curl -sf $APP_URL/healthz >/dev/null 2>&1; then
    HEALTH_DETAIL=$(curl -s $APP_URL/healthz | jq '.')
    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq ".app_healthz = $HEALTH_DETAIL")
fi

# ====================================================================
# 8. Metrics (llama.cpp)
# ====================================================================

# /metrics requires the API key when llama-server runs with LLAMA_API_KEY (export LLM_API_KEY).
LLM_AUTH=()
if [ -n "${LLM_API_KEY:-}" ]; then
    LLM_AUTH=(-H "Authorization: Bearer ${LLM_API_KEY}")
fi
if curl -sf "${LLM_AUTH[@]}" $LLM_LOCAL_URL/metrics >/dev/null 2>&1; then
    # Extract key metrics
    PROMPT_TOKENS=$(curl -s "${LLM_AUTH[@]}" $LLM_LOCAL_URL/metrics | grep '^ggml_prompt_tokens_total' | awk '{print $2}' || echo "0")
    COMPLETION_TOKENS=$(curl -s "${LLM_AUTH[@]}" $LLM_LOCAL_URL/metrics | grep '^ggml_completion_tokens_total' | awk '{print $2}' || echo "0")
    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq ".llm_metrics = {\"prompt_tokens\": $PROMPT_TOKENS, \"completion_tokens\": $COMPLETION_TOKENS}")
fi

# ====================================================================
# Output
# ====================================================================

if [ -n "$VERBOSE" ]; then
    echo ""
    echo "=========================================="
    echo "QuantFolio Health Check Results"
    echo "=========================================="
    echo "$RESULTS_JSON" | jq '.'
    echo "=========================================="
fi

# Output JSON for monitoring systems
echo "$RESULTS_JSON" | jq '.'

# Exit code
if [ "$ALL_OK" = "true" ]; then
    [ -n "$VERBOSE" ] && echo "✓ All critical components OK"
    exit 0
else
    [ -n "$VERBOSE" ] && echo "✗ One or more critical components down"
    exit 1
fi
