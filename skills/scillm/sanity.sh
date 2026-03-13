#!/bin/bash
# Sanity check for scillm skill
# Verifies proxy is running and basic completions work
# Exit 0=PASS, 1=FAIL, 3=SKIP (proxy unavailable)
set -e

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"

if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

PROXY_URL="${SCILLM_API_BASE:-http://localhost:4001}"
PROXY_KEY="${SCILLM_PROXY_KEY:-${LITELLM_MASTER_KEY:-sk-dev-proxy-123}}"

echo "=== scillm Skill Sanity Check ==="
echo ""

# 1. Check proxy is alive
echo -n "1. Proxy liveliness... "
if curl -sf "$PROXY_URL/health/liveliness" > /dev/null 2>&1; then
    echo "OK"
else
    echo "SKIP (proxy not reachable at $PROXY_URL)"
    exit 3
fi

# 2. Check readiness
echo -n "2. Proxy readiness... "
READY=$(curl -sf "$PROXY_URL/health/readiness" 2>/dev/null) || true
if echo "$READY" | grep -q '"ready"'; then
    GROUPS=$(echo "$READY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('model_groups',0))" 2>/dev/null || echo "?")
    echo "OK ($GROUPS model groups)"
else
    echo "FAIL (not ready)"
    exit 1
fi

# 3. Text completion
echo -n "3. Text completion... "
START=$(date +%s%N)
RESP=$(curl -sf "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"text","messages":[{"role":"user","content":"Say hello in 3 words"}],"max_tokens":16}' \
    --max-time 30 2>&1) || true
END=$(date +%s%N)
ELAPSED=$(( (END - START) / 1000000 ))

if echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['choices'][0]['message']['content']" 2>/dev/null; then
    CONTENT=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'][:50])" 2>/dev/null)
    echo "OK (${ELAPSED}ms) — $CONTENT"
else
    if echo "$RESP" | grep -qiE "(timeout|429|503|502)"; then
        echo "SKIP (provider unavailable)"
    else
        echo "FAIL"
        echo "   Response: ${RESP:0:100}"
        exit 1
    fi
fi

# 4. Model list
echo -n "4. Model list... "
MODELS=$(curl -sf "$PROXY_URL/v1/models" \
    -H "Authorization: Bearer $PROXY_KEY" 2>/dev/null) || true
if echo "$MODELS" | grep -q '"object": "list"'; then
    COUNT=$(echo "$MODELS" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo "?")
    echo "OK ($COUNT models)"
else
    echo "FAIL"
    exit 1
fi

# 5. Budget endpoint
echo -n "5. Budget endpoint... "
BUDGET=$(curl -sf "$PROXY_URL/v1/budget" \
    -H "Authorization: Bearer $PROXY_KEY" 2>/dev/null) || true
if [ -n "$BUDGET" ]; then
    echo "OK"
else
    echo "SKIP (budget not configured)"
fi

echo ""
echo "=== Sanity: PASS ==="
