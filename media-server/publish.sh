#!/bin/bash
# mediamtx の runOnReady フックから呼び出される。
# 環境変数 MTX_PATH="live/..." でストリームパスを受け取る。

STREAM_NAME="${MTX_PATH##*/}"

# ストリームキーのバリデーション: 英数字・ハイフン・アンダースコアのみ許可。
if [[ ! "${STREAM_NAME}" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo "$(date -u +%FT%TZ) [publish-error] 無効な STREAM_NAME: '${STREAM_NAME}' (MTX_PATH='${MTX_PATH}')" >&2
    exit 1
fi

# 同一ストリームキーの並列起動を防止
LOCK="/tmp/publish_${STREAM_NAME}.lock"
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) [publish] ${STREAM_NAME} already running, exit" >&2
    trap - EXIT TERM INT HUP
    exit 0
fi

set -e
LOG="/tmp/publish_${STREAM_NAME}.log"
exec >>"${LOG}" 2>&1
echo "[publish:$$] $(date -u +%FT%TZ) start key=${STREAM_NAME} MTX_PATH=${MTX_PATH}"

VIDEO_BITRATE="${VIDEO_BITRATE:-6000k}"
VIDEO_BITRATE_LOW="${VIDEO_BITRATE_LOW:-2000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-320k}"
HLS_SEGMENT_TIME="${HLS_SEGMENT_TIME:-0.5}"
HLS_PART_DURATION="${HLS_PART_DURATION:-0.1}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"

OUTPUT_DIR="/hls/live/${STREAM_NAME}"
mkdir -p "${OUTPUT_DIR}/high" "${OUTPUT_DIR}/low"

PID=""
FFMPEG_EXIT=0

cleanup() {
    trap - EXIT TERM INT HUP
    [ -n "${PID}" ] && kill "${PID}" 2>/dev/null || true
    wait "${PID}" 2>/dev/null || true
    flock -u 9 2>/dev/null || true
    exit "${FFMPEG_EXIT}"
}
trap cleanup EXIT TERM INT HUP

INPUT="rtsp://127.0.0.1:8554/${MTX_PATH}"
echo "[publish:${STREAM_NAME}] input=${INPUT}"

vbr_params() {
    local n="${1%k}"
    echo "${n}k $(( n * 135 / 100 ))k $(( n * 135 / 50 ))k"
}
read -r BV_H BV_H_MAX BV_H_BUF <<< "$(vbr_params "${VIDEO_BITRATE}")"
read -r BV_L BV_L_MAX BV_L_BUF <<< "$(vbr_params "${VIDEO_BITRATE_LOW}")"

if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc'; then
    echo "[publish:${STREAM_NAME}] encoder=h264_nvenc  high=${BV_H} low=${BV_L}"
    VC=(
        -c:v:0 h264_nvenc -rc:v:0 vbr -preset:v:0 llhq -tune:v:0 ll
        -b:v:0 "${BV_H}" -maxrate:v:0 "${BV_H_MAX}" -bufsize:v:0 "${BV_H_BUF}"
        -c:v:1 h264_nvenc -rc:v:1 vbr -preset:v:1 llhq -tune:v:1 ll
        -b:v:1 "${BV_L}" -maxrate:v:1 "${BV_L_MAX}" -bufsize:v:1 "${BV_L_BUF}"
    )
else
    echo "[publish:${STREAM_NAME}] encoder=libx264  high=${BV_H} low=${BV_L}"
    VC=(
        -c:v:0 libx264 -preset:v:0 ultrafast -tune:v:0 zerolatency
        -b:v:0 "${BV_H}" -maxrate:v:0 "${BV_H_MAX}" -bufsize:v:0 "${BV_H_BUF}"
        -c:v:1 libx264 -preset:v:1 ultrafast -tune:v:1 zerolatency
        -b:v:1 "${BV_L}" -maxrate:v:1 "${BV_L_MAX}" -bufsize:v:1 "${BV_L_BUF}"
    )
fi

echo "[publish:${STREAM_NAME}] starting ffmpeg..."
ffmpeg \
    -loglevel warning \
    -rtsp_transport tcp \
    -fflags +genpts \
    -use_wallclock_as_timestamps 1 \
    -i "${INPUT}" \
    -filter_complex "[0:v]split=2[vh][vl];[vl]scale=-2:720[vls]" \
    -map "[vh]"  -map 0:a \
    -map "[vls]" -map 0:a \
    "${VC[@]}" \
    -g:v:0 60 -keyint_min:v:0 60 -sc_threshold:v:0 0 \
    -g:v:1 60 -keyint_min:v:1 60 -sc_threshold:v:1 0 \
    -c:a:0 aac -ar:a:0 44100 -b:a:0 "${AUDIO_BITRATE}" -af:a:0 "aresample=async=1000" \
    -c:a:1 aac -ar:a:1 44100 -b:a:1 128k              -af:a:1 "aresample=async=1000" \
    -f hls \
    -hls_time "${HLS_SEGMENT_TIME}" \
    -hls_list_size "${HLS_LIST_SIZE}" \
    -hls_flags delete_segments+split_by_time+temp_file+program_date_time \
    -hls_segment_type fmp4 \
    -hls_fmp4_init_filename "init_%v.mp4" \
    -hls_part_duration "${HLS_PART_DURATION}" \
    -hls_segment_filename "${OUTPUT_DIR}/%v/seg%05d.m4s" \
    -master_pl_name master.m3u8 \
    -var_stream_map "v:0,a:0,name:high v:1,a:1,name:low" \
    "${OUTPUT_DIR}/%v/index.m3u8" &
PID=$!
echo "[publish:${STREAM_NAME}] ffmpeg PID=${PID}"

wait "${PID}" 2>/dev/null || FFMPEG_EXIT=$?
FFMPEG_EXIT="${FFMPEG_EXIT:-0}"
echo "[publish:${STREAM_NAME}] ffmpeg exited with code ${FFMPEG_EXIT}"
exit "${FFMPEG_EXIT}"
