#!/bin/bash
set -e

mkdir -p /hls/live

# FFmpeg バイナリが正常に動作するか起動時に検証する。
# ビルドが壊れたイメージを無言でデプロイしてしまう問題を防ぐ。
if ! /usr/local/bin/ffmpeg -version >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: ffmpeg binary is broken or missing" >&2
    exit 1
fi
if ! /usr/local/bin/ffmpeg -encoders 2>/dev/null | grep -q 'libx264'; then
    echo "[entrypoint] FATAL: ffmpeg built without libx264" >&2
    exit 1
fi

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "N/A")
if [ "${GPU_NAME}" = "N/A" ]; then
    echo "[entrypoint] nvidia-smi not found — NVENC unavailable, will fallback to libx264"
else
    echo "[entrypoint] GPU: ${GPU_NAME}"
fi

exec /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
