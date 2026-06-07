# SYCS Stream Server

OBS → LL-HLS → VRChat / ブラウザ向け超低遅延ライブ配信基盤。  
GTX1650 の NVENC を活用し、ngrok + Cloudflare Tunnel で HTTPS 公開する Docker Compose スタック。  
**設定ファイル不要** — すべての設定は環境変数 (`.env` または Portainer) で管理します。

---

## アーキテクチャ

```
OBS Studio
    │ RTMP (port 1935)
    ▼
┌──────────────────────────────────────────────────────┐
│ media-server (mediamtx + FFmpeg NVENC)               │
│                                                      │
│  mediamtx ── LL-HLS ネイティブ生成 ──────────────    │
│   OBS入力   →  MediaMTX HLS /live/{key}/index.m3u8  │
│                                                      │
│  FFmpeg (publish.sh)                                 │
│   RTSP → 720p VBR → RTMP → mediamtx再受信           │
│             →  MediaMTX HLS /live/{key}_transcode/   │
└────────────────────────┬─────────────────────────────┘
                         │ Docker Volume (hls-data)
                         ▼
┌──────────────────────────────────────────────────────┐
│ api-server (Python / FastAPI :8080)                  │
│                                                      │
│  ブラウザ向け: WebSocket でプレイリストをプッシュ    │
│    /api/token → /ws/hls/{key}  (m3u8 URL 非公開)    │
│    /seg/{path}?exp=T&sig=S     (HMAC 署名付き URL)   │
│                                                      │
│  VRChat 向け: 従来 HTTP 配信                         │
│    /hls/live/{key}/master.m3u8 (動的生成)            │
│    /hls/live/{key}/index.m3u8  (MediaMTXへプロキシ)  │
│                                                      │
│  ポータル: RTMP URL リアルタイム表示 (/ws)           │
└────────────────────────┬─────────────────────────────┘
                         │ HTTP (localhost:8080)
                         ▼
┌──────────────────────────────────────────────────────┐
│ Cloudflare Tunnel  ← 別スタックで管理               │
│  :8080 → https://stream.example.com                 │
│  VRChat / ブラウザがアクセス                         │
└──────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────┐
│ ngrok-rtmp (TCP トンネル、LAN 外配信時)              │
│  127.0.0.1:1935 → rtmp://x.tcp.ngrok.io:PORT/live   │
└──────────────────────────────────────────────────────┘
```

**ネットワークモード:** 全コンテナが `network_mode: host`。コンテナ間通信は `localhost` / `127.0.0.1`。

**期待遅延:**

| 経路 | 遅延 |
|------|------|
| ローカルネットワーク視聴 (ブラウザ) | ~400〜700ms |
| Cloudflare 経由 (ブラウザ LL-HLS) | ~700ms〜1.3s |
| Cloudflare 経由 (VRChat) | ~2〜3s |

---

## 必要要件

| 項目 | 要件 |
|------|------|
| OS | Ubuntu 22.04 LTS 以上 |
| GPU | GTX1650 以上 (NVENC 対応)、なければ libx264 自動フォールバック。NVIDIA ドライバ 560 以上推奨 |
| RAM | 4GB 以上 |
| Docker | 20.10 以上 + NVIDIA Container Toolkit 設定済み |
| Docker Compose | v2.x 以上 |
| ngrok | アカウント登録済み (無料プラン可、LAN 内配信のみなら不要) |

---

## クイックスタート

### 1. リポジトリ取得

```bash
git clone https://github.com/sukun-inu/SYCS-StreamServer.git
cd SYCS-StreamServer
```

### 2. 環境変数設定

```bash
cp .env.example .env
nano .env
# NGROK_AUTHTOKEN=xxx          を必ず設定 (LAN 外配信時)
# SITE_BASE_URL=https://...    Cloudflare Tunnel の公開ドメインを設定
```

> **Portainer を使う場合:** `.env` ファイルを作成せず、Portainer の  
> **Stacks → Stack 編集 → Environment variables** に各変数を直接入力してください。

### 3. 起動

```bash
docker compose up -d
docker compose logs -f
```

### 4. ポータル確認

ブラウザで `http://サーバーIP:8080` を開くと  
ngrok の RTMP URL・VRChat URL・視聴ページリンクが表示されます。

---

## OBS 設定

