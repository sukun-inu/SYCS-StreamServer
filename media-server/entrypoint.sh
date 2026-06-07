#!/bin/bash
set -e

mkdir -p /hls

if ! ffmpeg -version >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: ffmpeg が起動できません。" >&2
    ffmpeg -version 2>&1 | head -20 >&2 || true
    if command -v ldd >/dev/null 2>&1; then
        ldd "$(command -v ffmpeg)" 2>&1 | grep "not found" >&2 || true
    fi
    exit 1
fi

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "N/A")
if [ "${GPU_NAME}" = "N/A" ]; then
    echo "[entrypoint] nvidia-smi not found — NVENC unavailable, will fallback to libx264"
else
    echo "[entrypoint] GPU: ${GPU_NAME}"
fi

if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc'; then
    echo "[entrypoint] FFmpeg: h264_nvenc available"
else
    echo "[entrypoint] FFmpeg: h264_nvenc unavailable, will fallback to libx264"
fi

if ffmpeg -hide_banner -filters 2>/dev/null | grep -q 'scale_cuda'; then
    echo "[entrypoint] FFmpeg: scale_cuda available"
else
    echo "[entrypoint] FFmpeg: scale_cuda unavailable, scaling may use CPU"
fi

exec /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
