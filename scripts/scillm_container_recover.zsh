#!/usr/bin/env zsh
set -euo pipefail

# Generic recovery for scillm containers (codex-sidecar, certainly_bridge, codeworld-bridge)
# Usage: zsh scripts/scillm_container_recover.zsh [--execute]
# Env:
#   SCILLM_AGENT_WEBHOOK (optional) – notify with a compact JSON summary

DO_EXEC=0
[[ ${1:-} == "--execute" ]] && DO_EXEC=1

ts() { date -u +%Y-%m-%dT%H:%M:%SZ }

ok() { echo "[ok] $*" }
warn() { echo "[warn] $*" }

summaries=()

recover_codex() {
  local base=${CODEX_AGENT_API_BASE:-http://127.0.0.1:${CODEX_PORT:-8089}}
  base=${base%/}
  local h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
  local okflag=0
  grep -q 'HTTP 200' <<< "$h" && grep -q '"ok":true' <<< "$h" && okflag=1
  if [[ $okflag -eq 0 && $DO_EXEC -eq 1 && -f local/docker/compose.agents.yml ]]; then
    docker compose -f local/docker/compose.agents.yml up -d codex-sidecar >/dev/null 2>&1 || true
    sleep 2
    h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
    grep -q 'HTTP 200' <<< "$h" && grep -q '"ok":true' <<< "$h" && okflag=1
  fi
  summaries+="codex:${okflag}:${base}"
}

recover_certainly() {
  local base=${CERTAINLY_BRIDGE_BASE:-${LEAN4_BRIDGE_BASE:-http://127.0.0.1:8791}}
  base=${base%/}
  local h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
  local okflag=0
  grep -q 'HTTP 200' <<< "$h" && grep -q '"ok":true' <<< "$h" && okflag=1
  if [[ $okflag -eq 0 && $DO_EXEC -eq 1 && -f docker/compose.certainly.bridge.yml ]]; then
    COMPOSE_PROJECT_NAME=scillm-bridges docker compose -f docker/compose.certainly.bridge.yml up -d >/dev/null 2>&1 || true
    sleep 3
    h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
    grep -q 'HTTP 200' <<< "$h" && grep -q '"ok":true' <<< "$h" && okflag=1
  fi
  summaries+=" certainly:${okflag}:${base}"
}

recover_codeworld() {
  local base=${CODEWORLD_BASE:-http://127.0.0.1:8887}
  base=${base%/}
  local h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
  local okflag=0
  grep -q 'HTTP 200' <<< "$h" && okflag=1
  if [[ $okflag -eq 0 && $DO_EXEC -eq 1 && -f deploy/docker/compose.scillm.stack.yml ]]; then
    COMPOSE_PROJECT_NAME=scillm-bridges docker compose -f deploy/docker/compose.scillm.stack.yml up -d codeworld-bridge >/dev/null 2>&1 || true
    sleep 3
    h=$(curl -sS -m 5 -w '\nHTTP %{http_code}\n' "$base/healthz" || true)
    grep -q 'HTTP 200' <<< "$h" && okflag=1
  fi
  summaries+=" codeworld:${okflag}:${base}"
}

recover_codex
recover_certainly
recover_codeworld

echo "timestamp=$(ts)"
echo "summary=${summaries}"

if [[ -n ${SCILLM_AGENT_WEBHOOK:-} ]]; then
  jq -n --arg t "$(ts)" \
        --arg s "${summaries}" \
        '{component:"scillm-containers", timestamp:$t, summary:$s}' \
  | curl -sS -X POST -H 'Content-Type: application/json' --data @- "$SCILLM_AGENT_WEBHOOK" >/dev/null 2>&1 || true
fi

exit 0
