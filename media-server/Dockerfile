ARG CUDA_VERSION=12.6.3-runtime-ubuntu24.04

# ── Stage 1: mediamtx バイナリ取得 ───────────────────────────────────────────
FROM bluenviron/mediamtx:latest AS mediamtx

# ── Stage 2: 実行イメージ ─────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx

# FFmpeg は apt で入れる。LL-HLS セグメント生成は mediamtx が担当するため
# hls_part_duration 不要。RTMP トランスコード (libx264 / h264_nvenc) のみ使用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ビルド時に FFmpeg の最低限の動作を確認
RUN ffmpeg -version 2>&1 | grep -q "ffmpeg version" \
    && ffmpeg -encoders 2>&1 | grep -q "libx264" \
    && ffmpeg -protocols 2>&1 | grep -q "rtmp" \
    && echo "[dockerfile] FFmpeg OK — libx264 + RTMP confirmed"

RUN mkdir -p /etc/mediamtx /hls
COPY mediamtx.yml  /etc/mediamtx/mediamtx.yml
COPY publish.sh    /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
