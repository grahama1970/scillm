#!/bin/bash
# Sync bundled services from their source repositories.
# Run this before releasing scillm to ensure services are up-to-date.
#
# Usage:
#   ./scripts/sync-services.sh           # Sync all services
#   ./scripts/sync-services.sh --check   # Check if sync needed (for CI)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCILLM_ROOT="$(dirname "$SCRIPT_DIR")"

# Source locations (adjust if your paths differ)
MEMORY_SRC="${MEMORY_SRC:-/home/graham/workspace/experiments/memory}"
EMBEDDING_SRC="${EMBEDDING_SRC:-$MEMORY_SRC/docker/embedding}"

# Target locations
MEMORY_DST="$SCILLM_ROOT/services/memory"
EMBEDDING_DST="$SCILLM_ROOT/services/embedding"

check_mode=false
if [[ "${1:-}" == "--check" ]]; then
    check_mode=true
fi

sync_memory() {
    echo "Syncing memory service..."

    if [[ ! -d "$MEMORY_SRC" ]]; then
        echo "ERROR: Memory source not found at $MEMORY_SRC"
        echo "Set MEMORY_SRC env var to override."
        exit 1
    fi

    local src_sha
    src_sha=$(git -C "$MEMORY_SRC" rev-parse HEAD)
    local src_date
    src_date=$(git -C "$MEMORY_SRC" log -1 --format=%ci HEAD)

    if $check_mode; then
        if [[ -f "$MEMORY_DST/VERSION" ]]; then
            local dst_sha
            dst_sha=$(head -1 "$MEMORY_DST/VERSION")
            if [[ "$src_sha" == "$dst_sha" ]]; then
                echo "Memory service is up-to-date ($src_sha)"
                return 0
            else
                echo "Memory service needs sync:"
                echo "  Source: $src_sha ($src_date)"
                echo "  Bundled: $dst_sha"
                return 1
            fi
        else
            echo "Memory service VERSION file missing — sync required"
            return 1
        fi
    fi

    # Clean and copy
    rm -rf "$MEMORY_DST/graph_memory"
    cp -r "$MEMORY_SRC/src/graph_memory" "$MEMORY_DST/"
    cp "$MEMORY_SRC/pyproject.toml" "$MEMORY_DST/"
    cp "$MEMORY_SRC/uv.lock" "$MEMORY_DST/"

    # Remove __pycache__ directories
    find "$MEMORY_DST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    # Update Dockerfile paths (src/ -> .)
    sed -i 's|COPY src/|COPY |g' "$MEMORY_DST/Dockerfile" 2>/dev/null || true
    sed -i 's|where = \["src"\]|where = ["."]|g' "$MEMORY_DST/pyproject.toml" 2>/dev/null || true

    # Write version marker
    cat > "$MEMORY_DST/VERSION" <<EOF
$src_sha
$src_date
Synced from: $MEMORY_SRC
EOF

    echo "Memory service synced: $src_sha"
}

sync_embedding() {
    echo "Syncing embedding service..."

    if [[ ! -d "$EMBEDDING_SRC" ]]; then
        echo "WARNING: Embedding source not found at $EMBEDDING_SRC"
        echo "Skipping embedding sync."
        return 0
    fi

    if $check_mode; then
        # No version tracking for embedding yet
        echo "Embedding service check not implemented (no VERSION file)"
        return 0
    fi

    cp "$EMBEDDING_SRC/Dockerfile" "$EMBEDDING_DST/"
    cp "$EMBEDDING_SRC/service.py" "$EMBEDDING_DST/"
    cp "$EMBEDDING_SRC/download_model.py" "$EMBEDDING_DST/"

    echo "Embedding service synced"
}

main() {
    echo "=== scillm services sync ==="
    echo ""

    sync_memory
    sync_embedding

    echo ""
    if $check_mode; then
        echo "Check complete."
    else
        echo "Sync complete. Don't forget to commit the changes."
    fi
}

main
