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
| 目標遅延 | **0.5〜1秒** (Cloudflare 経由、PC LL-HLS) |
| 配信プロトコル | fmp4 LL-HLS ABR VBR (high/low 2バリアント、PC / Android 共通 URL) |
| 入力プロトコル | RTMP (OBS Studio 標準出力) |
| RTMP サーバー | mediamtx (Go 製、`runOnPublish` フックで FFmpeg を起動) |
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
| ビルド方式 | マルチステージ: `bluenviron/mediamtx:latest` からバイナリをコピー |
| プロセス | mediamtx + publish.sh (FFmpeg サブプロセス) |
| 役割 | RTMP 受信、FFmpeg 起動管理、2バリアント HLS 生成 |
| GPU アクセス | `deploy.resources.reservations.devices` (NVIDIA) |
| 公開ポート | `1935/tcp` (RTMP) |
| 書き込みパス | `/hls/live/high/` および `/hls/live/low/` |

**ABR VBR ストリームの構造:**

```
OBS (publisher)
    │ rtmp://host:1935/live  (ストリームキーは任意)
    ▼
mediamtx
    │ runOnPublish → publish.sh  (MTX_PATH="live/anything" → STREAM_NAME="live")
    │
    └── FFmpeg (filter_complex split)
         ├── /hls/live/master.m3u8   ← ABR マスター (high/low 両バリアント列挙)
         ├── /hls/live/high/         元解像度 VBR avg VIDEO_BITRATE
         │    ├── index.m3u8  (EXT-X-PART 付き LL-HLS)
         │    ├── init_high.mp4
         │    └── seg*.m4s
         └── /hls/live/low/          720p VBR avg VIDEO_BITRATE_LOW
              ├── index.m3u8
              ├── init_low.mp4
              └── seg*.m4s
```

**mediamtx → publish.sh の連携:**

mediamtx は `runOnPublish` フックを環境変数で呼び出す。  
`exec_push` の引数渡し問題を回避し、ストリーム名を確実に渡せる。

```
MTX_PATH="live/kawasaki"  (OBS のアプリ名/ストリームキー)
            ↓ publish.sh: STREAM_NAME="${MTX_PATH%%/*}"
STREAM_NAME="live"        (先頭コンポーネント = RTMP アプリ名)
```

OBS のストリームキーはパスに影響しない。RTMP アプリ名 (`/live`) のみが使われる。

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
| 役割 | HLS 配信・プレイリストパッチ、ポータルサイト、ngrok URL 取得、セッション管理 |
| ホストポート | `8080/tcp` (`network_mode: host` により直接公開) |
| 読み取りパス | `/hls/` (read-only マウント) |
| ngrok 取得 | `httpx` で `NGROK_RTMP_API` の `/api/tunnels` をクエリ |
| サイト URL | `SITE_BASE_URL` 環境変数から取得 |
| リアルタイム通知 | WebSocket `/ws` — ngrok URL 変更・セッション数変化をプッシュ |

### 2.3 ngrok-rtmp

| 項目 | 内容 |
|------|------|
| イメージ | `ngrok/ngrok:latest` |
| コマンド | `tcp --log=stdout ${NGROK_RTMP_TARGET:-127.0.0.1:1935}` |
| 役割 | RTMP ポートを TCP トンネルで公開 (LAN 外配信時) |
| 管理 UI | `localhost:4040` (host network) |
| 備考 | LAN 内配信のみなら削除可。未起動時はポータルにローカル IP を表示 |

### 2.4 Cloudflare Tunnel (別スタック)

このリポジトリには含まれない。別スタックで `cloudflared` を起動し、`localhost:8080` を公開する。  
公開ドメインを `SITE_BASE_URL` に設定するとポータルの URL 表示に反映される。

---

## 3. データフロー

