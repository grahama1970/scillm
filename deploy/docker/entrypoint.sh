#!/usr/bin/env bash
set -euo pipefail

echo "=== scillm entrypoint: generating bifrost config from proxy_server_config ==="

# Wipe stale Bifrost SQLite so it re-reads config.json on startup
rm -f /app/bifrost/data/config.db /app/bifrost/data/config.db-shm /app/bifrost/data/config.db-wal

# Generate bifrost.json from scillm's proxy config (single source of truth)
python3 /app/deploy/docker/generate_bifrost_config.py /app/config.yaml /app/bifrost/data/config.json

echo "=== scillm entrypoint: starting supervisord ==="
exec supervisord -n -c /etc/supervisor/conf.d/scillm.conf