| 項目 | 値 |
|------|-----|
| 設定 → 配信 → サービス | **カスタム...** |
| サーバー | ポータルの「RTMP URL」欄の URL (例: `rtmp://0.tcp.jp.ngrok.io:XXXXX/live`) |
| ストリームキー | **任意の半角英数字** (例: `kawasaki`) |

> ストリームキーは HLS 出力先ディレクトリ名になります。  
> ポータルで視聴 URL を生成する際は OBS と同じキーを入力してください。

**推奨 OBS エンコード設定:**

| 項目 | 推奨値 |
|------|--------|
| エンコーダ | NVENC H.264 |
| レート制御 | VBR |
| ビットレート | 3000〜6000 Kbps |
| キーフレーム間隔 | **1秒 固定** (重要) |

---

## ポータルの使い方

`http://サーバーIP:8080` にアクセスします。

1. **OBS 配信設定** — RTMP URL が自動表示される。ngrok 起動中は ngrok URL、未起動時は LAN IP を表示。
2. **視聴 URL 生成** — ストリームキー欄に OBS で設定したキーを入力して「確認」を押す。
3. 生成される **VRChat URL** (HLS 直 URL) と **視聴ページ** (ブラウザ向け) の 2 種類を使い分ける。

---

## 視聴方法

### ブラウザ視聴 (`/watch/{key}`)

- **WebSocket 経由でプレイリストを受信** — m3u8 URL はブラウザの DevTools に露出しない
- セグメント URL は HMAC 署名付き短命 URL (デフォルト 120 秒) — コピーしても即失効
- hls.js + カスタムローダーで LL-HLS を再生、遅延約 0.5〜1s

### VRChat 視聴 (VRChat URL)

- 従来の HTTP HLS 配信 (VRChat は WebSocket 未対応)
- ポータルの「VRChat URL」欄をコピーして VRChat のビデオプレイヤーに貼り付け
- PC / Android ともに同一 URL

---

## 設定パラメータ一覧

### Cloudflare Tunnel / サイト公開

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `SITE_BASE_URL` | *(空文字)* | Cloudflare Tunnel の公開ドメイン (例: `https://stream.example.com`) |

### ngrok (RTMP 公開)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok 認証トークン |
| `NGROK_RTMP_TARGET` | `127.0.0.1:1935` | RTMP トンネル向き先 |
| `NGROK_RTMP_API` | `http://127.0.0.1:4040` | api-server が参照する ngrok API URL |
| `NGROK_CACHE_TTL` | `30` | ngrok URL キャッシュ秒数 |

### エンコード (publish.sh — 720p low バリアント)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `VIDEO_BITRATE_LOW` | `2000k` | 720p バリアントの映像平均ビットレート |
| `VIDEO_FPS_LOW` | `30` | 720p バリアントの固定FPS。`30` は低負荷、`60` は滑らかさ優先 |
| `AUDIO_BITRATE` | `128k` | 720p バリアントの音声ビットレート |

> high バリアントはOBSの出力をそのまま使用します。OBS 側のビットレート設定が反映されます。  
> LL-HLS パーツ長 (100ms) やセグメント長 (1s) は `media-server/mediamtx.yml` で管理しています。

### セキュリティ (署名付きセグメント URL)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MEDIAMTX_HLS_URL` | `http://127.0.0.1:8888` | 旧互換のMediaMTX HLS内部URL |
| `MEDIAMTX_HLS_URLS` | `http://127.0.0.1:8888,http://media-server:8888,http://sycs-media-server:8888` | api-server がMediaMTXのLL-HLSプレイリスト/セグメントを探す内部URL候補 |
| `MEDIAMTX_HLS_TIMEOUT` | `1.0` | MediaMTX HLS取得タイムアウト秒数 |
| `HLS_MISSING_CACHE_TTL` | `1.0` | HLS未検出時に短時間キャッシュして待機画面の過剰pollを抑える秒数 |
| `HLS_CHANGE_POLL_INTERVAL` | `0.5` | watchfiles補助用のHLS変更poll間隔秒数 |
| `SEGMENT_SECRET` | *(起動ごとランダム)* | セグメント URL 署名 HMAC シークレット |
| `SEGMENT_TTL` | `120` | 署名付き URL の有効期限 (秒) |
| `SEGMENT_WAIT_TIMEOUT` | `1.5` | LL-HLS の未生成 part 先読みを短時間待つ秒数 |
| `TOKEN_TTL` | `60` | 視聴ページ接続用ワンタイムトークンの有効期限 (秒) |

