#!/usr/bin/env bash
# grep_guard_scillm.sh — Detect forbidden scillm import patterns in downstream projects.
# Usage:
#   bash scripts/grep_guard_scillm.sh /path/to/project [/path/to/project2 ...]
#   bash scripts/grep_guard_scillm.sh --self-test
#
# Exit codes: 0 = clean, 1 = violations found, 2 = usage error

set -euo pipefail

# Allowlisted paths (these ARE the proxy/core — they may call Chutes directly)
ALLOWLIST_GLOBS=(
  '!**/litellm/**'
  '!**/chutes/**'
  '!**/.archive/**'
  '!**/.venv/**'
  '!**/__pycache__/**'
  '!**/scillm/batch.py'
  '!**/scillm/paved/**'
  '!**/scillm/extras/**'
  '!**/scillm/__init__.py'
  '!**/chutes_error_hook.py'
  '!**/fetch_chutes_models.py'
  '!**/pricing_providers.py'
  '!**/ops-chutes/**'
  '!**/scripts/**'
  '!**/scenarios/**'
  '!**/local/**'
  '!**/*_test.py'
  '!**/test_*'
  '!**/tests/**'
  '!**/fixtures/**'
  '!**/sanity/**'
  '!**/debug/**'
  '!**/backup/**'
)

check_project() {
  local dir="$1"
  local violations=0

  echo "=== Scanning: $dir ==="

  local glob_args=()
  for g in "${ALLOWLIST_GLOBS[@]}"; do
    glob_args+=(--glob "$g")
  done

  # Pattern 1: Env-var access of CHUTES_API_BASE (the string "CHUTES_API_BASE" in quotes)
  local hits
  hits=$(rg -n --type py "${glob_args[@]}" '"CHUTES_API_BASE"' "$dir" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  VIOLATION: Direct CHUTES_API_BASE usage (use SCILLM_API_BASE instead):"
    echo "$hits" | sed 's/^/    /'
    violations=$((violations + $(echo "$hits" | wc -l)))
  fi

  # Pattern 2: Env-var access of CHUTES_API_KEY (the string "CHUTES_API_KEY" in quotes)
  hits=$(rg -n --type py "${glob_args[@]}" '"CHUTES_API_KEY"' "$dir" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  VIOLATION: Direct CHUTES_API_KEY usage (use SCILLM_PROXY_KEY instead):"
    echo "$hits" | sed 's/^/    /'
    violations=$((violations + $(echo "$hits" | wc -l)))
  fi

  # Pattern 3: Manual auth headers
  hits=$(rg -n --type py "${glob_args[@]}" 'extra_headers=.*Authorization' "$dir" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  VIOLATION: Manual Authorization headers:"
    echo "$hits" | sed 's/^/    /'
    violations=$((violations + $(echo "$hits" | wc -l)))
  fi

  # Pattern 4: Raw HTTP to chat/completions
  hits=$(rg -n --type py "${glob_args[@]}" 'requests\.(get|post)\(.*chat/completions' "$dir" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  VIOLATION: Raw HTTP calls to chat/completions:"
    echo "$hits" | sed 's/^/    /'
    violations=$((violations + $(echo "$hits" | wc -l)))
  fi

  # Pattern 5: Hardcoded Chutes URLs (bypass proxy)
  hits=$(rg -n --type py "${glob_args[@]}" 'https://(llm\.chutes\.ai|chutes\.graham\.ai)/v1' "$dir" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "  VIOLATION: Hardcoded Chutes URLs (use SCILLM_API_BASE via proxy):"
    echo "$hits" | sed 's/^/    /'
    violations=$((violations + $(echo "$hits" | wc -l)))
  fi

  if [[ $violations -eq 0 ]]; then
    echo "  OK: No violations found."
  else
    echo "  TOTAL: $violations violation(s) found."
  fi

  return $violations
}

self_test() {
  echo "=== Self-test ==="
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf $tmpdir" EXIT

  # Create a clean file (should pass)
  mkdir -p "$tmpdir/clean"
  cat > "$tmpdir/clean/good.py" << 'PYEOF'
from scillm.paved import chat_json
import os
result = chat_json("test")
PYEOF

  # Create a migrated file with retained variable name (should pass)
  cat > "$tmpdir/clean/migrated.py" << 'PYEOF'
import os
CHUTES_API_KEY = os.environ.get("SCILLM_PROXY_KEY", "sk-dev-proxy-123")
CHUTES_API_BASE = os.environ.get("SCILLM_API_BASE", "http://localhost:4001")
PYEOF

  # Create a bad file (should fail)
  mkdir -p "$tmpdir/bad"
  cat > "$tmpdir/bad/naughty.py" << 'PYEOF'
import os
from scillm import acompletion
r = acompletion(api_base=os.environ["CHUTES_API_BASE"], api_key=os.environ["CHUTES_API_KEY"])
PYEOF

  # Test clean dir
  if check_project "$tmpdir/clean" >/dev/null 2>&1; then
    echo "  PASS: Clean project passes"
  else
    echo "  FAIL: Clean project should pass"
    exit 1
  fi

  # Test bad dir
  if check_project "$tmpdir/bad" >/dev/null 2>&1; then
    echo "  FAIL: Bad project should fail"
    exit 1
  else
    echo "  PASS: Bad project detected violations"
  fi

  echo "=== Self-test PASSED ==="
}

# --- Main ---
if [[ $# -eq 0 ]]; then
  echo "Usage: $0 /path/to/project [/path/to/project2 ...]"
  echo "       $0 --self-test"
  exit 2
fi

if [[ "$1" == "--self-test" ]]; then
  self_test
  exit 0
fi

total_violations=0
for project in "$@"; do
  if [[ ! -d "$project" ]]; then
    echo "ERROR: $project is not a directory"
    exit 2
  fi
  if ! check_project "$project"; then
    total_violations=$((total_violations + 1))
  fi
done

if [[ $total_violations -gt 0 ]]; then
  echo ""
  echo "FAILED: $total_violations project(s) with violations."
  echo "See SCILLM_PAVED_PATH_CONTRACT.md for allowed surfaces."
  exit 1
fi

echo ""
echo "ALL CLEAN."
exit 0
