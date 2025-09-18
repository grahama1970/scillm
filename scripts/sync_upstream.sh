#!/usr/bin/env bash
set -euo pipefail

# Sync fork with upstream and rebase feature branches, gated by smokes.
# Usage:
#   scripts/sync_upstream.sh [--no-push]
#
# Env (optional):
#   BRANCHES="feat/mini-agent-api feat/parallel-acompletions-clean feat/router-lifecycle-clean feat/ollama-turbo-auth-clean pr/codex-agent"
#   PYTEST=pytest
#   PYTEST_ARGS="tests/smoke -q -r a"
#   REMOTE_UPSTREAM=upstream
#   REMOTE_ORIGIN=origin

REMOTE_UPSTREAM=${REMOTE_UPSTREAM:-upstream}
REMOTE_ORIGIN=${REMOTE_ORIGIN:-origin}
BRANCHES=${BRANCHES:-"feat/mini-agent-api feat/parallel-acompletions-clean feat/router-lifecycle-clean feat/ollama-turbo-auth-clean pr/codex-agent"}
PYTEST=${PYTEST:-pytest}
PYTEST_ARGS=${PYTEST_ARGS:-"tests/smoke -q -r a"}
DO_PUSH=1
if [[ "${1:-}" == "--no-push" ]]; then DO_PUSH=0; fi

echo "[sync] Enabling git rerere for conflict reuse"
git config rerere.enabled true

echo "[sync] Fetching upstream"
git fetch --prune ${REMOTE_UPSTREAM}

echo "[sync] Fast-forwarding main to ${REMOTE_UPSTREAM}/main"
git checkout main
git merge --ff-only ${REMOTE_UPSTREAM}/main
if [[ ${DO_PUSH} -eq 1 ]]; then git push ${REMOTE_ORIGIN} main; fi

run_smokes() {
  echo "[smokes] Running offline smokes: ${PYTEST} ${PYTEST_ARGS}"
  ${PYTEST} ${PYTEST_ARGS}
}

for BR in ${BRANCHES}; do
  echo "[sync] Rebase branch: ${BR} onto main"
  if ! git show-ref --verify --quiet refs/heads/${BR}; then
    echo "[sync] Skipping missing branch: ${BR}"; continue
  fi
  git checkout ${BR}
  # Stash uncommitted changes to avoid aborts
  STASHED=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    STASHED=1; git stash push -u -m sync_upstream_autostash
  fi
  set +e
  git rebase main
  REBASE_RC=$?
  set -e
  if [[ ${REBASE_RC} -ne 0 ]]; then
    echo "[sync] Rebase failed on ${BR}; aborting and restoring state"
    git rebase --abort || true
    if [[ ${STASHED} -eq 1 ]]; then git stash pop || true; fi
    git checkout main
    exit 2
  fi

  echo "[sync] Validating ${BR} with smokes"
  if ! run_smokes; then
    echo "[sync] Smokes failed on ${BR}; restoring state"
    if [[ ${STASHED} -eq 1 ]]; then git stash pop || true; fi
    git checkout main
    exit 3
  fi

  if [[ ${DO_PUSH} -eq 1 ]]; then
    echo "[sync] Pushing ${BR} with --force-with-lease"
    git push --force-with-lease ${REMOTE_ORIGIN} ${BR}
  else
    echo "[sync] Dry-run: not pushing ${BR}"
  fi
  if [[ ${STASHED} -eq 1 ]]; then git stash pop || true; fi
Done

git checkout main
echo "[sync] Done"
