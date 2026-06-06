# SYCS Stream Server

OBS → LL-HLS → VRChat 向け超低遅延ライブ配信基盤。  
GTX1650 の NVENC を活用し、ngrok 経由で HTTPS 公開する Docker Compose スタック。  
**設定ファイル不要** — すべての設定は環境変数 (`.env` または Portainer) で管理します。

---

## アーキテクチャ

```
OBS Studio
    │ RTMP (port 1935)
    ▼
┌──────────────────────────────────────────────┐
│ media-server (nginx-rtmp + FFmpeg NVENC)     │
│  RTMP 受信 → h264_nvenc エンコード           │
│  ├─ pc/      LL-HLS fmp4  (超低遅延)         │
│  └─ android/ 標準 HLS TS  (最大互換)         │
└──────────────────┬───────────────────────────┘
                   │ Docker Volume (hls-data)
                   ▼
┌──────────────────────────────────────────────┐
│ api-server (Python / FastAPI :8080)          │
│  HLS 配信 / ポータルサイト / ngrok RTMP 表示 │
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
│  localhost:1935 → rtmp://x.tcp.ngrok.io:PORT │
└──────────────────────────────────────────────┘
```

**ネットワークモード:** 全コンテナが `network_mode: host` を使用。  
コンテナ間通信はコンテナ名 DNS の代わりに `localhost` を使用します。

**期待遅延:**

| 経路 | 遅延 |
|------|------|
| ローカルネットワーク視聴 | ~0.5s |
| Cloudflare 経由 (PC LL-HLS) | ~1〜1.5s |
| Cloudflare 経由 (Android HLS) | ~1.5〜3s |

---

## 必要要件

| 項目 | 要件 |
|------|------|
| OS | Ubuntu 22.04 LTS (KVM/QEMU 仮想化可) |
| GPU | GTX1650 以上 (NVENC 対応) |
| RAM | 4GB 以上 |
| Docker | 20.10 以上 + NVIDIA Container Toolkit 設定済み |
| Docker Compose | v2.x 以上 |
| ngrok | アカウント登録済み (無料プラン可) |

---

## クイックスタート

### 1. リポジトリ取得

```bash
git clone https://github.com/YOUR_USER/SYCS-StreamServer.git
cd SYCS-StreamServer
```

### 2. 環境変数設定

```bash
cp .env.example .env
nano .env
# NGROK_AUTHTOKEN=xxx          を必ず設定
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
ngrok の公開 URL・VRChat URL・RTMP アドレスが表示されます。

---

## OBS 設定

| 項目 | 値 |
|------|-----|
| 設定 → 配信 → サービス | **カスタム...** |
| サーバー | ポータルの「RTMP(OBS 配信先)」欄の URL |
| ストリームキー | 任意 (例: `stream`) |

**推奨エンコード設定:**

| 項目 | 推奨値 |
|------|--------|
| エンコーダ | NVENC H.264 (または x264) |
| レート制御 | CBR |
| ビットレート | 3000〜6000 Kbps |
| キーフレーム間隔 | 1秒 固定 |

---

## VRChat での視聴

ポータルサイトの接続情報から URL をコピーして使います。

| プラットフォーム | 使う URL | 遅延 |
|----------------|---------|------|
| **VRChat PC** | 「VRChat PC」欄 (`/pc/master.m3u8`) | ~1〜1.5s |
| **VRChat Android** | 「VRChat Android」欄 (`/android/index.m3u8`) | ~1.5〜3s |

---

## Portainer / GitHub Stack デプロイ

Portainer の **Stacks → Add stack → Repository** から:

| 項目 | 値 |
|------|-----|
| Repository URL | `https://github.com/YOUR_USER/SYCS-StreamServer` |
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
> このリポジトリには含まれません。Cloudflare Zero Trust ダッシュボードで  
> トンネルを作成し、`localhost:8080` を公開してください。

### ngrok (RTMP 公開)

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `NGROK_AUTHTOKEN` | *(必須)* | ngrok 認証トークン |
| `NGROK_RTMP_TARGET` | `localhost:1935` | RTMP トンネル向き先 |
| `NGROK_RTMP_API` | `http://localhost:4040` | FastAPI が参照する ngrok API URL |
| `NGROK_CACHE_TTL` | `30` | ngrok URL キャッシュ秒数 |

### エンコード / HLS

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `RTMP_PORT` | `1935` | RTMP 受信ポート |
| `VIDEO_BITRATE` | `4000k` | 映像ビットレート |
| `AUDIO_BITRATE` | `128k` | 音声ビットレート |
| `HLS_SEGMENT_TIME` | `0.5` | セグメント長 (秒) |
| `HLS_PART_DURATION` | `0.1` | LL-HLS パーツ長 (秒) |
| `HLS_LIST_SIZE` | `6` | プレイリスト保持数 |
| `API_PORT` | `8080` | ポータルサイト公開ポート |
| `LOG_LEVEL` | `info` | ログレベル |

### Docker / インフラ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `CUDA_VERSION` | `12.3.1-runtime-ubuntu22.04` | ベースイメージの CUDA バージョン |
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
├── docker-compose.yml        ← 全サービス定義 (設定ファイル不要)
├── .env.example              ← コピーして .env を作成
├── .gitignore
├── README.md
├── SPEC.md
├── media-server/
│   ├── Dockerfile            ← nvidia/cuda ベース + nginx-rtmp + FFmpeg
│   ├── nginx.conf            ← RTMP 受信 (on_publish → publish.sh)
│   ├── publish.sh            ← PC/Android 2系統 HLS 生成
│   └── entrypoint.sh
├── api-server/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py               ← FastAPI ポータル + HLS 配信 + ngrok 取得
└── ngrok/
    └── ngrok.yml.example     ← 参照用 (現在は使用しない)
```

---

## トラブルシューティング

### ngrok RTMP URL が表示されない

```bash
# ngrok-rtmp コンテナのログを確認
docker compose logs ngrok-rtmp

# よくある原因:
# - NGROK_AUTHTOKEN が未設定または誤り
# - ngrok アカウントのトンネル上限に達している (無料: TCP 1トンネル/アカウント)
```

> **無料プランの注意:** ngrok 無料プランは TCP トンネル 1 本まで。  
> LAN 内から OBS 配信する場合は `ngrok-rtmp` サービスをコメントアウトできます。

### Cloudflare Tunnel 経由でアクセスできない

このリポジトリは Cloudflare Tunnel のコンテナを含みません。  
別スタックで `cloudflared` を起動し、`localhost:8080` をトンネル経由で公開してください。  
公開ドメインを `SITE_BASE_URL` に設定するとポータルの URL 表示に反映されます。

### GPU が認識されない

```bash
docker exec sycs-media-server nvidia-smi
# または
docker compose logs media-server | grep -E "NVENC|GPU"
```

`libx264` フォールバックのログが出る場合は NVIDIA Container Toolkit を確認:
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 遅延が大きい / 遅延が増え続ける

1. `.env` で `HLS_SEGMENT_TIME=0.5`・`HLS_PART_DURATION=0.1` になっているか確認
2. ブラウザで `http://サーバーIP:8080/hls/live/{key}/pc/index.m3u8` を開き  
   `EXT-X-PROGRAM-DATE-TIME` タグが含まれているか確認
3. OBS のキーフレーム間隔を 1 秒に設定する

### OBS が接続できない (LAN 外配信)

```bash
docker compose logs ngrok-rtmp | grep "url="
# 表示された rtmp://x.tcp.ngrok.io:PORT/live を OBS の配信先に設定
```
