#!/bin/bash
# nginx-rtmp の exec_push から呼ばれる。$1 = ストリームキー
#
# 【出力2系統】
#   pc/      … LL-HLS fmp4  (VRChat PC / ブラウザ向け、超低遅延)
#   android/ … 標準 HLS TS   (VRChat Android 向け、最大互換)
#
# nginx-rtmp は "play" 接続を複数受け付けるので、
# 2 つの FFmpeg プロセスが独立して同じ RTMP ストリームを購読できる。
set -e

STREAM_NAME="${1:?stream name required}"
VIDEO_BITRATE="${VIDEO_BITRATE:-4000k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
HLS_SEGMENT_TIME="${HLS_SEGMENT_TIME:-0.5}"
HLS_PART_DURATION="${HLS_PART_DURATION:-0.1}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"

OUTPUT_DIR="/hls/live/${STREAM_NAME}"
mkdir -p "${OUTPUT_DIR}/pc" "${OUTPUT_DIR}/android"

# 子プロセスを確実に終了させる
cleanup() {
    [ -n "${PID_PC}"  ] && kill "${PID_PC}"  2>/dev/null
    [ -n "${PID_AND}" ] && kill "${PID_AND}" 2>/dev/null
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

# 共通引数配列
COMMON=(
    -loglevel warning
    -fflags +genpts
    -use_wallclock_as_timestamps 1
    -i "rtmp://localhost:1935/live/${STREAM_NAME}"
    "${VC[@]}"
    -g 60
    -keyint_min 60
    -sc_threshold 0
    -c:a aac
    -ar 44100
    -b:a "${AUDIO_BITRATE}"
    -af "aresample=async=1000"
)

# ── PC 向け: LL-HLS fmp4 ──────────────────────────────────────────────────────
# EXT-X-PART によるパーツ配信で ~100ms 粒度の超低遅延を実現する
ffmpeg "${COMMON[@]}" \
    -f hls \
    -hls_time "${HLS_SEGMENT_TIME}" \
    -hls_list_size "${HLS_LIST_SIZE}" \
    -hls_flags delete_segments+split_by_time+low_latency+temp_file+program_date_time \
    -hls_segment_type fmp4 \
    -hls_fmp4_init_filename init.mp4 \
    -hls_part_duration "${HLS_PART_DURATION}" \
    -hls_segment_filename "${OUTPUT_DIR}/pc/seg%05d.m4s" \
    -master_pl_name master.m3u8 \
    "${OUTPUT_DIR}/pc/index.m3u8" &
PID_PC=$!

# nginx-rtmp の 2 本目の subscribe が安定するまで待機
sleep 0.5

# ── Android 向け: 標準 HLS MPEG-TS ───────────────────────────────────────────
# fmp4 を使わず TS にすることで AVPro Mobile / ExoPlayer の互換性を最大化する
# LL-HLS は TS 非対応のため通常 HLS。最小遅延は segment_time × 3 ≒ 1.5s
ffmpeg "${COMMON[@]}" \
    -f hls \
    -hls_time "${HLS_SEGMENT_TIME}" \
    -hls_list_size "${HLS_LIST_SIZE}" \
    -hls_flags delete_segments+split_by_time+temp_file+program_date_time \
    -hls_segment_type mpegts \
    -hls_segment_filename "${OUTPUT_DIR}/android/seg%05d.ts" \
    -master_pl_name master.m3u8 \
    "${OUTPUT_DIR}/android/index.m3u8" &
PID_AND=$!

# 両プロセスが終わるまで待つ (trap が先に発火して exit することが多い)
wait "${PID_PC}" "${PID_AND}" 2>/dev/null || true
