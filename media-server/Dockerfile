# ── Stage 1: mediamtx バイナリ取得 ───────────────────────────────────────────
FROM bluenviron/mediamtx:latest AS mediamtx

# ── Stage 2: FFmpeg ソースビルド (NVENC + LL-HLS) ────────────────────────────
# Ubuntu パッケージ版 FFmpeg 6.1.1 は hls_part_duration 未対応のため自前ビルド
FROM ubuntu:24.04 AS ffmpeg-builder
ARG DEBIAN_FRONTEND=noninteractive
ARG FFMPEG_VERSION=7.1.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential pkg-config nasm yasm wget ca-certificates \
        libx264-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# nv-codec-headers: NVENC/NVDEC API スタブ (実装はホストドライバが dlopen で提供)
RUN git clone --depth 1 https://github.com/FFmpeg/nv-codec-headers.git /tmp/nv-codec \
    && make -C /tmp/nv-codec install \
    && rm -rf /tmp/nv-codec

RUN wget -q "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.bz2" -O /tmp/ffmpeg.tar.bz2 \
    && tar xf /tmp/ffmpeg.tar.bz2 -C /tmp \
    && cd "/tmp/ffmpeg-${FFMPEG_VERSION}" \
    && ./configure \
        --prefix=/usr/local \
        --pkg-config-flags="--static" \
        --enable-gpl \
        --enable-nonfree \
        --enable-nvenc \
        --enable-nvdec \
        --enable-libx264 \
        --enable-static \
        --disable-shared \
        --disable-doc \
        --disable-debug \
        --disable-ffplay \
    && make -j"$(nproc)" \
    && make install \
    && strip /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg*

# ── Stage 3: 実行イメージ ─────────────────────────────────────────────────────
ARG CUDA_VERSION=12.6.3-runtime-ubuntu24.04
FROM nvidia/cuda:${CUDA_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx       /mediamtx            /usr/local/bin/mediamtx
COPY --from=ffmpeg-builder /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-builder /usr/local/bin/ffprobe /usr/local/bin/ffprobe

RUN apt-get update && apt-get install -y --no-install-recommends \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/mediamtx /hls/live
COPY mediamtx.yml /etc/mediamtx/mediamtx.yml
COPY publish.sh   /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/mediamtx /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
             /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935
ENTRYPOINT ["/entrypoint.sh"]
