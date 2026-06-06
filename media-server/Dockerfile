# CUDA_VERSION は FROM 命令で使うため全 stage より前に宣言する
ARG CUDA_VERSION=12.3.1-runtime-ubuntu22.04

# Stage 1: mediamtx バイナリ取得
FROM bluenviron/mediamtx:latest AS mediamtx

# Stage 2: CUDA + FFmpeg + mediamtx
FROM nvidia/cuda:${CUDA_VERSION}

ARG DEBIAN_FRONTEND=noninteractive

COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/mediamtx /hls/live

COPY mediamtx.yml /etc/mediamtx/mediamtx.yml
COPY publish.sh /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/mediamtx /usr/local/bin/publish.sh /entrypoint.sh

EXPOSE 1935

ENTRYPOINT ["/entrypoint.sh"]