```
OBS
 │ RTMP :1935  (app="live", streamkey=任意)
 ▼
mediamtx (media-server)
 └── runOnPublish → publish.sh (MTX_PATH="live/xxx" → STREAM_NAME="live")
      └── FFmpeg (h264_nvenc → ABR fmp4 LL-HLS)
           ├─ /hls/live/master.m3u8      ← ABR マスター (high/low 列挙)
           ├─ /hls/live/high/
           │    ├── index.m3u8           ← EXT-X-PART 付き
           │    ├── init_high.mp4
           │    └── seg*.m4s
           └─ /hls/live/low/
                ├── index.m3u8
                ├── init_low.mp4
                └── seg*.m4s

      ↓ Docker Volume 共有 (read-only)

FastAPI (api-server :8080)
 │ GET /hls/live/master.m3u8
 │   → ファイルをそのまま返す
 │ GET /hls/live/high/index.m3u8 [?_HLS_msn=N&_HLS_part=P]
 │   → _patch_playlist() でパッチ後に返す
 │     ① EXT-X-PROGRAM-DATE-TIME 注入 (遅延蓄積防止)
 │     ② HOLD-BACK 縮小 (ライブエッジ追従促進)
 │   → LL-HLS ブロッキングリクエストは watchfiles で index.m3u8 更新まで待機
 │
 │ GET /api/ngrok  → ngrok RTMP URL + サイト URL + ローカル IP を返す
 │ WS  /ws        → ngrok URL 変化・セッション数変化をリアルタイムプッシュ
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

| 区間 | LL-HLS (PC/Android 共通) |
|------|--------------------------|
| OBS キャプチャ + RTMP | ~20ms |
| NVENC エンコード | ~30ms |
| HLS パーツ書き込み | ~100ms (0.1s パーツ) |
| FastAPI 配信 | ~5ms |
| Cloudflare RTT | ~30〜100ms |
| AVPro バッファ | ~300〜500ms |
| **合計 (Cloudflare 経由)** | **~0.5〜0.8s** |

*ローカルネットワーク視聴は ~0.15〜0.3s。*

### 4.2 遅延最小化のための設計判断

| 判断 | 理由 |
|------|------|
| fmp4 LL-HLS ABR を PC/Android 共通採用 | Apple HLS Authoring Spec 準拠で最短遅延。Android AVPro も後方互換 |
| LL-HLS ブロッキングリクエスト対応 | クライアントポーリング遅延をゼロに |
| `temp_file` フラグ | アトミック書き込みで途中読みを防止 |
| `Cache-Control: no-cache, no-store` | CDN / ngrok エッジキャッシュを完全回避 |
| HOLD-BACK 60% 削減 | プレイヤーのライブエッジ追従距離を縮小 |

---

## 5. LL-HLS 実装仕様

### 5.1 HLS ファイル構成

```
/hls/live/
├── master.m3u8          ABR マスタープレイリスト
├── high/
│   ├── index.m3u8       EXT-X-PART 付き LL-HLS メディアプレイリスト
│   ├── init_high.mp4    fmp4 初期化セグメント
│   └── seg*.m4s         セグメント / パーツ
└── low/
    ├── index.m3u8
    ├── init_low.mp4
    └── seg*.m4s
```

### 5.2 プレイリスト形式 (index.m3u8)

```m3u8
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.300  ← Python が 0.180 に縮小
#EXT-X-PART-INF:PART-TARGET=0.100000
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-MAP:URI="init_high.mp4"
#EXT-X-PROGRAM-DATE-TIME:2024-01-15T10:30:00.000Z               ← Python が注入
#EXTINF:0.500000,
seg00042.m4s
#EXT-X-PART:DURATION=0.100000,URI="seg00043.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.100000,URI="seg00043.1.m4s"
...
```

### 5.3 ブロッキングリクエスト

LL-HLS 対応クライアントが送るリクエスト例:
```
GET /hls/live/high/index.m3u8?_HLS_msn=44&_HLS_part=2
```

**実装フロー:**
```
Client → FastAPI (_block_until_ready)
              │ asyncio.Event で待機 (タイムアウト 5s)
              │
watchfiles → _notify_waiters ← inotify (index.m3u8 更新を検知)
              │
              └→ Event.set() → FastAPI が応答
