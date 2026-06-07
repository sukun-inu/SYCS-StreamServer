ARG CUDA_VERSION=12.6.3-runtime-ubuntu24.04

# ── Stage 1: mediamtx バイナリ取得 ───────────────────────────────────────────
FROM bluenviron/mediamtx:latest AS mediamtx

# ── Stage 2: 実行イメージ ─────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# ビルド時に動作確認: 失敗したら CI でイメージが push されない
RUN ffmpeg -version 2>&1 | grep -q "ffmpeg version" \
    && ffmpeg -encoders 2>&1 | grep -q "libx264" \
    && echo "[dockerfile] FFmpeg OK"

RUN mkdir -p /etc/mediamtx /hls/live
COPY mediamtx.yml /etc/mediamtx/mediamtx.yml
COPY publish.sh   /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
