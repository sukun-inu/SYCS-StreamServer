ARG CUDA_VERSION=12.6.3-runtime-ubuntu24.04

# ── Stage 1: mediamtx バイナリ取得 ───────────────────────────────────────────
FROM bluenviron/mediamtx:latest AS mediamtx

# ── Stage 2: mwader/static-ffmpeg ────────────────────────────────────────────
# Docker multi-stage 専用の static build。LL-HLS (hls_part_duration) 対応済み。
# /ffmpeg, /ffprobe が置かれている。
FROM mwader/static-ffmpeg:latest AS ffmpeg-static

# ── Stage 3: 実行イメージ ─────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx      /mediamtx                /usr/local/bin/mediamtx
COPY --from=ffmpeg-static /ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg-static /ffprobe /usr/local/bin/ffprobe

# ビルド時にLL-HLS対応を確認: 実際に -hls_part_duration を渡してエラーが出ないかテスト
RUN ffmpeg -version 2>&1 | head -1 \
    && ffmpeg -encoders 2>&1 | grep -q "libx264" \
        || { echo "FAIL: libx264 encoder missing"; exit 1; } \
    && mkdir -p /tmp/hls_test \
    && ffmpeg -v error \
        -f lavfi -i "color=c=black:s=320x240:r=30" -t 1 \
        -c:v libx264 -preset ultrafast \
        -f hls \
        -hls_time 1 \
        -hls_part_duration 0.2 \
        -hls_segment_type fmp4 \
        -hls_fmp4_init_filename "init.mp4" \
        /tmp/hls_test/test.m3u8 \
        || { echo "FAIL: hls_part_duration not accepted (LL-HLS unavailable)"; exit 1; } \
    && rm -rf /tmp/hls_test \
    && echo "[dockerfile] FFmpeg OK — LL-HLS (hls_part_duration) confirmed"

RUN mkdir -p /etc/mediamtx /hls/live
COPY mediamtx.yml  /etc/mediamtx/mediamtx.yml
COPY publish.sh    /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