```

---

## 6. タイムスタンプ同期・遅延蓄積対策

### 6.1 FFmpeg 側対策 (publish.sh)

| フラグ | 効果 |
|--------|------|
| `-fflags +genpts` | PTS 欠損時に自動生成 |
| `-use_wallclock_as_timestamps 1` | タイムスタンプを OS 壁時計に固定。OBS クロックドリフトをリセット |
| `-af aresample=async=1000` | 最大 1000 サンプル/秒で音声 TS を映像に同期 |
| `-hls_flags +program_date_time` | FFmpeg が `EXT-X-PROGRAM-DATE-TIME` を自動付与 |

### 6.2 Python 自動パッチ処理 (main.py: `_patch_playlist`)

すべての `.m3u8` レスポンスに適用される。

#### EXT-X-PROGRAM-DATE-TIME 注入

FFmpeg が生成しなかった場合のフォールバック:
```
推定時刻 = 現在時刻 - Σ(#EXTINF の時間)
```

#### HOLD-BACK 縮小

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

ポータルサイト (HTML)。ストリーマー向け管理画面。

- RTMP URL をリアルタイム表示 (WebSocket / REST ポーリングフォールバック)
- ストリームキー欄に `live` と入力すると視聴 URL を生成

### WebSocket /ws

ngrok URL 変化・セッション数変化をブラウザにリアルタイムプッシュ。

```json
{"site": "https://...", "rtmp": "rtmp://x.tcp.ngrok.io:PORT/live", "rtmp_local": "rtmp://192.168.x.x:1935/live"}
{"sessions": {"active": 3, "max": 100}}
{"ping": true}
```

### GET /api/ngrok

WebSocket フォールバック用 REST エンドポイント。

```json
{
  "site":       "https://stream.example.com",
  "rtmp":       "rtmp://x.tcp.ngrok.io:PORT/live",
  "rtmp_local": "rtmp://192.168.x.x:1935/live"
}
```

`rtmp` は ngrok 未起動時に空文字 `""`。`NGROK_CACHE_TTL` 秒間キャッシュ。

### GET /api/stream/{key}

指定キーのストリーム状態を返す。

```json
{
  "key":     "live",
  "active":  true,
  "hls_url": "/hls/live/master.m3u8"
}
```

`active` は `/hls/{key}/high/index.m3u8` の存在で判定。

### GET /hls/{path}

HLS ファイル配信。

| パス | 内容 |
|------|------|
| `/hls/live/master.m3u8` | ABR マスタープレイリスト (high/low 両バリアント列挙) |
| `/hls/live/high/index.m3u8` | high メディアプレイリスト (EXT-X-PART 付き、パッチ済み) |
| `/hls/live/high/init_high.mp4` | high fmp4 初期化セグメント |
| `/hls/live/high/seg*.m4s` | high セグメント / パーツ |
| `/hls/live/low/index.m3u8` | low メディアプレイリスト (720p、パッチ済み) |
| `/hls/live/low/init_low.mp4` | low fmp4 初期化セグメント |
| `/hls/live/low/seg*.m4s` | low セグメント / パーツ |

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

### GET /watch/{key}

視聴者向け HLS.js プレイヤーページ。`key` = `live` が標準。

---

## 8. 環境変数リファレンス

### ngrok / サイト公開関連

| 変数 | デフォルト | 参照元 |
|------|-----------|--------|
| `SITE_BASE_URL` | *(空文字)* | api-server (Cloudflare 公開ドメイン) |
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok-rtmp コンテナ |
| `NGROK_RTMP_TARGET` | `127.0.0.1:1935` | ngrok-rtmp の `command` |
| `NGROK_RTMP_API` | `http://127.0.0.1:4040` | api-server |
| `NGROK_CACHE_TTL` | `30` | api-server |

### HLS チューニング

| `HLS_SEGMENT_TIME` | `HLS_PART_DURATION` | 遅延目安 | 負荷 |
|-------------------|-------------------|---------|------|
| `0.5` | `0.1` | ~0.5〜1s | 高 |
| `1.0` | `0.2` | ~1〜2s | 中 |
| `2.0` | `0.5` | ~2〜4s | 低 |

### セッション管理

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAX_SESSIONS` | `100` | 同時視聴上限 |
| `SESSION_TIMEOUT` | `8.0` | セッション自動消滅秒数 (LL-HLS ブロッキングタイムアウト 5s より長く設定) |
| `QUEUE_TIMEOUT` | `20.0` | キュー待機タイムアウト秒数 |
| `MAX_BPS` | `1375000` | セッションあたり最大受信バイト/秒 (11 Mbps 相当) |

### GOP と segment_time の関係

セグメントはキーフレームでのみ切断される。OBS のキーフレーム間隔を `HLS_SEGMENT_TIME` の倍数 (例: 1s) に設定することを推奨。

---

## 9. コンテナ依存関係

```
media-server ──[healthcheck: nc -z 127.0.0.1 1935]──▶ api-server
                                                        │
                                                        └──[media-server healthy]──▶ ngrok-rtmp
```

Docker Volume `hls-data`:
- `media-server`: read/write
- `api-server`: read-only

全コンテナは `network_mode: host` のためコンテナ間通信に `localhost` / `127.0.0.1` を使用する。

---

## 10. ngrok 構成

### 役割分担

| 役割 | 実装 | 管理場所 |
|------|------|---------|
| サイト / HLS 公開 (HTTPS) | Cloudflare Tunnel | **別スタック** |
| RTMP 公開 (TCP) | ngrok-rtmp | このリポジトリ |

### ngrok RTMP URL の取得フロー

```python
# api-server/main.py
tunnels = await _fetch_tunnels("http://127.0.0.1:4040/api/tunnels")
result = {
    "site":       SITE_BASE_URL,
    "rtmp":       "rtmp://{ngrok_host}/live",   # ngrok 起動中のみ
    "rtmp_local": "rtmp://{local_ip}:1935/live", # 常に設定
}
```

### ngrok 無料プランの制限

| 制限 | 内容 | 対策 |
|------|------|------|
| URL 変動 | RTMP URL がリスタートごとに変更 | ポータルの WebSocket でリアルタイム更新 |
| 同時トンネル | TCP 1 トンネル/アカウント | LAN 内配信なら `ngrok-rtmp` を削除 |
| 帯域 | 月 1GB | 有料プランへアップグレード |

---

*最終更新: 2026-06-06*
