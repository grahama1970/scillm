#!/usr/bin/env zsh
set -euo pipefail

# One-shot wrapper: optionally launch mini-agent, run review + compare.

: ${DRY_RUN:=1}
: ${CODEX_AGENT_HOST:=127.0.0.1}
: ${CODEX_AGENT_PORT:=8788}
export CODEX_AGENT_API_BASE="http://${CODEX_AGENT_HOST}:${CODEX_AGENT_PORT}"

cmd_start_agent=(uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host ${CODEX_AGENT_HOST} --port ${CODEX_AGENT_PORT})

echo "[review-e2e] Using base: ${CODEX_AGENT_API_BASE} (DRY_RUN=${DRY_RUN})"

if ! curl -sf "${CODEX_AGENT_API_BASE}/healthz" >/dev/null; then
  echo "[review-e2e] mini-agent not responding; will start: ${cmd_start_agent[@]}"
  if [[ "$DRY_RUN" == "0" ]]; then
    nohup ${cmd_start_agent[@]} >/tmp/mini_agent.log 2>&1 &
    echo "[review-e2e] Launched mini-agent (pid $!)"
    # Wait briefly for readiness
    for i in {1..20}; do
      if curl -sf "${CODEX_AGENT_API_BASE}/healthz" >/dev/null; then
        break
      fi
      sleep 0.5
    done
  fi
else
  echo "[review-e2e] mini-agent already healthy."
fi

echo "[review-e2e] List models: ${CODEX_AGENT_API_BASE}/v1/models"
[[ "$DRY_RUN" == "0" ]] && curl -s "${CODEX_AGENT_API_BASE}/v1/models" | jq . || true

echo "[review-e2e] Run review script"
if [[ "$DRY_RUN" == "0" ]]; then
  python3 scripts/review/run_scillm_review.py
  python3 scripts/review/compare_reviews.py
else
  echo python3 scripts/review/run_scillm_review.py
  echo python3 scripts/review/compare_reviews.py
fi

echo "[review-e2e] Done. Outputs in docs/review_competition/"

