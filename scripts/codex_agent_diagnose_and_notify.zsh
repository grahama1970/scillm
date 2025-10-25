#!/usr/bin/env zsh
set -euo pipefail

# Diagnose + optionally recover codex-agent sidecar, then notify a webhook.
# Usage:
#   zsh scripts/codex_agent_diagnose_and_notify.zsh [--execute]
# Env:
#   SCILLM_AGENT_WEBHOOK (optional) – POST target for concise JSON result
#   CODEX_AGENT_API_BASE (default http://127.0.0.1:8089)
#   CODEX_PORT (default 8089) – used when starting compose

ts() { date -u +%Y-%m-%dT%H:%M:%SZ }

DO_EXEC=0
if [[ ${1:-} == "--execute" ]]; then
  DO_EXEC=1
fi

BASE=${CODEX_AGENT_API_BASE:-http://127.0.0.1:${CODEX_PORT:-8089}};
BASE=${BASE%/}

mkdir -p .artifacts/diagnostics
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DIAG=.artifacts/diagnostics/codex_agent_diag_${STAMP}.txt

{
  echo "timestamp=$(ts)"
  echo "base=${BASE}"
  echo "docker_ps=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -i codex || true)"
  echo "healthz:"
  curl -sS -m 6 -w '\nHTTP %{http_code}\n' "${BASE}/healthz" || true
  echo "models:"
  curl -sS -m 6 -w '\nHTTP %{http_code}\n' "${BASE}/v1/models" || true
} >| "$DIAG"

HEALTH_OK=0
if grep -q "HTTP 200" "$DIAG" && grep -q '"ok":true' "$DIAG"; then
  HEALTH_OK=1
fi

if [[ $HEALTH_OK -eq 0 && $DO_EXEC -eq 1 ]]; then
  # attempt recovery via compose
  if [[ -f local/docker/compose.agents.yml ]]; then
    CODEX_PORT=${CODEX_PORT:-8089}
    docker compose -f local/docker/compose.agents.yml up -d codex-sidecar >/dev/null 2>&1 || true
    sleep 2
    curl -sS -m 6 -w '\nHTTP %{http_code}\n' "${BASE}/healthz" >> "$DIAG" 2>/dev/null || true
    if grep -q 'HTTP 200' "$DIAG" && grep -q '"ok":true' "$DIAG"; then
      HEALTH_OK=1
    fi
  else
    echo "compose_missing=1" >> "$DIAG"
  fi
fi

# Optional webhook
if [[ -n ${SCILLM_AGENT_WEBHOOK:-} ]]; then
  payload=$(jq -n --arg t "$(ts)" \
                --arg base "$BASE" \
                --arg diag "$DIAG" \
                --argjson ok $HEALTH_OK \
                '{component:"codex-agent", timestamp:$t, base_url:$base, health_ok:$ok, diag_path:$diag}')
  curl -sS -X POST -H 'Content-Type: application/json' --data "$payload" "$SCILLM_AGENT_WEBHOOK" >/dev/null 2>&1 || true
fi

echo "diag_path=${DIAG}"
if [[ $HEALTH_OK -eq 1 ]]; then
  echo "status=ok"
  exit 0
else
  echo "status=fail"
  exit 1
fi
