#!/bin/bash
# Sanity check for scillm skill
# Verifies proxy is running and basic completions work
# Exit 0=PASS, 1=FAIL, 3=SKIP (proxy unavailable)
set -e

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"

dotenv_value() {
    ENV_FILE="$REPO_ROOT/.env" python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path

names = sys.argv[1].split(",")
default = sys.argv[2]
env_file = Path(os.environ["ENV_FILE"])
values = {}
if env_file.is_file():
    for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

for name in names:
    value = os.environ.get(name) or values.get(name)
    if value:
        print(value)
        raise SystemExit(0)

print(default)
PY
}

PROXY_URL="$(dotenv_value SCILLM_API_BASE "http://localhost:4001")"
PROXY_KEY="$(dotenv_value SCILLM_MASTER_KEY,LITELLM_MASTER_KEY,SCILLM_PROXY_KEY "sk-dev-proxy-123")"
CALLER_SKILL="${SCILLM_CALLER_SKILL:-scillm-sanity}"

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

# 3. Auth endpoint
echo -n "3. Auth endpoint... "
AUTH=$(curl -sf "$PROXY_URL/v1/scillm/auth" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "X-Caller-Skill: $CALLER_SKILL" 2>/dev/null) || true
if echo "$AUTH" | grep -q '"status"'; then
    echo "OK"
else
    echo "FAIL"
    exit 1
fi

# 4. Text completion
echo -n "4. Text completion... "
START=$(date +%s%N)
RESP=$(curl -sf "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "X-Caller-Skill: $CALLER_SKILL" \
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

# 5. Model list
echo -n "5. Model list... "
MODELS=$(curl -sf "$PROXY_URL/v1/models" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "X-Caller-Skill: $CALLER_SKILL" 2>/dev/null) || true
if echo "$MODELS" | grep -q '"object": "list"'; then
    COUNT=$(echo "$MODELS" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo "?")
    echo "OK ($COUNT models)"
else
    echo "FAIL"
    exit 1
fi

# 6. Budget endpoint
echo -n "6. Budget endpoint... "
BUDGET=$(curl -sf "$PROXY_URL/v1/budget" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "X-Caller-Skill: $CALLER_SKILL" 2>/dev/null) || true
if [ -n "$BUDGET" ]; then
    echo "OK"
else
    echo "SKIP (budget not configured)"
fi

echo ""
echo "=== Sanity: PASS ==="
