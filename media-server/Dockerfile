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
    && curl -fsSL "${FFMPEG_URL}" \
       | tar -xJf - --strip-components=1 -C /tmp --wildcards '*/ffmpeg' '*/ffprobe' \
    && install -m755 /tmp/ffmpeg /tmp/ffprobe /usr/local/bin/

# ── Stage 3: 実行イメージ ─────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx      /mediamtx                /usr/local/bin/mediamtx
COPY --from=ffmpeg-static /usr/local/bin/ffmpeg    /usr/local/bin/ffmpeg
COPY --from=ffmpeg-static /usr/local/bin/ffprobe   /usr/local/bin/ffprobe

# ビルド時にLL-HLS対応を確認: hls_part_duration がなければ CI でイメージが push されない
RUN ffmpeg -version 2>&1 | grep -q "ffmpeg version" \
    && ffmpeg -encoders 2>&1 | grep -q "libx264" \
    && ffmpeg -h muxer=hls 2>&1 | grep -q "hls_part_duration" \
    && echo "[dockerfile] FFmpeg OK — LL-HLS (hls_part_duration) confirmed"

RUN mkdir -p /etc/mediamtx /hls/live
COPY mediamtx.yml  /etc/mediamtx/mediamtx.yml
COPY publish.sh    /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
