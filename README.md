# SYCS Stream Server

OBS → LL-HLS → VRChat 向け超低遅延ライブ配信基盤。  
GTX1650 の NVENC を活用し、ngrok + Cloudflare Tunnel で HTTPS 公開する Docker Compose スタック。  
**設定ファイル不要** — すべての設定は環境変数 (`.env` または Portainer) で管理します。

---

## アーキテクチャ

```
OBS Studio
    │ RTMP (port 1935)
    ▼
┌──────────────────────────────────────────────┐
│ media-server (mediamtx + FFmpeg NVENC)       │
│  RTMP 受信 → h264_nvenc ABR VBR エンコード   │
│  → fmp4 LL-HLS (high/low 2バリアント)        │
└──────────────────┬───────────────────────────┘
                   │ Docker Volume (hls-data)
                   ▼
┌──────────────────────────────────────────────┐
│ api-server (Python / FastAPI :8080)          │
│  HLS 配信 / ポータルサイト / セッション管理  │
│  ngrok RTMP URL リアルタイム配信 (WebSocket) │
└──────────────────┬───────────────────────────┘
                   │ HTTP (localhost:8080)
                   ▼
┌──────────────────────────────────────────────┐
│ Cloudflare Tunnel  ← 別スタックで管理        │
│  :8080 → https://stream.example.com          │
│  VRChat PC / Android / ブラウザがアクセス    │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│ ngrok-rtmp (TCP トンネル、LAN 外配信時)      │
│  127.0.0.1:1935 → rtmp://x.tcp.ngrok.io:PORT │
└──────────────────────────────────────────────┘
```

**ネットワークモード:** 全コンテナが `network_mode: host` を使用。  
コンテナ間通信は `localhost` / `127.0.0.1` で行います。

**期待遅延:**

| 経路 | 遅延 |
|------|------|
| ローカルネットワーク視聴 | ~0.5s |
| Cloudflare 経由 (PC LL-HLS) | ~1〜1.5s |
| Cloudflare 経由 (Android) | ~1〜2s |

---

## 必要要件

| 項目 | 要件 |
|------|------|
| OS | Ubuntu 22.04 LTS (KVM/QEMU 仮想化可) |
| GPU | GTX1650 以上 (NVENC 対応)、なければ libx264 自動フォールバック |
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
ngrok の RTMP URL・VRChat URL が表示されます。

---

## OBS 設定

| 項目 | 値 |
|------|-----|
| 設定 → 配信 → サービス | **カスタム...** |
| サーバー | ポータルの「RTMP URL」欄の URL (例: `rtmp://0.tcp.jp.ngrok.io:XXXXX/live`) |
| ストリームキー | **任意** (何を入力しても動作します) |

> ngrok が起動していない場合はポータルにローカル IP の RTMP URL が表示されます。  
> LAN 内から配信する場合は `ngrok-rtmp` サービスを docker-compose.yml でコメントアウトできます。

**推奨 OBS エンコード設定:**

| 項目 | 推奨値 |
|------|--------|
| エンコーダ | NVENC H.264 (または x264) |
| レート制御 | VBR または CBR |
| ビットレート | 3000〜6000 Kbps |
| キーフレーム間隔 | 1秒 固定 |

---

## ポータルの使い方

`http://サーバーIP:8080` にアクセスすると OBS 配信設定と視聴 URL 生成ページが開きます。

1. **OBS 配信設定** — RTMP URL が自動表示される。ngrok 起動中は ngrok URL、未起動時は LAN IP を表示。
2. **視聴 URL 生成** — ストリームキー欄に `live` と入力して「確認」を押す。
3. **VRChat URL** が生成されるのでコピーして VRChat のビデオプレイヤーに貼り付ける。

---

## VRChat での視聴

| プラットフォーム | URL | 遅延 |
|----------------|-----|------|
| **VRChat PC** | ポータルの「VRChat URL」欄 | ~0.5〜1s |
| **VRChat Android** | 同じ URL (LL-HLS フォールバック) | ~1〜2s |

> PC / Android ともに同一 URL を使います。  
> fmp4 LL-HLS は後方互換設計なので、LL-HLS 非対応の Android AVPro でも再生できます。

---

## Portainer / GitHub Stack デプロイ

Portainer の **Stacks → Add stack → Repository** から:

| 項目 | 値 |
|------|-----|
| Repository URL | `https://github.com/sukun-inu/SYCS-StreamServer` |
| Branch | `main` |
| Compose path | `docker-compose.yml` |
| Environment variables | `.env.example` の内容を各行貼り付け、値を埋める |

> `.env` ファイルは `.gitignore` で除外済み。秘密情報は Portainer の  
> Environment Variables 欄に直接入力してください。

---

## 設定パラメータ一覧

### Cloudflare Tunnel / サイト公開

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `SITE_BASE_URL` | *(空文字)* | Cloudflare Tunnel の公開ドメイン (例: `https://stream.example.com`) |

> Cloudflare Tunnel のコンテナは **別スタック** で管理します。  
> Cloudflare Zero Trust ダッシュボードでトンネルを作成し、`localhost:8080` を公開してください。

