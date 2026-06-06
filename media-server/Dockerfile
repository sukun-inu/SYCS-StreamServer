ARG CUDA_VERSION=12.3.1-runtime-ubuntu22.04
FROM nvidia/cuda:${CUDA_VERSION}

ARG DEBIAN_FRONTEND=noninteractive

# nginx-rtmp + FFmpeg (Ubuntu 22.04 の FFmpeg 4.4.2 は LL-HLS 対応済み)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        libnginx-mod-rtmp \
        ffmpeg \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY nginx.conf /etc/nginx/nginx.conf
COPY publish.sh /usr/local/bin/publish.sh
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/publish.sh /entrypoint.sh \
    && mkdir -p /hls/live \
    && chown -R www-data:www-data /hls

EXPOSE 1935

ENTRYPOINT ["/entrypoint.sh"]
