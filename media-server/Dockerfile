ARG CUDA_VERSION=12.6.3-runtime-ubuntu24.04

# ── Stage 1: mediamtx バイナリ取得 ───────────────────────────────────────────
FROM bluenviron/mediamtx:latest AS mediamtx

# ── Stage 2: johnvansickle static FFmpeg 取得 ────────────────────────────────
# static ビルドなので shared lib 依存なし。hls_part_duration (LL-HLS) を含む 7.x 系。
FROM ubuntu:24.04 AS ffmpeg-static
ARG DEBIAN_FRONTEND=noninteractive
ARG FFMPEG_URL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -A "Mozilla/5.0 (X11; Linux x86_64)" "${FFMPEG_URL}" \
       | tar -xJf - --strip-components=1 -C /tmp --wildcards '*/ffmpeg' '*/ffprobe' \
    && install -m755 /tmp/ffmpeg /tmp/ffprobe /usr/local/bin/

# ── Stage 3: 実行イメージ ─────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx      /mediamtx                /usr/local/bin/mediamtx
COPY --from=ffmpeg-static /usr/local/bin/ffmpeg    /usr/local/bin/ffmpeg
COPY --from=ffmpeg-static /usr/local/bin/ffprobe   /usr/local/bin/ffprobe

# ビルド時にLL-HLS対応を確認: 失敗時は原因を表示してビルドを止める
RUN echo "=== ffmpeg version ===" && ffmpeg -version 2>&1 | head -3 \
    && ffmpeg -version 2>&1 | grep -q "ffmpeg version" \
        || { echo "FAIL: ffmpeg binary not working"; exit 1; } \
    && echo "=== libx264 ===" \
    && ffmpeg -encoders 2>&1 | grep libx264 || true \
    && ffmpeg -encoders 2>&1 | grep -q "libx264" \
        || { echo "FAIL: libx264 encoder missing"; exit 1; } \
    && echo "=== hls_part_duration ===" \
    && ffmpeg -h muxer=hls 2>&1 | grep hls_part_duration || true \
    && ffmpeg -h muxer=hls 2>&1 | grep -q "hls_part_duration" \
        || { echo "FAIL: hls_part_duration not supported (LL-HLS unavailable)"; exit 1; } \
    && echo "[dockerfile] FFmpeg OK — LL-HLS (hls_part_duration) confirmed"

RUN mkdir -p /etc/mediamtx /hls/live
COPY mediamtx.yml  /etc/mediamtx/mediamtx.yml
COPY publish.sh    /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
