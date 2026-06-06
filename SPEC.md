# SYCS Stream Server — 技術仕様書

## 目次

1. [システム概要](#1-システム概要)
2. [コンポーネント仕様](#2-コンポーネント仕様)
3. [データフロー](#3-データフロー)
4. [遅延設計](#4-遅延設計)
5. [LL-HLS 実装仕様](#5-ll-hls-実装仕様)
6. [タイムスタンプ同期・遅延蓄積対策](#6-タイムスタンプ同期遅延蓄積対策)
7. [API 仕様](#7-api-仕様)
8. [環境変数リファレンス](#8-環境変数リファレンス)
9. [コンテナ依存関係](#9-コンテナ依存関係)
10. [ngrok 構成](#10-ngrok-構成)

---

## 1. システム概要

| 項目 | 内容 |
|------|------|
| 目的 | OBS → VRChat 向けライブ映像のリアルタイム配信 |
| 目標遅延 | PC: **0.5〜1秒** / Android: **1〜1.5秒** (Cloudflare 経由) |
| 配信プロトコル | fmp4 LL-HLS ABR VBR (high/low 2レベル、PC / Android 共通 URL) |
| 入力プロトコル | RTMP (OBS Studio 標準出力) |
| エンコーダ | NVIDIA NVENC `h264_nvenc` (GTX1650)、libx264 自動フォールバック |
| サイト公開 | Cloudflare Tunnel (別スタック管理) |
| RTMP 公開 | ngrok TCP Tunnel (ngrok-rtmp コンテナ) |
| ネットワーク | `network_mode: host` — 全コンテナがホスト NW を共有 |
| 設定方式 | **全て環境変数** (`.env` / Portainer) — 設定ファイル不要 |
| ランタイム | Docker Compose v2 + NVIDIA Container Toolkit |

---

## 2. コンポーネント仕様

### 2.1 media-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `nvidia/cuda:${CUDA_VERSION}` (デフォルト: `12.3.1-runtime-ubuntu22.04`) |
| プロセス | nginx (nginx-rtmp モジュール組み込み) |
| 役割 | RTMP 受信、FFmpeg 起動管理、2系統 HLS 生成 |
| GPU アクセス | `deploy.resources.reservations.devices` (NVIDIA) |
| 公開ポート | `${RTMP_PORT:-1935}/tcp` |
| 書き込みパス | `/hls/live/<key>/pc/` および `/hls/live/<key>/android/` |

**ABR VBR ストリームの仕組み:**

fmp4 LL-HLS は後方互換設計のため、LL-HLS 非対応プレイヤーでも動作する。  
master.m3u8 に高画質・低画質を列挙し、プレイヤーが帯域に応じて自動選択する。

```
OBS (publisher)
    │ rtmp://localhost:1935/live/stream
    ▼
nginx-rtmp (exec_push → publish.sh)
    │
    └── FFmpeg (filter_complex split)
         ├── /hls/live/{key}/high/  元解像度 VBR avg VIDEO_BITRATE
         │    ├── master.m3u8 (ABR マスター ← ここに high/low 両方列挙)
         │    ├── index.m3u8  (EXT-X-PART 付き)
         │    ├── init_high.mp4
         │    └── seg*.m4s
         └── /hls/live/{key}/low/  720p VBR avg VIDEO_BITRATE_LOW
              ├── index.m3u8
              ├── init_low.mp4
              └── seg*.m4s

VRChat PC  → ABR 自動選択 + EXT-X-PART → ~0.5〜1s
VRChat AND → ABR 自動選択 + セグメントフォールバック → ~1〜1.5s
```

**FFmpeg コマンド詳細 (publish.sh):**

```bash
# VBR パラメータ: maxrate = avg × 1.35、bufsize = maxrate × 2
-filter_complex "[0:v]split=2[vh][vl];[vl]scale=-2:720[vls]"
-map "[vh]"  -map 0:a   # high (v:0, a:0)
-map "[vls]" -map 0:a   # low  (v:1, a:1)

-c:v:0 h264_nvenc -rc:v:0 vbr -b:v:0 VIDEO_BITRATE    -maxrate:v:0 ... -bufsize:v:0 ...
-c:v:1 h264_nvenc -rc:v:1 vbr -b:v:1 VIDEO_BITRATE_LOW -maxrate:v:1 ... -bufsize:v:1 ...
-c:a:0 aac -b:a:0 AUDIO_BITRATE  # high 音声
-c:a:1 aac -b:a:1 128k           # low 音声 (固定)

-var_stream_map "v:0,a:0,name:high v:1,a:1,name:low"
-master_pl_name master.m3u8
```

### 2.2 api-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `python:3.12-slim` |
| フレームワーク | FastAPI + uvicorn |
| 役割 | HLS 配信、playlist パッチ、ポータルサイト、ngrok RTMP URL 取得 |
| ホストポート | `8080/tcp` (`network_mode: host` により直接公開) |
| 読み取りパス | `/hls/` (read-only マウント) |
| ngrok 取得 | `httpx` で `NGROK_RTMP_API` の `/api/tunnels` をクエリ |
| サイト URL | `SITE_BASE_URL` 環境変数から取得 (Cloudflare Tunnel の公開ドメイン) |

### 2.3 ngrok-rtmp

| 項目 | 内容 |
|------|------|
| イメージ | `ngrok/ngrok:latest` |
| コマンド | `tcp --log=stdout ${NGROK_RTMP_TARGET:-localhost:1935}` |
| 役割 | RTMP ポートを TCP トンネルで公開 (LAN 外配信時) |
| 管理 UI | `localhost:4040` (host network) |
| 備考 | LAN 内配信のみなら削除可 |

### 2.4 Cloudflare Tunnel (別スタック)

このリポジトリには含まれない。別スタックで `cloudflared` を起動し、`localhost:8080` を公開する。  
公開ドメインを `SITE_BASE_URL` に設定するとポータルの URL 表示に反映される。

---

## 3. データフロー

```
OBS
 │ RTMP :1935
 ▼
nginx-rtmp (media-server)
 └── FFmpeg (h264_nvenc → ABR fmp4 LL-HLS)
      ├─ /hls/live/{key}/master.m3u8   ← ABR マスター (high/low を列挙)
      ├─ /hls/live/{key}/high/
      │    ├── index.m3u8              ← EXT-X-PART 付き
      │    ├── init_high.mp4
      │    ├── seg00001.m4s
      │    ├── seg00002.0.m4s          ← パーツ (0.1s)
      │    └── ...
      └─ /hls/live/{key}/low/
           ├── index.m3u8
           ├── init_low.mp4
           └── seg*.m4s

         ↓ Docker Volume 共有 (read-only)

FastAPI (api-server :8080)
 │ GET /hls/live/{key}/pc/master.m3u8
 │   → playlist を読み込み _patch_playlist() で加工してから返す
 │     ① EXT-X-PROGRAM-DATE-TIME 注入 (遅延蓄積防止)
 │     ② HOLD-BACK 縮小 (ライブエッジ追従促進)
 │
 │ GET /api/ngrok  → ngrok-rtmp:4040 をクエリ (RTMP URL のみ)
 │                   SITE_BASE_URL 環境変数からサイト URL を返す
 │
 ▼
Cloudflare Tunnel (別スタック)
 │ localhost:8080 → https://stream.example.com
 ▼
VRChat (AVPro) / ブラウザ (HLS.js)
```

---

## 4. 遅延設計

### 4.1 遅延内訳

| 区間 | PC (LL-HLS) | Android (HLS TS) |
|------|-------------|-----------------|
| OBS キャプチャ + RTMP | ~20ms | ~20ms |
| NVENC エンコード | ~30ms | ~30ms |
| HLS パーツ書き込み | ~100ms (0.1s パーツ) | ~500ms (0.5s セグメント) |
| playlist ブロッキング | 0ms (push 通知) | — |
| FastAPI 配信 | ~5ms | ~5ms |
| ngrok RTT | ~50〜200ms | ~50〜200ms |
| AVPro バッファ | ~300〜500ms | ~300〜500ms |
| **合計** | **~0.5〜0.9s** | **~0.9〜1.4s** |

*ngrok エッジサーバーの距離により変動あり。ローカルネットワーク視聴はそれぞれ ~0.15s / ~0.65s。*

### 4.2 各プラットフォームの遅延比較

| 方式 | 遅延 | 互換性 |
|------|------|--------|
| PC: LL-HLS fmp4 | ~1〜1.5s | VRChat PC (AVPro)、HLS.js ブラウザ |
| Android: HLS TS | ~1.5〜3s | VRChat Android (AVPro Mobile / ExoPlayer) |

### 4.3 遅延最小化のための設計判断

| 判断 | 理由 |
|------|------|
| PC 向けに LL-HLS fmp4 採用 | Apple HLS Authoring Spec 準拠で最短遅延 |
| Android 向けに MPEG-TS 採用 | fmp4 の AVPro Mobile 互換性リスク回避 |
| LL-HLS ブロッキングリクエスト対応 | クライアントポーリング遅延をゼロに |
| `temp_file` フラグ | アトミック書き込みで途中読みを防止 |
| `Cache-Control: no-store` | ngrok エッジキャッシュを完全回避 |
| HOLD-BACK を 60% に削減 | プレイヤーのライブエッジ追従距離を縮小 |

---

## 5. LL-HLS 実装仕様

### 5.1 プレイリスト形式 (PC: index.m3u8)

```m3u8
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.300  ← Python が 0.180 に縮小
#EXT-X-PART-INF:PART-TARGET=0.100000
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-MAP:URI="init.mp4"
#EXT-X-PROGRAM-DATE-TIME:2024-01-15T10:30:00.000Z               ← Python が注入
#EXTINF:0.500000,
seg00042.m4s
#EXT-X-PART:DURATION=0.100000,URI="seg00043.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.100000,URI="seg00043.1.m4s"
...
```

### 5.2 プレイリスト形式 (Android: index.m3u8)

```m3u8
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-PROGRAM-DATE-TIME:2024-01-15T10:30:00.000Z               ← Python が注入
#EXTINF:0.500000,
seg00042.ts
#EXTINF:0.500000,
seg00043.ts
...
```

### 5.3 ブロッキングリクエスト (PC のみ)

LL-HLS 対応クライアントが送るリクエスト例:
```
GET /hls/live/stream/pc/index.m3u8?_HLS_msn=44&_HLS_part=2
```

**実装フロー:**
```
Client → FastAPI (_block_until_ready)
              │ asyncio.Event で待機
              │
watchfiles → _notify_waiters ← inotify (index.m3u8 更新を検知)
              │
              └→ Event.set() → FastAPI が応答
```

タイムアウト: 5秒 (ngrok のデフォルト 100s 以内)

---

## 6. タイムスタンプ同期・遅延蓄積対策

### 6.1 問題と原因

| 問題 | 原因 |
|------|------|
| A/V ずれ | OBS 音声/映像クロックの微細なドリフト |
| タイムスタンプ不連続 | RTMP 再接続時の PTS リセット |
| 遅延の蓄積 | プレイヤーがライブエッジからの遅れを検知できない |

### 6.2 FFmpeg 側対策 (publish.sh)

| フラグ | 効果 |
|--------|------|
| `-fflags +genpts` | PTS 欠損時に自動生成 |
| `-use_wallclock_as_timestamps 1` | タイムスタンプを OS 壁時計に固定。OBS クロックドリフトをリセット |
| `-af aresample=async=1000` | 最大 1000 サンプル/秒で音声 TS を映像に同期 |
| `-hls_flags +program_date_time` | FFmpeg が `EXT-X-PROGRAM-DATE-TIME` を自動付与 |

### 6.3 Python 自動パッチ処理 (main.py: `_patch_playlist`)

すべての `.m3u8` レスポンスに適用される。PC / Android 両方が対象。

#### EXT-X-PROGRAM-DATE-TIME 注入 (`_estimate_first_segment_pdt`)

FFmpeg が生成しなかった場合のフォールバック。

```
推定時刻 = 現在時刻 - Σ(#EXTINF の時間)
```

この値を使って AVPro 等の対応プレイヤーが現在のライブエッジを特定し、自動追従する。

#### HOLD-BACK 縮小 (`_shrink_hold_back`)

```
新 HOLD-BACK = max(0.75s, 元の値 × 0.6)
```

FFmpeg のデフォルトは `segment_time × 3` 程度。縮小することでプレイヤーがよりライブエッジ近くで再生する。

---

## 7. API 仕様

### GET /health

```json
{"status": "ok"}
```

### GET /

ポータルサイト (HTML)。HLS.js プレイヤー搭載。4秒ごとに自動更新。

### GET /api/streams

```json
{
  "streams": [
    {
      "key":     "stream",
      "active":  true,
      "hls_url": "/hls/live/stream/master.m3u8"
    }
  ]
}
```

### GET /api/ngrok

`NGROK_RTMP_API` をクエリし RTMP 公開 URL を返す。`site` は `SITE_BASE_URL` 環境変数の値。

```json
{
  "site": "https://stream.example.com",
  "rtmp": "rtmp://x.tcp.ngrok.io:PORT/live"
}
```

未起動時は `rtmp` が空文字 `""`。`NGROK_CACHE_TTL` 秒間キャッシュ。

### GET /hls/{path}

HLS ファイル配信。

| パス | 内容 |
|------|------|
| `/hls/live/{key}/master.m3u8` | ABR マスタープレイリスト (high/low 両バリアント列挙) |
| `/hls/live/{key}/high/index.m3u8` | high メディアプレイリスト (EXT-X-PART 付き、パッチ済み) |
| `/hls/live/{key}/high/init_high.mp4` | high fmp4 初期化セグメント |
| `/hls/live/{key}/high/seg*.m4s` | high セグメント / パーツ |
| `/hls/live/{key}/low/index.m3u8` | low メディアプレイリスト (720p、パッチ済み) |
| `/hls/live/{key}/low/init_low.mp4` | low fmp4 初期化セグメント |
| `/hls/live/{key}/low/seg*.m4s` | low セグメント / パーツ |

**LL-HLS ブロッキングクエリパラメータ:**

| パラメータ | 説明 |
|---|---|
| `_HLS_msn` | 待機するメディアシーケンス番号 |
| `_HLS_part` | 待機するパーツ番号 |

**全レスポンス共通ヘッダー:**

```
Cache-Control: no-cache, no-store, must-revalidate
Access-Control-Allow-Origin: *
```

---

## 8. 環境変数リファレンス

### ngrok / サイト公開関連

| 変数 | デフォルト | 参照元 |
|------|-----------|--------|
| `SITE_BASE_URL` | *(空文字)* | api-server 環境変数 (Cloudflare 公開ドメイン) |
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok-rtmp コンテナ |
| `NGROK_RTMP_TARGET` | `localhost:1935` | ngrok-rtmp の `command` |
| `NGROK_RTMP_API` | `http://localhost:4040` | api-server 環境変数 |
| `NGROK_CACHE_TTL` | `30` | api-server 環境変数 |

### HLS チューニング

| `HLS_SEGMENT_TIME` | `HLS_PART_DURATION` | PC 遅延 | Android 遅延 | 負荷 |
|-------------------|-------------------|---------|-------------|------|
| `0.5` | `0.1` | ~0.5〜1s | ~1.5s | 高 |
| `1.0` | `0.2` | ~1〜2s | ~3s | 中 |
| `2.0` | `0.5` | ~2〜4s | ~6s | 低 |

### GOP と segment_time の関係

セグメントはキーフレームでのみ切断される。OBS のキーフレーム間隔を `HLS_SEGMENT_TIME` の倍数 (例: 1s) に設定することを推奨。

---

## 9. コンテナ依存関係

```
media-server ──[healthcheck: nc -z :1935]──▶ api-server
                                               │
                                               └──[media-server healthy]──▶ ngrok-rtmp
```

Docker Volume `hls-data`:
- `media-server`: read/write
- `api-server`: read-only

全コンテナは `network_mode: host` のためコンテナ間通信に `localhost` を使用する。

---

## 10. ngrok / Cloudflare 構成

### 設計思想

設定ファイルを持たず、すべてのパラメータを環境変数 + docker-compose の `command` / `environment` で制御する。これにより Portainer の Environment Variables 欄だけでデプロイ・設定変更が完結する。

### 役割分担

| 役割 | 実装 | 管理場所 |
|------|------|---------|
| サイト / HLS 公開 (HTTPS) | Cloudflare Tunnel | **別スタック** |
| RTMP 公開 (TCP) | ngrok-rtmp | このリポジトリ |

### ngrok-rtmp 構成

```yaml
ngrok-rtmp:
  network_mode: host
  command: tcp --log=stdout ${NGROK_RTMP_TARGET:-localhost:1935}
  environment:
    - NGROK_AUTHTOKEN=${NGROK_AUTHTOKEN}
```

`network_mode: host` により `localhost:1935` で直接 RTMP に到達できる。  
管理 UI は `localhost:4040` でアクセス可能。

### ngrok RTMP URL の取得フロー

```python
# api-server/main.py
rtmp_tunnels = await _fetch_tunnels(NGROK_RTMP_API)  # http://localhost:4040/api/tunnels
result = {"site": SITE_BASE_URL, "rtmp": ""}
```

`SITE_BASE_URL` は環境変数から直接取得 (Cloudflare Tunnel のドメインは固定のためクエリ不要)。

### キャッシュ制御

ngrok RTMP URL は `NGROK_CACHE_TTL`(デフォルト 30s) 間キャッシュされる。  
リスタート後に URL が変わった場合も最大 30 秒以内に更新される。

### ngrok 無料プランの制限

| 制限 | 内容 | 対策 |
|------|------|------|
| URL 変動 | RTMP URL がリスタートごとに変更 | ポータルから最新 URL を確認・OBS 更新 |
| 同時トンネル | TCP 1 トンネル/アカウント | LAN 内配信なら `ngrok-rtmp` を削除 |
| 帯域 | 月 1GB | 有料プランへアップグレード |
| 固定 URL | 有料のみ | `--remote-addr=x.tcp.ngrok.io:PORT` (有料プラン) |

---

*最終更新: 2026-06-06*
