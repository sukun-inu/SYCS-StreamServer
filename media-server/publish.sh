#!/bin/bash
# nginx-rtmp の exec_push から呼ばれる。$1 = ストリームキー
#
# 【fmp4 LL-HLS 単一ストリーム】
#   LL-HLS 対応プレイヤー (VRChat PC / HLS.js) → EXT-X-PART でパーツ単位取得 → ~0.5〜1s
#   非対応プレイヤー (VRChat Android / AVPro Mobile) → EXT-X-PART を無視してセグメント単位 → ~1〜1.5s
#   PC / Android 共通の単一 URL で配信。
set -e

STREAM_NAME="${1:?stream name required}"
VIDEO_BITRATE="${VIDEO_BITRATE:-4000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
HLS_SEGMENT_TIME="${HLS_SEGMENT_TIME:-0.5}"
HLS_PART_DURATION="${HLS_PART_DURATION:-0.1}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"

OUTPUT_DIR="/hls/live/${STREAM_NAME}"
mkdir -p "${OUTPUT_DIR}"

cleanup() {
    [ -n "${PID}" ] && kill "${PID}" 2>/dev/null
    wait 2>/dev/null
    exit 0
}
trap cleanup EXIT TERM INT HUP

# ── NVENC / libx264 判定 ──────────────────────────────────────────────────────
if ffmpeg -hide_banner -hwaccels 2>&1 | grep -q cuda; then
    echo "[publish:${STREAM_NAME}] NVENC (h264_nvenc)"
    VC=(
        -c:v h264_nvenc
        -preset:v llhq
        -tune:v ll
        -rc:v cbr
        -b:v "${VIDEO_BITRATE}"
        -maxrate:v "${VIDEO_BITRATE}"
        -bufsize:v "${VIDEO_BITRATE}"
    )
else
    echo "[publish:${STREAM_NAME}] NVENC 不可 → libx264"
    VC=(
        -c:v libx264
        -preset ultrafast
        -tune zerolatency
        -b:v "${VIDEO_BITRATE}"
    )
fi

# ── fmp4 LL-HLS ───────────────────────────────────────────────────────────────
ffmpeg \
    -loglevel warning \
    -fflags +genpts \
    -use_wallclock_as_timestamps 1 \
    -i "rtmp://localhost:1935/live/${STREAM_NAME}" \
    "${VC[@]}" \
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
    "${OUTPUT_DIR}/index.m3u8" &
PID=$!

wait "${PID}" 2>/dev/null || true
