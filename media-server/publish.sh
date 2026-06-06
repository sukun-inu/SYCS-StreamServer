#!/bin/bash

# ${1:?} より前に引数をログ (set -e なし、失敗しても続行)
echo "$(date -u +%FT%TZ) [publish-debug] PID=$$ args=$# argv: $*" \
    >> /tmp/publish_debug.log 2>&1 || true

STREAM_NAME="${1}"
if [ -z "${STREAM_NAME}" ]; then
    echo "$(date -u +%FT%TZ) [publish-error] stream name empty, argv: $*" \
        >> /tmp/publish_debug.log 2>&1 || true
    exit 1
fi

set -e

# exec_push は stdout/stderr を閉じるためファイルに出力する
LOG="/tmp/publish_${STREAM_NAME}.log"
exec >>"${LOG}" 2>&1
echo "[publish:$$] $(date -u +%FT%TZ) start key=${STREAM_NAME}"

VIDEO_BITRATE="${VIDEO_BITRATE:-6000k}"
VIDEO_BITRATE_LOW="${VIDEO_BITRATE_LOW:-2000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-320k}"
HLS_SEGMENT_TIME="${HLS_SEGMENT_TIME:-0.5}"
HLS_PART_DURATION="${HLS_PART_DURATION:-0.1}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"

OUTPUT_DIR="/hls/live/${STREAM_NAME}"
mkdir -p "${OUTPUT_DIR}/high" "${OUTPUT_DIR}/low"

cleanup() {
    trap - EXIT TERM INT HUP
    [ -n "${PID}" ] && kill "${PID}" 2>/dev/null
    wait 2>/dev/null
    exit 0
}
trap cleanup EXIT TERM INT HUP

# VBR パラメータ計算: maxrate = avg × 1.35、bufsize = maxrate × 2
vbr_params() {
    local n="${1%k}"
    echo "${n}k $(( n * 135 / 100 ))k $(( n * 135 / 50 ))k"
}
read -r BV_H BV_H_MAX BV_H_BUF <<< "$(vbr_params "${VIDEO_BITRATE}")"
read -r BV_L BV_L_MAX BV_L_BUF <<< "$(vbr_params "${VIDEO_BITRATE_LOW}")"

# h264_nvenc エンコーダの有無を確認する。
# -hwaccels はデコード用アクセラレーション一覧であり NVENC (エンコーダ) は出ない。
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc'; then
    echo "[publish:${STREAM_NAME}] h264_nvenc ABR VBR  high=${BV_H} low=${BV_L}"
    VC=(
        -c:v:0 h264_nvenc -rc:v:0 vbr -preset:v:0 llhq -tune:v:0 ll
        -b:v:0 "${BV_H}" -maxrate:v:0 "${BV_H_MAX}" -bufsize:v:0 "${BV_H_BUF}"
        -c:v:1 h264_nvenc -rc:v:1 vbr -preset:v:1 llhq -tune:v:1 ll
        -b:v:1 "${BV_L}" -maxrate:v:1 "${BV_L_MAX}" -bufsize:v:1 "${BV_L_BUF}"
    )
else
    echo "[publish:${STREAM_NAME}] h264_nvenc 不可 → libx264 ABR  high=${BV_H} low=${BV_L}"
    VC=(
        -c:v:0 libx264 -preset:v:0 ultrafast -tune:v:0 zerolatency
        -b:v:0 "${BV_H}" -maxrate:v:0 "${BV_H_MAX}" -bufsize:v:0 "${BV_H_BUF}"
        -c:v:1 libx264 -preset:v:1 ultrafast -tune:v:1 zerolatency
        -b:v:1 "${BV_L}" -maxrate:v:1 "${BV_L_MAX}" -bufsize:v:1 "${BV_L_BUF}"
    )
fi

ffmpeg \
    -loglevel warning \
    -fflags +genpts \
    -use_wallclock_as_timestamps 1 \
    -i "rtmp://127.0.0.1:1935/live/${STREAM_NAME}" \
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
    -hls_flags delete_segments+split_by_time+low_latency+temp_file+program_date_time \
    -hls_segment_type fmp4 \
    -hls_fmp4_init_filename "init_%v.mp4" \
    -hls_part_duration "${HLS_PART_DURATION}" \
    -hls_segment_filename "${OUTPUT_DIR}/%v/seg%05d.m4s" \
    -master_pl_name master.m3u8 \
    -var_stream_map "v:0,a:0,name:high v:1,a:1,name:low" \
    "${OUTPUT_DIR}/%v/index.m3u8" &
PID=$!

wait "${PID}" 2>/dev/null || true
