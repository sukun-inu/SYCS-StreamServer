#!/bin/bash
# nginx-rtmp の exec_push から呼ばれる。$1 = ストリームキー
set -e

STREAM_NAME="${1:?stream name required}"
VIDEO_BITRATE="${VIDEO_BITRATE:-4000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
HLS_SEGMENT_TIME="${HLS_SEGMENT_TIME:-0.5}"
HLS_PART_DURATION="${HLS_PART_DURATION:-0.1}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"

OUTPUT_DIR="/hls/live/${STREAM_NAME}"
mkdir -p "${OUTPUT_DIR}"

# NVENC が使えるか確認し、使えなければ libx264 にフォールバック
if ffmpeg -hide_banner -hwaccels 2>&1 | grep -q cuda; then
    VIDEO_CODEC_ARGS=(
        -c:v h264_nvenc
        -preset:v llhq      # Low Latency High Quality
        -tune:v ll
        -rc:v cbr
        -b:v "${VIDEO_BITRATE}"
        -maxrate:v "${VIDEO_BITRATE}"
        -bufsize:v "${VIDEO_BITRATE}"
    )
    echo "[publish] Using NVENC (h264_nvenc)"
else
    VIDEO_CODEC_ARGS=(
        -c:v libx264
        -preset ultrafast
        -tune zerolatency
        -b:v "${VIDEO_BITRATE}"
    )
    echo "[publish] NVENC unavailable — falling back to libx264"
fi

exec ffmpeg \
    -loglevel warning \
    -fflags +genpts \
    -use_wallclock_as_timestamps 1 \
    -i "rtmp://localhost:1935/live/${STREAM_NAME}" \
    "${VIDEO_CODEC_ARGS[@]}" \
    -g 60 \
    -keyint_min 60 \
    -sc_threshold 0 \
    -c:a aac \
    -ar 44100 \
    -b:a "${AUDIO_BITRATE}" \
    -af "aresample=async=1000" \
    -f hls \
    -hls_time "${HLS_SEGMENT_TIME}" \
    -hls_list_size "${HLS_LIST_SIZE}" \
    -hls_flags delete_segments+split_by_time+low_latency+temp_file+program_date_time \
    -hls_segment_type fmp4 \
    -hls_fmp4_init_filename init.mp4 \
    -hls_part_duration "${HLS_PART_DURATION}" \
    -hls_segment_filename "${OUTPUT_DIR}/seg%05d.m4s" \
    -master_pl_name master.m3u8 \
    "${OUTPUT_DIR}/index.m3u8"
