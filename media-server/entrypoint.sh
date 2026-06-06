#!/bin/bash
set -e

mkdir -p /hls/live

if command -v nvidia-smi &>/dev/null; then
    echo "[entrypoint] GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"
else
    echo "[entrypoint] nvidia-smi not found — GPU encoding may not be available"
fi

exec /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
