# SYCS Stream Server — 技術仕様書

## 目次

1. [システム概要](#1-システム概要)
2. [コンポーネント仕様](#2-コンポーネント仕様)
3. [データフロー](#3-データフロー)
4. [遅延設計](#4-遅延設計)
5. [LL-HLS 実装仕様](#5-ll-hls-実装仕様) (5.3 タイムスタンプ同期・遅延蓄積対策 を含む)
6. [セキュリティ設計](#6-セキュリティ設計)
7. [API 仕様](#7-api-仕様)
8. [環境変数リファレンス](#8-環境変数リファレンス)
9. [コンテナ依存関係](#9-コンテナ依存関係)
10. [ngrok 構成](#10-ngrok-構成)

---

## 1. システム概要

| 項目 | 内容 |
|------|------|
| 目的 | OBS → VRChat / ブラウザ向けライブ映像のリアルタイム配信 |
| 目標遅延 | **0.3〜0.6s** (ローカル)、**~1s** (Cloudflare 経由) |
| 配信プロトコル | fmp4 LL-HLS ABR (high/low 2バリアント) |
| 入力プロトコル | RTMP (OBS Studio 標準出力) |
| LL-HLS 生成 | **mediamtx ネイティブ** (FFmpeg は使用しない) |
| エンコーダ | NVENC `h264_nvenc` (GTX1650)、libx264 自動フォールバック |
| FFmpeg 役割 | **トランスコードのみ** (RTSP→720p→RTMP、HLS 生成はしない) |
| プレイリスト配信 | ブラウザ向けは **WebSocket プッシュ** (m3u8 URL 非公開) |
| セグメント配信 | **HMAC 署名付き短命 URL** (`/seg/`) |
| サイト公開 | Cloudflare Tunnel (別スタック管理) |
| RTMP 公開 | ngrok TCP Tunnel (ngrok-rtmp コンテナ) |
| ネットワーク | `network_mode: host` — 全コンテナがホスト NW を共有 |
| ランタイム | Docker Compose v2 + NVIDIA Container Toolkit |

---

## 2. コンポーネント仕様

### 2.1 media-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `nvidia/cuda:12.6.3-runtime-ubuntu24.04` |
| ビルド方式 | マルチステージ: `bluenviron/mediamtx:latest` からバイナリをコピー、FFmpeg は `apt install ffmpeg` |
| プロセス | mediamtx + publish.sh (FFmpeg サブプロセス) |
| 役割 | RTMP 受信、mediamtx ネイティブ LL-HLS 生成、FFmpeg 720p トランスコード管理 |
| GPU アクセス | `deploy.resources.reservations.devices` (NVIDIA) |
| 公開ポート | `1935/tcp` (RTMP) |
| 書き込みパス | `/hls/live/{key}/` および `/hls/live/{key}_transcode/` |

**ストリーム構成:**

```
OBS (publisher)
    │ rtmp://host:1935/live/{stream_key}
    ▼
mediamtx
    │ ── LL-HLS ネイティブ生成 ──────────────────────────────────────────
    │     /hls/live/{key}/index.m3u8     (OBS 元解像度、high)
    │     /hls/live/{key}/init.mp4
    │     /hls/live/{key}/seg*.mp4
    │
    └── runOnReady → publish.sh  (flock 排他制御)
         │ MTX_PATH="live/{key}" → STREAM_NAME="{key}"
         │ "_transcode" サフィックスのパスは即 exit 0 (再帰防止)
         └── FFmpeg  (RTSP → 720p VBR → RTMP)
              出力: rtmp://127.0.0.1:1935/live/{key}_transcode
                    ↓ mediamtx が再受信
                    /hls/live/{key}_transcode/index.m3u8  (720p、low)
                    /hls/live/{key}_transcode/init.mp4
                    /hls/live/{key}_transcode/seg*.mp4
```

**mediamtx LL-HLS 設定 (`mediamtx.yml`):**

| 設定 | 値 | 意味 |
|------|----|------|
| `hlsVariant` | `lowLatency` | EXT-X-PART / EXT-X-SERVER-CONTROL 有効 |
| `hlsSegmentDuration` | `1s` | 1 セグメント長 |
| `hlsPartDuration` | `100ms` | 1 パーツ長 (LL-HLS 最小粒度) |
| `hlsSegmentCount` | `7` | プレイリスト保持セグメント数 |
| `hlsDirectory` | `/hls` | ファイル書き出し先 (Docker Volume) |

**publish.sh — FFmpeg トランスコード:**

```bash
INPUT="rtsp://127.0.0.1:8554/live/{key}"
OUTPUT="rtmp://127.0.0.1:1935/live/{key}_transcode"

# NVENC 利用可能時
ffmpeg -rtsp_transport tcp -fflags +genpts -use_wallclock_as_timestamps 1 \
  -i "${INPUT}" \
  -vf "scale=-2:720" \
  -c:v h264_nvenc -rc vbr -preset llhq -tune ll \
  -b:v 2000k -maxrate 2700k -bufsize 5400k \
  -g 60 -keyint_min 60 -sc_threshold 0 \
  -c:a aac -b:a 128k -ar 44100 -af "aresample=async=1000" \
  -f flv "${OUTPUT}"
```

`h264_nvenc` 非対応時は `libx264 -preset ultrafast -tune zerolatency` に自動フォールバック。

### 2.2 api-server

| 項目 | 内容 |
|------|------|
| ベースイメージ | `python:3.12-slim` |
| フレームワーク | FastAPI + uvicorn |
| 役割 | WebSocket プレイリストプッシュ、署名付きセグメント配信、ポータルサイト、セッション管理 |
| ホストポート | `8080/tcp` |
| 読み取りパス | `/hls/` (read-only マウント) |
| 主要依存 | fastapi, uvicorn[standard], watchfiles, aiofiles, httpx |

**WebSocket LL-HLS 配信 (ブラウザ向け):**

```
GET /api/token/{key}                     ← ワンタイムトークン取得 (60s TTL)
WS  /ws/hls/{key}?token=xxx             ← WebSocket 接続
     ↓ 接続直後にプッシュ
     {"type":"master","content":"#EXTM3U..."}
     {"type":"level","variant":"high","content":"#EXTM3U... (署名済み)"}
     {"type":"level","variant":"low", "content":"#EXTM3U... (署名済み)"}
     ↓ mediamtx が index.m3u8 を更新するたびに自動プッシュ (≤100ms)
     {"type":"level","variant":"high","content":"..."}
```

**VRChat 向け通常 HTTP 配信:**

```
GET /hls/live/{key}/master.m3u8         ← 動的生成 (mediamtx は書かない)
GET /hls/live/{key}/index.m3u8          ← LL-HLS ブロッキング対応
GET /hls/live/{key}/*.mp4               ← セグメント (sid 帯域制御付き)
```

### 2.3 ngrok-rtmp

| 項目 | 内容 |
|------|------|
| イメージ | `ngrok/ngrok:latest` |
| 役割 | RTMP ポートを TCP トンネルで外部公開 |
| 管理 UI | `localhost:4040` |

### 2.4 Cloudflare Tunnel (別スタック)

このリポジトリには含まれない。別スタックで `cloudflared` を起動し、`localhost:8080` を公開する。  
公開ドメインを `SITE_BASE_URL` に設定するとポータルの URL 表示に反映される。

---

## 3. データフロー

```
OBS
 │ RTMP :1935  (app="live", streamkey={key})
 ▼
mediamtx (media-server)
 │ LL-HLS ネイティブ書き出し ──────────────────────────────────────────────
 │   /hls/live/{key}/index.m3u8      ← EXT-X-PART 付き、100ms パーツ
 │   /hls/live/{key}/seg*.mp4
 │
 └── runOnReady → publish.sh
      └── FFmpeg (RTSP:8554 → 720p → RTMP:1935/{key}_transcode)
           ↓ mediamtx が再受信
           /hls/live/{key}_transcode/index.m3u8
           /hls/live/{key}_transcode/seg*.mp4

           ↓ Docker Volume (hls-data)

FastAPI (api-server :8080)
 │
 ├─ [ブラウザ視聴ページ /watch/{key}]
 │   GET  /api/token/{key}
 │        → ワンタイムトークン (60s TTL、消費で無効化)
 │   WS   /ws/hls/{key}?token=xxx
 │        → セッション確保
 │        → master コンテンツ生成・プッシュ
 │        → watchfiles で index.m3u8 変更を検知
 │        → プレイリスト読み取り → セグメント URL を HMAC 署名に書き換え → プッシュ
 │   GET  /seg/live/{key}/seg*.mp4?exp=T&sig=S
 │        → 署名検証 (120s TTL) → ファイル配信
 │
 ├─ [VRChat / 直 URL アクセス]
 │   GET  /hls/live/{key}/master.m3u8
 │        → api-server が動的生成 (mediamtx は書かない)
 │        → sid セッション確保、_inject_sid で URL に sid を付与
 │   GET  /hls/live/{key}/index.m3u8?_HLS_msn=N&_HLS_part=P&sid=S
 │        → watchfiles イベント待機 (LL-HLS ブロッキング) → パッチ済みで返す
 │   GET  /hls/live/{key}/*.mp4?sid=S
 │        → sid 帯域チェック → ファイル配信
 │
 └─ [ポータル /]
     WS /ws → ngrok URL・セッション数をリアルタイムプッシュ

Cloudflare Tunnel / ブラウザ / VRChat
```

---

## 4. 遅延設計

### 4.1 遅延内訳

| 区間 | 時間 |
|------|------|
| OBS キャプチャ + RTMP 送出 | ~20ms |
| NVENC エンコード (high, mediamtx 受信) | ~30ms |
| mediamtx LL-HLS パーツ書き込み | **~100ms** (パーツ長) |
| watchfiles 検知 + WS プッシュ | ~1〜5ms |
| Cloudflare RTT | ~30〜100ms |
| hls.js バッファ (`liveSyncDurationCount: 1`) | ~100〜300ms |
| **合計 (ブラウザ、Cloudflare 経由)** | **~300〜550ms** |

*旧アーキテクチャ (HTTP long-poll) 比: パーツ長分 (~100ms) + WS 接続確立遅延除去で約 30〜50% 削減。*

### 4.2 遅延最小化の設計判断

| 判断 | 理由 |
|------|------|
| mediamtx ネイティブ LL-HLS | FFmpeg の `hls_part_duration` オプションは mainline FFmpeg に存在しない |
| WebSocket プッシュ配信 | HTTP long-poll の往復遅延 (≈パーツ長) をゼロにする |
| `hlsPartDuration: 100ms` | 200ms から短縮。push サイクルが倍速 |
| `liveSyncDurationCount: 1` | hls.js の再生バッファをセグメント 1 個分に最小化 |
| HOLD-BACK 60% 削減 | プレイヤーのライブエッジ追従距離を縮小 |
| `Cache-Control: no-cache, no-store` | CDN / ngrok エッジキャッシュを完全回避 |

---

## 5. LL-HLS 実装仕様

### 5.1 HLS ファイル構成 (mediamtx 書き出し)

```
/hls/live/
├── {key}/
│   ├── index.m3u8          EXT-X-PART 付き LL-HLS メディアプレイリスト (high)
│   ├── init.mp4            fmp4 初期化セグメント
│   └── seg*.mp4            セグメント / パーツ
└── {key}_transcode/
    ├── index.m3u8          LL-HLS メディアプレイリスト (low, 720p)
    ├── init.mp4
    └── seg*.mp4
```

master.m3u8 は **mediamtx が生成しない**。api-server が動的生成する。

### 5.2 master.m3u8 動的生成 (api-server)

```m3u8
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,NAME=high
/hls/live/{key}/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME=low
/hls/live/{key}_transcode/index.m3u8
```

### 5.3 タイムスタンプ同期・遅延蓄積対策

| 対策 | 適用箇所 | 内容 |
|------|---------|------|
| `-use_wallclock_as_timestamps 1` | publish.sh (low バリアント) | FFmpeg の PTS を OS 壁時計に固定。トランスコードパスのクロックドリフトをリセット |
| `-fflags +genpts` | publish.sh (low バリアント) | PTS 欠損時に自動生成 |
| `-af aresample=async=1000` | publish.sh (low バリアント) | 最大 1000 サンプル/秒で音声 TS を映像に同期 |
| `-g 60 -keyint_min 60 -sc_threshold 0` | publish.sh (low バリアント) | GOP 固定でセグメント境界を安定させる |
| `EXT-X-PROGRAM-DATE-TIME` 注入 | `_patch_playlist` (全経路) | mediamtx が PDT を書かない場合のフォールバック: `現在時刻 - Σ(EXTINF)` で推定 |
| `PART-HOLD-BACK` 縮小 | `_patch_playlist` (全経路) | `max(0.3s, 元の値 × 0.6)` — Apple 仕様の最小値 (3 × PART-TARGET) を下限とする |
| `liveSyncDurationCount: 1` | hls.js 設定 (WS 経路) | 再生バッファをセグメント 1 個分に最小化してライブエッジに追従 |

**high バリアントの注意点:**  
OBS → mediamtx の直結パスは FFmpeg を通らないため、`-use_wallclock_as_timestamps` が適用されない。  
ただしドリフト蓄積は実用上は問題にならない。理由はプレイヤーの種類で異なる:

| プレイヤー | ドリフト対策 |
|---|---|
| ブラウザ (hls.js) | PDT を参照して壁時計ベースのライブエッジを計算。`liveSyncDurationCount: 1` で追従。能動的に補正する |
| VRChat (AVPro) | PDT は参照しない。ただし mediamtx のセグメントロールオーバー (最大 7 個保持) により古いセグメントが失効し、プレイヤーは自然に最新エッジへ引き寄せられる。OBS クロックの現実的なズレ (<0.01%/h) では問題にならない |

### 5.4 プレイリストパッチ処理 (`_patch_playlist`)

HTTP・WebSocket 両経路に共通適用される。

| 処理 | 内容 |
|------|------|
| `EXT-X-PROGRAM-DATE-TIME` 注入 | `現在時刻 - Σ(EXTINF 秒数)` で推定。遅延蓄積防止 |
| HOLD-BACK 縮小 | `max(0.5s, 元の値 × 0.6)` でライブエッジ追従を促進 |

### 5.4 WebSocket プレイリストプッシュ

```
watchfiles (awatch HLS_DIR)
    │ index.m3u8 変更検知
    ▼
_notify_waiters(rel_path)
    ├─ HTTP long-poll 待機 Event.set()      (VRChat 向け)
    └─ asyncio.Queue.put_nowait(rel_path)   (WS 向け)
         ▼
ws_hls ハンドラー
    1. index.m3u8 読み取り
    2. _patch_playlist()
    3. _sign_playlist_segments()   セグメント URI → /seg/ 署名 URL に書き換え
    4. ws.send_json({"type":"level","variant":"high","content":"..."})
```

### 5.5 クライアント側 LL-HLS ブロッキング (JavaScript)

hls.js の `pLoader` を `WsPlaylistLoader` で差し替え、HTTP ポーリングを WebSocket 待機に変換する。

```javascript
// hls.js が _HLS_msn=N&_HLS_part=P 付きで level を要求
→ WsPlaylistLoader.load(ctx)
    latest = _ws.levels[variant]
    if (_hlsSatisfies(latest, msn, part)) → 即返す
    else → levelWaiters に登録
               ↓
    ws.onmessage: level push受信
    → _deliverLevel(variant, content)
        → waiters の中でsatisfy するものを resolve
```

---

## 6. セキュリティ設計

### 6.1 視聴ページの m3u8 保護

| 脅威 | 対策 |
|------|------|
| DevTools から m3u8 URL をコピーして再生 | ブラウザ視聴ページは m3u8 を HTTP で配信しない。内容は WebSocket でのみ送信 |
| WebSocket token の使い回し | ワンタイム消費 (取得後 60 秒 TTL、接続で即失効) |
| WS URL の共有 | token なし接続は 1008 で即 close |

### 6.2 セグメント URL 署名

```
/seg/live/{key}/seg000001.mp4?exp=1234567890&sig=abcdef0123456789
```

| 要素 | 説明 |
|------|------|
| `exp` | Unix 時刻 (秒)。この時刻を過ぎると 403 |
| `sig` | `HMAC-SHA256(_SEGMENT_SECRET, "{path}:{exp}")` の先頭 16 文字 |
| TTL | デフォルト 120 秒 (`SEGMENT_TTL` 環境変数) |
| シークレット | `SEGMENT_SECRET` 環境変数。未設定時は起動ごとにランダム生成 |

署名されたプレイリストは WebSocket 経由でのみ届く。セグメント URL をコピーしても 120 秒で失効する。

### 6.3 VRChat 向け HTTP (sid セッション管理)

VRChat は WebSocket に未対応のため、従来の HTTP 経由で配信する。

| 機能 | 実装 |
|------|------|
| セッション上限 | `MAX_SESSIONS` (デフォルト 100)。超過新規接続はキュー待機 |
| セッション失効 | 最終リクエストから `SESSION_TIMEOUT` 秒 (デフォルト 15s) |
| 帯域制限 | セッションあたり `MAX_BPS` バイト/秒 超過で 429 → セッション解放 |

---

## 7. API 仕様

### GET /health

```json
{"status": "ok"}
```

---

### GET /api/stream/{key}

指定キーのストリーム状態を返す。

```json
{
  "key":     "kawasaki",
  "active":  true,
  "hls_url": "/hls/live/kawasaki/master.m3u8"
}
```

`active` は `/hls/live/{key}/index.m3u8` の存在で判定。

---

### GET /api/token/{key}

ブラウザ視聴ページが WebSocket 接続前に取得するワンタイムトークン。

**レスポンス:**
```json
{"token": "a3f8...", "ttl": 60}
```

- token はサーバー内に 1 回のみ使用可能で保持される
- TTL 60 秒 (`TOKEN_TTL` 環境変数) で自動失効
- `/ws/hls/{key}?token=xxx` に使用後は再利用不可

---

### WebSocket /ws

ポータル向け。ngrok URL 変化・セッション数をリアルタイムプッシュ。

```json
{"site": "https://...", "rtmp": "rtmp://...", "rtmp_local": "rtmp://192.168.x.x:1935/live", "sessions": {"active": 3, "max": 100}}
{"ping": true}
```

---

### WebSocket /ws/hls/{key}?token={token}

視聴ページ向け。プレイリストをリアルタイムプッシュする。

**認証エラー:** token 不正・期限切れは `1008 Policy Violation` で即 close。

**メッセージ形式 (server → client):**

| type | 内容 |
|------|------|
| `master` | master プレイリスト文字列 (`content`) |
| `level` | バリアント playlist 文字列 (`variant`: `"high"` or `"low"`, `content`) |
| `ping` | キープアライブ (25 秒間無変化時) |

セグメント URI は署名付き `/seg/` URL に書き換えられた状態でプッシュされる。

---

### GET /seg/{path}?exp={exp}&sig={sig}

HMAC 署名を検証してセグメントを配信する。

| 条件 | レスポンス |
|------|-----------|
| 署名正常・有効期限内 | `200 OK` + ファイル本体 |
| 有効期限切れ・署名不正 | `403 Forbidden` |
| ファイル不在 | `404 Not Found` |

---

### GET /hls/{path}

VRChat / 直 URL アクセス向け HTTP HLS 配信。

| パス | 内容 |
|------|------|
| `/hls/live/{key}/master.m3u8` | api-server が動的生成する ABR マスタープレイリスト |
| `/hls/live/{key}/index.m3u8` | LL-HLS メディアプレイリスト (パッチ済み) |
| `/hls/live/{key}/*.mp4` | セグメント / パーツ (sid 帯域制御付き) |

**LL-HLS ブロッキングクエリ:**
```
GET /hls/live/{key}/index.m3u8?_HLS_msn=44&_HLS_part=2&sid=abc123
```

| パラメータ | 説明 |
|---|---|
| `_HLS_msn` | 待機するメディアシーケンス番号 |
| `_HLS_part` | 待機するパーツ番号 |
| `sid` | セッション ID (master.m3u8 取得時に払い出し) |

---

### GET /watch/{key}

視聴者向け hls.js プレイヤーページ。WebSocket でプレイリストを受け取る。

---

### GET /

ポータルサイト (HTML)。RTMP URL のリアルタイム表示、視聴 URL 生成。

---

## 8. 環境変数リファレンス

### サイト公開 / ngrok

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `SITE_BASE_URL` | *(空文字)* | Cloudflare Tunnel の公開ドメイン |
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok 認証トークン |
| `NGROK_RTMP_TARGET` | `127.0.0.1:1935` | ngrok TCP トンネル向き先 |
| `NGROK_RTMP_API` | `http://127.0.0.1:4040` | api-server が参照する ngrok API |
| `NGROK_CACHE_TTL` | `30` | ngrok URL キャッシュ秒数 |

### エンコード (publish.sh)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `VIDEO_BITRATE_LOW` | `2000k` | low バリアント (720p) 映像平均ビットレート |
| `AUDIO_BITRATE` | `128k` | low バリアント音声ビットレート |

> high バリアントは OBS からの入力をそのまま受け取るため、ビットレートは OBS 側で設定する。

### セキュリティ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `SEGMENT_SECRET` | *(起動ごとランダム)* | セグメント URL 署名 HMAC シークレット。固定したい場合は明示設定 |
| `SEGMENT_TTL` | `120` | 署名付きセグメント URL の有効期限 (秒) |
| `TOKEN_TTL` | `60` | ワンタイムトークンの有効期限 (秒) |

> `SEGMENT_SECRET` 未設定時は再起動ごとにランダム生成されるため、古い署名 URL がすべて失効する (再生中断なし、hls.js が新プレイリストを受け取れば復旧)。

### セッション管理

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAX_SESSIONS` | `100` | 同時視聴上限 (超過はキュー待機) |
| `SESSION_TIMEOUT` | `15.0` | 最終リクエストからのセッション消滅秒数 |
| `QUEUE_TIMEOUT` | `20.0` | キュー待機タイムアウト秒数 (超過で 503) |
| `MAX_BPS` | `3000000` | セッションあたり最大受信バイト/秒 (超過で 429) |

### ログ / インフラ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `LOG_LEVEL` | `info` | uvicorn ログレベル |
| `HLS_DIR` | `/hls` | HLS ファイル読み取りルートパス |

---

## 9. コンテナ依存関係

```
media-server ──[healthcheck: nc -z 127.0.0.1 1935]──▶ api-server
                                                        └──[healthy]──▶ ngrok-rtmp
```

Docker Volume `hls-data`:
- `media-server`: read/write (mediamtx + publish.sh が書き込む)
- `api-server`: read-only (watchfiles で監視し、/hls・/seg エンドポイントで配信)

全コンテナは `network_mode: host` — コンテナ間通信は `localhost` / `127.0.0.1`。

---

## 10. ngrok 構成

### 役割分担

| 役割 | 実装 | 管理場所 |
|------|------|---------|
| サイト / HLS 公開 (HTTPS) | Cloudflare Tunnel | **別スタック** |
| RTMP 公開 (TCP) | ngrok-rtmp | このリポジトリ |

### ngrok 無料プランの制限

| 制限 | 内容 | 対策 |
|------|------|------|
| URL 変動 | RTMP URL がリスタートごとに変更 | ポータル WebSocket でリアルタイム更新 |
| 同時トンネル | TCP 1 トンネル/アカウント | LAN 内配信なら `ngrok-rtmp` を削除可 |
| 帯域 | 月 1GB | 有料プランへアップグレード |

---

*最終更新: 2026-06-07*