> `SEGMENT_SECRET` を固定しない場合、コンテナ再起動で既存の署名 URL が失効します。  
> 視聴ページは再接続時に新しい署名 URL を受け取るため、通常は問題ありません。

### セッション管理 (VRChat / HTTP 向け)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAX_SESSIONS` | `100` | 同時視聴上限 (超過新規接続はキュー待機) |
| `SESSION_TIMEOUT` | `15.0` | 最終リクエストからのセッション消滅秒数 |
| `QUEUE_TIMEOUT` | `20.0` | キュー待機タイムアウト秒数 (超過で 503) |
| `MAX_BPS` | `3000000` | セッションあたり最大受信バイト/秒 (超過で 429) |

### API / ログ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `LOG_LEVEL` | `info` | ログレベル (debug / info / warning / error) |

---

## ファイル構成

```
SYCS-StreamServer/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── SPEC.md
├── media-server/
│   ├── Dockerfile            nvidia/cuda ベース + mediamtx + apt FFmpeg
│   ├── mediamtx.yml          LL-HLS ネイティブ設定 (hlsVariant: lowLatency)
│   ├── publish.sh            FFmpeg トランスコード (RTSP→720p→RTMP)
│   └── entrypoint.sh
└── api-server/
    ├── Dockerfile
    ├── requirements.txt
    ├── main.py               FastAPI: WS プッシュ + 署名配信
    └── templates/
        ├── portal.html       ポータル画面
        └── watch.html        ブラウザ視聴ページ
```

---

## トラブルシューティング

### OBS が接続できない

```bash
docker compose logs ngrok-rtmp | grep "url="
```

表示された `rtmp://x.tcp.ngrok.io:PORT/live` を OBS のサーバーに設定してください。  
`NGROK_AUTHTOKEN` が未設定・誤りの場合は ngrok コンテナが起動しません。LAN 内のみで使う場合はポータルに表示されるローカル RTMP URL を使用してください。

### 映像が届かない / ストリームが映らない

```bash
# mediamtx のログを確認
docker compose logs media-server

# publish.sh のログを確認 (配信開始後に生成)
docker exec sycs-media-server cat /tmp/publish_{ストリームキー}.log

# HLS ファイルが生成されているか確認
docker exec sycs-media-server ls /hls/live/
# LL-HLS プレイリストがMediaMTXから取れるか確認
curl -fsS http://127.0.0.1:8888/live/{ストリームキー}/index.m3u8 | head
```

よくある原因:
- OBS のキーフレーム間隔が 1 秒以外に設定されている
- `media-server/mediamtx.yml` の `hlsAlwaysRemux: yes` が無効で、MediaMTX が `/hls` へ常時書き出していない
- GPU が認識されていない (下記参照)

### GPU が認識されない / NVENC が使えない

```bash
docker exec sycs-media-server nvidia-smi
docker compose logs media-server | grep -E "GPU|nvenc|scale_cuda|pipeline"
```

`libx264` フォールバックのログが出る場合は NVIDIA Container Toolkit を確認:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### ブラウザ視聴ページが再生されない

ブラウザの DevTools → Console でエラーを確認してください。

```bash
# api-server のログを確認
docker compose logs api-server
```

よくある原因:
- `/api/token/{key}` が 400 を返している → ストリームキーが正規表現 `^[A-Za-z0-9_-]{1,64}$` に一致しない
- WebSocket 接続が失敗している → Cloudflare Tunnel の WebSocket が有効になっているか確認

### 遅延が大きい / 遅延が増え続ける

1. OBS のキーフレーム間隔を **1 秒固定** に設定する
2. ブラウザの DevTools → Network で `/ws/hls/{key}` の WebSocket が接続されているか確認
3. `media-server/mediamtx.yml` の `hlsAlwaysRemux: yes` と `hlsPartDuration: 100ms` が設定されているか確認

### Cloudflare Tunnel 経由でアクセスできない

このリポジトリは Cloudflare Tunnel のコンテナを含みません。別スタックで `cloudflared` を起動し、`localhost:8080` をトンネル経由で公開してください。  
Cloudflare の設定で **WebSocket を有効** にしてください (`/ws/hls/` が使用します)。