### ngrok (RTMP 公開)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok 認証トークン |
| `NGROK_RTMP_TARGET` | `127.0.0.1:1935` | RTMP トンネル向き先 |
| `NGROK_RTMP_API` | `http://127.0.0.1:4040` | api-server が参照する ngrok API URL |
| `NGROK_CACHE_TTL` | `30` | ngrok URL キャッシュ秒数 |

### エンコード / HLS

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `VIDEO_BITRATE` | `6000k` | high バリアント映像平均ビットレート (maxrate 自動 1.35 倍) |
| `VIDEO_BITRATE_LOW` | `2000k` | low バリアント映像平均ビットレート (720p) |
| `AUDIO_BITRATE` | `320k` | high バリアント音声上限 (low は 128k 固定) |
| `HLS_SEGMENT_TIME` | `0.5` | セグメント長 (秒) |
| `HLS_PART_DURATION` | `0.1` | LL-HLS パーツ長 (秒) |
| `HLS_LIST_SIZE` | `6` | プレイリスト保持セグメント数 |

### セッション管理

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAX_SESSIONS` | `100` | 同時視聴上限 (超過新規接続はキュー待機) |
| `SESSION_TIMEOUT` | `8.0` | 最終リクエストからのセッション消滅秒数 |
| `QUEUE_TIMEOUT` | `20.0` | キュー待機タイムアウト秒数 (超過で 503) |
| `MAX_BPS` | `1375000` | セッションあたり最大受信バイト/秒 (超過で 429) |

### API / ログ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `API_PORT` | `8080` | ポータルサイト公開ポート |
| `LOG_LEVEL` | `info` | ログレベル (debug / info / warning / error) |

### Docker / インフラ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `GPU_COUNT` | `1` | 割り当て GPU 数 |
| `RESTART_POLICY` | `unless-stopped` | コンテナ再起動ポリシー |
| `HC_INTERVAL` | `10s` | ヘルスチェック間隔 |
| `HC_TIMEOUT` | `5s` | ヘルスチェックタイムアウト |
| `HC_RETRIES` | `3` | ヘルスチェック失敗許容回数 |
| `MEDIA_CONTAINER` | `sycs-media-server` | メディアサーバーコンテナ名 |
| `API_CONTAINER` | `sycs-api-server` | API サーバーコンテナ名 |
| `NGROK_RTMP_CONTAINER` | `sycs-ngrok-rtmp` | ngrok RTMP コンテナ名 |

---

## ファイル構成

```
SYCS-StreamServer/
├── docker-compose.yml        ← 全サービス定義
├── .env.example              ← コピーして .env を作成
├── .gitignore
├── README.md
├── SPEC.md
├── media-server/
│   ├── Dockerfile            ← nvidia/cuda ベース (マルチステージ: mediamtx + FFmpeg)
│   ├── mediamtx.yml          ← RTMP 受信設定 (runOnPublish → publish.sh)
│   ├── publish.sh            ← FFmpeg ABR VBR LL-HLS 生成スクリプト
│   └── entrypoint.sh
└── api-server/
    ├── Dockerfile
    ├── requirements.txt
    └── main.py               ← FastAPI ポータル + HLS 配信 + WebSocket
```

---

## トラブルシューティング

### ngrok RTMP URL が表示されない

```bash
docker compose logs ngrok-rtmp
```

よくある原因:
- `NGROK_AUTHTOKEN` が未設定または誤り
- ngrok アカウントのトンネル上限 (無料: TCP 1 トンネル/アカウント)

> LAN 内から配信する場合は `ngrok-rtmp` サービスをコメントアウトできます。  
> ポータルに自動でローカル IP ベースの RTMP URL が表示されます。

### OBS が接続できない (LAN 外配信)

```bash
docker compose logs ngrok-rtmp | grep "url="
```

表示された `rtmp://x.tcp.ngrok.io:PORT/live` を OBS のサーバーに設定してください。

### HLS 出力が生成されない / ストリームが映らない

```bash
# publish.sh のログを確認 (ストリーム開始後に生成される)
docker exec sycs-media-server cat /tmp/publish_live.log

# mediamtx のログを確認
docker compose logs media-server
```

### GPU が認識されない

```bash
docker exec sycs-media-server nvidia-smi
docker compose logs media-server | grep -E "NVENC|GPU|nvenc"
```

`h264_nvenc 不可 → libx264` のログが出る場合は NVIDIA Container Toolkit を確認:
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 遅延が大きい / 遅延が増え続ける

1. `.env` で `HLS_SEGMENT_TIME=0.5`・`HLS_PART_DURATION=0.1` になっているか確認
2. OBS のキーフレーム間隔を 1 秒に設定する
3. ブラウザで `/hls/live/high/index.m3u8` を開き `EXT-X-PROGRAM-DATE-TIME` タグがあるか確認

### Cloudflare Tunnel 経由でアクセスできない

このリポジトリは Cloudflare Tunnel のコンテナを含みません。  
別スタックで `cloudflared` を起動し、`localhost:8080` をトンネル経由で公開してください。  
公開ドメインを `SITE_BASE_URL` に設定するとポータルの URL 表示に反映されます。
