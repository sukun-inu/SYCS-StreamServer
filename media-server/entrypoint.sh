#!/bin/bash
set -e

mkdir -p /hls/live

# GPU 確認ログ (起動時のみ)
if command -v nvidia-smi &>/dev/null; then
    echo "[entrypoint] GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"
else
    echo "[entrypoint] nvidia-smi not found — GPU encoding may not be available"
fi

exec nginx -g "daemon off;"
