#!/bin/bash
set -e

# volume マウント後も www-data (nginx ワーカー) が /hls に書き込めるよう権限を修正する。
# Dockerfile の chown はイメージビルド時のみ有効で、既存 volume には引き継がれない。
mkdir -p /hls/live
chown -R www-data:www-data /hls 2>/dev/null || true

if command -v nvidia-smi &>/dev/null; then
    echo "[entrypoint] GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"
else
    echo "[entrypoint] nvidia-smi not found — GPU encoding may not be available"
fi

exec nginx -g "daemon off;"
