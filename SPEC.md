# SYCS Stream Server — 技術仕様書

## 目次

1. [システム概要](#1-システム概要)
2. [コンポーネント構成](#2-コンポーネント構成)
3. [データフロー](#3-データフロー)
4. [遅延設計](#4-遅延設計)
5. [LL-HLS 実装仕様](#5-ll-hls-実装仕様)
6. [タイムスタンプ同期・遅延蓄積対策](#6-タイムスタンプ同期遅延蓄積対策)
7. [API 仕様](#7-api-仕様)
8. [設定パラメータ詳細](#8-設定パラメータ詳細)
9. [コンテナ間依存関係](#9-コンテナ間依存関係)
10. [Cloudflare Tunnel 構成](#10-cloudflare-tunnel-構成)

---

## 1. システム概要

| 項目 | 内容 |
|------|------|
| 目的 | OBS → VRChat 向けライブ映像をリアルタイム配信 |
| 目標遅延 | Cloudflare Tunnel 経由で **1〜1.5秒** / ローカルネットワーク **0.5秒未満** |
| 配信プロトコル | Low Latency HLS (LL-HLS / Apple HLS Authoring Spec 準拠) |
| 入力プロトコル | RTMP (OBS Studio 標準出力) |
| エンコーダ | NVIDIA NVENC `h264_nvenc` (GTX1650)、libx264 自動フォールバック付き |
| 公開方式 | Cloudflare Tunnel (HTTPS / HTTP/1.1) |
| ランタイム | Docker Compose v2 + NVIDIA Container Toolkit |

---

## 2. コンポーネント構成

### 2.1 media-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `nvidia/cuda:12.3.1-runtime-ubuntu22.04` |
| プロセス | nginx (nginx-rtmp モジュール組み込み) |
| 役割 | RTMP 受信、FFmpeg 起動管理、LL-HLS ファイル生成 |
| GPU アクセス | Docker `deploy.resources.reservations.devices` (NVIDIA) |
| 公開ポート | `1935/tcp` (RTMP ingest) |
| 書き込みパス | `/hls/live/<stream_key>/` (Docker Volume) |

**nginx-rtmp の動作:**

OBS から `rtmp://host:1935/live/<stream_key>` で接続があると、nginx-rtmp の `exec_push` ディレクティブが `publish.sh <stream_key>` を実行する。  
ストリーム終了時に FFmpeg プロセスに SIGTERM が送られる (`exec_kill_signal term`)。

**FFmpeg コマンド (publish.sh):**

```
ffmpeg
  [入力]
    -fflags +genpts                   # PTS 欠損時に自動生成
    -use_wallclock_as_timestamps 1    # タイムスタンプ基準を壁時計に固定
    -i rtmp://localhost:1935/live/<key>

  [映像 NVENC]
    -c:v h264_nvenc
    -preset:v llhq                    # Low Latency High Quality
    -tune:v ll                        # Low Latency チューニング
    -rc:v cbr                         # 固定ビットレート
    -b:v / -maxrate:v / -bufsize:v    # ビットレート上限制御
    -g 60 -keyint_min 60              # GOP 固定 (2秒@30fps)
    -sc_threshold 0                   # シーンチェンジ検出無効

  [音声]
    -c:a aac -ar 44100
    -af aresample=async=1000          # 音声ドリフト自動補正 (1000sample/s)

  [HLS 出力]
    -f hls
    -hls_time 0.5                     # セグメント長 (秒)
    -hls_list_size 6                  # プレイリスト保持数
    -hls_flags delete_segments        # 古いセグメントを自動削除
              +split_by_time          # 時間ベースで分割
              +low_latency            # LL-HLS 有効 (EXT-X-PART 生成)
              +temp_file              # アトミック書き込み (tmp→rename)
              +program_date_time      # EXT-X-PROGRAM-DATE-TIME 付与
    -hls_segment_type fmp4            # Fragmented MP4 (LL-HLS 必須)
    -hls_fmp4_init_filename init.mp4  # 初期化セグメント
    -hls_part_duration 0.1            # パーツ長 (秒)
    -master_pl_name master.m3u8       # マスタープレイリスト生成
```

### 2.2 api-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `python:3.12-slim` |
| フレームワーク | FastAPI + uvicorn |
| 役割 | HLS ファイル HTTP 配信、playlist パッチ処理、ステータス UI |
| 公開ポート | `8080/tcp` |
| 読み取りパス | `/hls/` (Docker Volume、read-only マウント) |
| ファイル監視 | `watchfiles` による非同期 inotify 監視 |

### 2.3 cloudflared

| 項目 | 内容 |
|------|------|
| イメージ | `cloudflare/cloudflared:latest` |
| 役割 | api-server の HTTP をインターネットへ HTTPS で公開 |
| 設定ファイル | `./cloudflared/config.yml` (Git 管理外) |
| 依存 | `api-server` ヘルスチェック通過後に起動 |

---

## 3. データフロー

```
OBS
 │ RTMP/TCP :1935
 ▼
nginx-rtmp (media-server)
 │ exec_push
 ▼
FFmpeg (media-server 内、GPU アクセス)
 │ h264_nvenc encode
 │ LL-HLS fmp4 segments
 ▼ 書き込み
/hls/live/<key>/
  ├── init.mp4         ← 初期化セグメント (fmp4 ヘッダー)
  ├── master.m3u8      ← マスタープレイリスト
  ├── index.m3u8       ← メディアプレイリスト (EXT-X-PART 付き)
  ├── seg00001.m4s     ← 完成セグメント (0.5s)
  ├── seg00002.0.m4s   ← パーツ 0 (0.1s)
  ├── seg00002.1.m4s   ← パーツ 1 (0.1s)
  └── ...
 │ Docker Volume 共有
 ▼
FastAPI (api-server)
 │ GET /hls/live/<key>/master.m3u8
 │ playlist パッチ処理 (_patch_playlist)
 │   ・EXT-X-PROGRAM-DATE-TIME 注入
 │   ・HOLD-BACK 縮小
 │ Cache-Control: no-cache, no-store
 ▼
cloudflared
 │ HTTPS
 ▼
VRChat / ブラウザ (AVPro / HLS.js)
```

---

## 4. 遅延設計

### 4.1 遅延内訳 (期待値)

| 区間 | 遅延 | 備考 |
|------|------|------|
| OBS キャプチャ | ~16ms | 60fps 時の 1 フレーム |
| RTMP 送信 (LAN) | ~5ms | ローカルネットワーク |
| NVENC エンコード | ~20〜40ms | GTX1650 / llhq プリセット |
| LL-HLS パーツ書き込み | ~100ms | `HLS_PART_DURATION=0.1` |
| FastAPI 配信 | ~5ms | ローカル |
| **ローカル合計** | **~150〜170ms** | プレイヤーバッファ除く |
| Cloudflare RTT | +50〜200ms | エッジサーバーの距離依存 |
| AVPro バッファ (VRChat) | ~300〜500ms | プレイヤー実装依存 |
| **VRChat 実測期待値** | **~0.6〜1.5s** | Cloudflare 経由 |

### 4.2 遅延最小化のための設計判断

| 判断 | 内容 |
|------|------|
| LL-HLS 採用 | 通常 HLS (3〜30s) に対し 1s 未満を実現 |
| fmp4 セグメント | TS より小さいオーバーヘッド、LL-HLS 必須フォーマット |
| HOLD-BACK 縮小 | FFmpeg デフォルト値の 60% に削減 (最低 0.75s) |
| `temp_file` フラグ | ファイル書き込みをアトミックにし、途中読みを防ぐ |
| `Cache-Control: no-store` | Cloudflare によるセグメントキャッシュを防止 |
| LL-HLS ブロッキングリクエスト | クライアントのポーリング間隔を排除し、パーツ生成直後に配信 |

---

## 5. LL-HLS 実装仕様

### 5.1 プレイリスト形式

FFmpeg + パッチ処理後の `index.m3u8` サンプル:

```m3u8
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.300,CAN-SKIP-UNTIL=12.0
#EXT-X-PART-INF:PART-TARGET=0.100000
#EXT-X-TARGETDURATION:1
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-MAP:URI="init.mp4"

#EXT-X-PROGRAM-DATE-TIME:2024-01-15T10:30:00.000Z   ← Python が注入
#EXTINF:0.500000,
seg00042.m4s

#EXT-X-PART:DURATION=0.100000,URI="seg00043.0.m4s",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.100000,URI="seg00043.1.m4s"
#EXT-X-PART:DURATION=0.100000,URI="seg00043.2.m4s"
#EXTINF:0.500000,
seg00043.m4s

#EXT-X-PART:DURATION=0.100000,URI="seg00044.0.m4s",INDEPENDENT=YES  ← 現在生成中
```

### 5.2 ブロッキングリクエスト対応

LL-HLS 対応クライアントは次のようなリクエストを送る:

```
GET /hls/live/stream/index.m3u8?_HLS_msn=44&_HLS_part=2
```

「セグメント 44 のパーツ 2 が完成するまで応答を保留せよ」という指示。  
これにより 0.1s パーツが完成した瞬間にクライアントへ配信され、ポーリング遅延がゼロになる。

**実装:**

```
クライアント          FastAPI              watchfiles
    │                   │                     │
    │ GET ?_HLS_msn=44  │                     │
    │──────────────────▶│                     │
    │                   │ asyncio.Event 待機   │
    │                   │◀────────────────────│ inotify: index.m3u8 更新
    │                   │ Event.set()         │
    │ playlist (patched)│                     │
    │◀──────────────────│                     │
```

タイムアウト: 5秒 (Cloudflare Tunnel のデフォルトタイムアウト 100s 以内)

---

## 6. タイムスタンプ同期・遅延蓄積対策

### 6.1 問題

| 問題 | 原因 |
|------|------|
| A/V ずれ | OBS の音声/映像クロックの微細なドリフト |
| タイムスタンプ不連続 | RTMP 再接続時の PTS リセット |
| 遅延の蓄積 | プレイヤーがライブエッジからの遅れを検知できない |

### 6.2 FFmpeg 側の対策 (publish.sh)

| フラグ | 効果 |
|--------|------|
| `-fflags +genpts` | PTS が欠損・不整合な場合に自動生成 |
| `-use_wallclock_as_timestamps 1` | タイムスタンプ基準を OS の壁時計に固定。OBS クロックのドリフトをリセット |
| `-af aresample=async=1000` | 最大 1000 サンプル/秒の速度で音声タイムスタンプを映像に同期 |
| `-hls_flags program_date_time` | 各セグメントに実時刻 (`EXT-X-PROGRAM-DATE-TIME`) を埋め込み |

### 6.3 Python 側の自動パッチ処理 (main.py: `_patch_playlist`)

`.m3u8` を返すたびに毎回実行される。

#### EXT-X-PROGRAM-DATE-TIME 注入 (`_estimate_first_segment_pdt`)

FFmpeg が `-hls_flags program_date_time` で生成しなかった場合のフォールバック。

```
推定時刻 = 現在時刻 - Σ(#EXTINF 時間)
```

例: 現在 10:30:03.0、プレイリストに 0.5s × 6 = 3.0s 分のセグメントがある場合  
→ 先頭セグメント開始時刻 = 10:30:00.0

AVPro など `EXT-X-PROGRAM-DATE-TIME` に対応したプレイヤーは、この値と現在時刻を比較して自分の遅れを検知し、ライブエッジへ自動追従する。

#### HOLD-BACK 縮小 (`_shrink_hold_back`)

```
新しい HOLD-BACK = max(0.75s, 元の値 × 0.6)
```

`EXT-X-SERVER-CONTROL` の `HOLD-BACK` はプレイヤーがライブエッジから何秒遅れて再生するかのターゲット値。  
デフォルト (FFmpeg 生成値) は `HLS_SEGMENT_TIME × 3` 程度になることが多く、縮小することで追従余裕を削減する。

---

## 7. API 仕様

### GET /health

ヘルスチェック。Docker healthcheck から使用。

**レスポンス:**
```json
{"status": "ok"}
```

---

### GET /

ステータス Web UI (HTML)。配信中ストリームと VRChat 用 URL を表示。  
3秒ごとに `/api/streams` をポーリングして自動更新。

---

### GET /api/streams

配信中ストリームの一覧を返す。

**レスポンス:**
```json
{
  "streams": [
    {
      "key": "stream",
      "active": true,
      "hls_url": "/hls/live/stream/master.m3u8"
    }
  ]
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| `key` | string | ストリームキー (OBS のストリームキーと一致) |
| `active` | boolean | `index.m3u8` が存在するか |
| `hls_url` | string | master.m3u8 へのパス |

---

### GET /hls/{path}

HLS ファイル配信。

**パスパターン:**

| パス | 説明 |
|------|------|
| `/hls/live/<key>/master.m3u8` | マスタープレイリスト (VRChat 推奨 URL) |
| `/hls/live/<key>/index.m3u8` | メディアプレイリスト (パッチ処理済み) |
| `/hls/live/<key>/init.mp4` | fmp4 初期化セグメント |
| `/hls/live/<key>/seg<N>.m4s` | 完成セグメント |
| `/hls/live/<key>/seg<N>.<P>.m4s` | LL-HLS パーツ |

**クエリパラメータ (LL-HLS ブロッキング):**

| パラメータ | 型 | 説明 |
|---|---|---|
| `_HLS_msn` | integer | 待機するメディアシーケンス番号 |
| `_HLS_part` | integer | 待機するパーツ番号 |

**レスポンスヘッダー (全ファイル共通):**

```
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
Access-Control-Allow-Origin: *
```

**Content-Type:**

| 拡張子 | Content-Type |
|--------|-------------|
| `.m3u8` | `application/vnd.apple.mpegurl` |
| `.m4s` | `video/iso.segment` |
| `.mp4` | `video/mp4` |
| `.ts` | `video/MP2T` |

---

## 8. 設定パラメータ詳細

### HLS チューニングの指針

| 用途 | `HLS_SEGMENT_TIME` | `HLS_PART_DURATION` | 期待遅延 | 負荷 |
|------|-------------------|-------------------|---------|------|
| 超低遅延 (推奨) | 0.5 | 0.1 | ~0.5〜1s | 高 |
| バランス | 1.0 | 0.2 | ~1〜2s | 中 |
| 安定重視 | 2.0 | 0.5 | ~2〜4s | 低 |

### GOP 設定と `HLS_SEGMENT_TIME` の関係

セグメントはキーフレームでのみ切れる。`-g 60`（30fps で 2秒ごと）の場合、`HLS_SEGMENT_TIME=0.5` でも実際のセグメントは 0.5s の倍数になる。  
→ OBS のキーフレーム間隔も 0.5s の倍数 (例: 1s または 2s) に設定することを推奨。

### ビットレートとバッファの関係

```
-bufsize:v = VIDEO_BITRATE
```

バッファをビットレートと等しくすることで 1 秒相当のバッファを確保。  
GTX1650 の NVENC 最大ビットレートは H.264 で約 140Mbps だが、実用上は 8000k 程度が安定。

---

## 9. コンテナ間依存関係

```
media-server
    │ healthcheck: nc -z localhost 1935
    │ (RTMP ポートが開いたら healthy)
    ▼
api-server
    │ healthcheck: curl http://localhost:8080/health
    │ (FastAPI が応答したら healthy)
    ▼
cloudflared
    │ (api-server が healthy になってから起動)
```

Docker Volume `hls-data`:
- `media-server`: read/write
- `api-server`: read-only

---

## 10. Cloudflare Tunnel 構成

### キャッシュ制御

HLS セグメントが Cloudflare のエッジキャッシュに乗ると古いセグメントが返り、ストリームが止まる。  
FastAPI が全レスポンスに `Cache-Control: no-cache, no-store` を付与しているため、デフォルトでは Cloudflare はキャッシュしない。

念のため Cloudflare ダッシュボードで **Cache Rules** を設定することを推奨:

```
If URI path matches /hls/*
→ Cache Status: Bypass
```

### タイムアウト設定

| 項目 | 設定値 | 理由 |
|------|--------|------|
| Cloudflare origin タイムアウト | デフォルト 100s | LL-HLS ブロッキングリクエストは最大 5s で応答するため問題なし |
| `keepAliveTimeout` (config.yml) | 90s | keep-alive 接続を維持してオーバーヘッド削減 |

### WebSocket の非使用

LL-HLS はシンプルな HTTP GET のみで動作するため、WebSocket は不要。  
Cloudflare Tunnel の HTTP/1.1 対応のみで十分。

---

*最終更新: 2026-06-06*
