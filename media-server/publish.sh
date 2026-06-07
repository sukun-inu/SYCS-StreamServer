#!/bin/bash
# mediamtx の runOnReady フックから呼び出される。
# 役割: RTSP 入力を 720p にトランスコードして live/{key}_transcode パスへ RTMP で再配信。
# LL-HLS セグメント生成は mediamtx が担当するため FFmpeg は HLS 出力しない。

STREAM_NAME="${MTX_PATH##*/}"

# "_transcode" サフィックスのパスは本スクリプトが生成した RTMP 出力。
# mediamtx が runOnReady を再帰的に発火させるため、ここで抜ける。
if [[ "${STREAM_NAME}" == *_transcode ]]; then
    exit 0
fi

# ストリームキーのバリデーション
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

VIDEO_BITRATE_LOW="${VIDEO_BITRATE_LOW:-2000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
VIDEO_FPS_LOW="${VIDEO_FPS_LOW:-30}"
TRANSCODE_RESTART_DELAY="${TRANSCODE_RESTART_DELAY:-1}"

PID=""
SLEEP_PID=""
FFMPEG_EXIT=0

cleanup() {
    trap - EXIT TERM INT HUP
    [ -n "${PID}" ] && kill "${PID}" 2>/dev/null || true
    wait "${PID}" 2>/dev/null || true
    [ -n "${SLEEP_PID}" ] && kill "${SLEEP_PID}" 2>/dev/null || true
    wait "${SLEEP_PID}" 2>/dev/null || true
    flock -u 9 2>/dev/null || true
    exit "${FFMPEG_EXIT}"
}
trap cleanup EXIT TERM INT HUP

INPUT="rtsp://127.0.0.1:8554/${MTX_PATH}"
OUTPUT="rtmp://127.0.0.1:1935/live/${STREAM_NAME}_transcode"
echo "[publish:${STREAM_NAME}] input=${INPUT} → output=${OUTPUT}"

vbr_params() {
    local n="${1%k}"
    echo "${n}k $(( n * 135 / 100 ))k $(( n * 135 / 50 ))k"
}
read -r BV_L BV_L_MAX BV_L_BUF <<< "$(vbr_params "${VIDEO_BITRATE_LOW}")"

ffmpeg_has_encoder() {
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "$1"
}

ffmpeg_has_filter() {
    ffmpeg -hide_banner -filters 2>/dev/null | grep -q "$1"
}

IN_HW=()
VF=()
VC=()
KF=(-r "${VIDEO_FPS_LOW}" -fps_mode cfr -g "${VIDEO_FPS_LOW}" -keyint_min "${VIDEO_FPS_LOW}" \
    -force_key_frames "expr:gte(t,n_forced*1)")

if ffmpeg_has_encoder 'h264_nvenc'; then
    if ffmpeg_has_filter 'scale_cuda'; then
        echo "[publish:${STREAM_NAME}] pipeline=cuda_decode+scale_cuda+h264_nvenc low=${BV_L} fps=${VIDEO_FPS_LOW}"
        IN_HW=(-hwaccel cuda -hwaccel_output_format cuda -extra_hw_frames 8)
        VF=(-vf "scale_cuda=w=-2:h=720:format=nv12")
    else
        echo "[publish:${STREAM_NAME}] pipeline=cpu_scale+h264_nvenc low=${BV_L} fps=${VIDEO_FPS_LOW} (scale_cuda unavailable)"
        VF=(-vf "scale=-2:720,fps=${VIDEO_FPS_LOW}")
    fi
    # -preset p4 -tune ll: 新 NVENC API (旧 llhq 相当)。低遅延 + 中品質。
    # -forced-idr 1: セグメント境界を IDR フレームにして mediamtx が正確に切れるようにする。
    # sc_threshold は libx264 専用のため NVENC には渡さない。
    VC=(-c:v h264_nvenc -rc vbr -preset p4 -tune ll -bf 0 -forced-idr 1
        -b:v "${BV_L}" -maxrate "${BV_L_MAX}" -bufsize "${BV_L_BUF}")
else
    echo "[publish:${STREAM_NAME}] pipeline=cpu_scale+libx264 low=${BV_L} fps=${VIDEO_FPS_LOW}"
    VF=(-vf "scale=-2:720,fps=${VIDEO_FPS_LOW}")
    VC=(-c:v libx264 -preset ultrafast -tune zerolatency
        -b:v "${BV_L}" -maxrate "${BV_L_MAX}" -bufsize "${BV_L_BUF}")
    KF+=(-sc_threshold 0)
fi

while true; do
    FFMPEG_EXIT=0
    PID=""
    echo "[publish:${STREAM_NAME}] starting ffmpeg transcode..."
    ffmpeg \
        -loglevel warning \
        -rtsp_transport tcp \
        -fflags +genpts \
        -use_wallclock_as_timestamps 1 \
        "${IN_HW[@]}" \
        -i "${INPUT}" \
        "${VF[@]}" \
        "${VC[@]}" \
        "${KF[@]}" \
        -c:a aac -b:a "${AUDIO_BITRATE}" -ar 44100 -af "aresample=async=1000" \
        -f flv "${OUTPUT}" &
    PID=$!
    echo "[publish:${STREAM_NAME}] ffmpeg PID=${PID}"

    wait "${PID}" 2>/dev/null || FFMPEG_EXIT=$?
    PID=""
    FFMPEG_EXIT="${FFMPEG_EXIT:-0}"
    echo "[publish:${STREAM_NAME}] ffmpeg exited with code ${FFMPEG_EXIT}; restarting in ${TRANSCODE_RESTART_DELAY}s while source is ready"

    sleep "${TRANSCODE_RESTART_DELAY}" &
    SLEEP_PID=$!
    wait "${SLEEP_PID}" 2>/dev/null || true
    SLEEP_PID=""
done
