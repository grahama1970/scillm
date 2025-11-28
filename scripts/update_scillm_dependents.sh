#!/usr/bin/env bash
# Sync downstream repos that pin scillm via file:///../litellm
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPENDENTS=(
  "extractor"
  "devops"
  "amd"
  "pi-mono"
)
if command -v uv >/dev/null 2>&1; then
  install_cmd=(uv pip install -e "$ROOT_DIR")
else
  install_cmd=(python3 -m pip install -e "$ROOT_DIR")
fi
for repo in "${DEPENDENTS[@]}"; do
  target_dir="$ROOT_DIR/../$repo"
  if [[ -d "$target_dir" ]]; then
    echo "[scillm-update] Updating $repo via ${install_cmd[*]}"
    (cd "$target_dir" && "${install_cmd[@]}")
  else
    echo "[scillm-update] Skipping $repo (not found at $target_dir)" >&2
  fi
done
