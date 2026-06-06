# SYCS Stream Server

OBS → LL-HLS → VRChat 向け超低遅延ライブ配信基盤。  
GTX1650 の NVENC を活用し、Cloudflare Tunnel 経由で HTTPS 公開する Docker Compose スタック。

---

## アーキテクチャ

```
OBS Studio
    │ RTMP (port 1935)
    ▼
┌──────────────────────────────────────┐
│ media-server (nginx-rtmp + FFmpeg)   │
│  ・RTMP 受信                          │
│  ・h264_nvenc エンコード (GTX1650)    │
│  ・LL-HLS セグメント生成              │
└───────────────┬──────────────────────┘
                │ Docker Volume (hls-data)
                ▼
┌──────────────────────────────────────┐
│ api-server (Python / FastAPI)        │
│  ・LL-HLS ファイル配信 (port 8080)    │
│  ・playlist タイムスタンプ自動補正    │
│  ・ブロッキングリクエスト対応         │
│  ・ステータス Web UI                  │
└───────────────┬──────────────────────┘
                │ HTTP
                ▼
┌──────────────────────────────────────┐
│ cloudflared (Cloudflare Tunnel)      │
└───────────────┬──────────────────────┘
                │ HTTPS
                ▼
          VRChat / ブラウザ
```

**期待遅延:** ローカル視聴 ~0.5s / Cloudflare Tunnel 経由 ~1〜1.5s

---

## 必要要件

| 項目 | 要件 |
|------|------|
| OS | Ubuntu 22.04 LTS (仮想化 KVM/QEMU 可) |
| GPU | GTX1650 以上 (NVENC 対応、Turing/Pascal) |
| RAM | 4GB 以上 |
| Docker | 20.10 以上 (NVIDIA Container Toolkit 設定済み) |
| Docker Compose | v2.x 以上 |
| Cloudflare | Tunnel 設定済みアカウント |

---

## クイックスタート

### 1. リポジトリの準備

```bash
git clone https://github.com/YOUR_USER/SYCS-StreamServer.git
cd SYCS-StreamServer
cp .env.example .env
```

### 2. 環境変数の設定

```bash
# .env を編集 (最低限 VIDEO_BITRATE を環境に合わせて調整)
nano .env
```

### 3. Cloudflare Tunnel のセットアップ

```bash
# Tunnel を作成
cloudflared tunnel create sycs-stream

# 認証情報を配置
cp ~/.cloudflared/<TUNNEL_ID>.json ./cloudflared/

# 設定ファイルを作成
cp cloudflared/config.yml.example cloudflared/config.yml
nano cloudflared/config.yml  # YOUR_TUNNEL_ID と hostname を置換

# DNS CNAME を登録
cloudflared tunnel route dns sycs-stream stream.example.com
```

### 4. 起動

```bash
docker compose up -d

# ログ確認
docker compose logs -f
```

### 5. ステータス確認

ブラウザで `http://サーバーIP:8080` を開く。

---

## OBS 設定

| 項目 | 値 |
|------|-----|
| 設定 → 配信 → サービス | **カスタム...** |
| サーバー | `rtmp://サーバーIP:1935/live` |
| ストリームキー | 任意 (例: `stream`) |

**推奨エンコード設定:**

| 項目 | 推奨値 |
|------|--------|
| エンコーダ | NVENC H.264 (もしくは x264) |
| レート制御 | CBR |
| ビットレート | 3000〜6000 Kbps |
| キーフレーム間隔 | 2秒 固定 |
| プリセット | Max Quality / Low Latency |
| プロファイル | High |

---

## VRChat での視聴

1. ワールド内の Video Player オブジェクトに以下の URL を入力:

```
https://stream.example.com/hls/live/stream/master.m3u8
```

> `stream` の部分は OBS のストリームキーに合わせてください。

2. `master.m3u8` が AVPro で再生されない場合は `index.m3u8` を試してください:

```
https://stream.example.com/hls/live/stream/index.m3u8
```

---

## Portainer / GitHub Stack デプロイ

Portainer の **Stacks → Add stack → Repository** から以下を設定:

| 項目 | 値 |
|------|-----|
| Repository URL | `https://github.com/YOUR_USER/SYCS-StreamServer` |
| Branch | `main` |
| Compose path | `docker-compose.yml` |
| Environment variables | `.env` の内容を各行入力 |

> `.env` はセキュリティのため Git 管理外 (`.gitignore` 設定済み)。  
> Portainer の Environment Variables 欄に直接入力することで秘密情報を安全に管理できます。

---

## 設定パラメータ

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `RTMP_PORT` | `1935` | OBS からの RTMP 受信ポート |
| `VIDEO_BITRATE` | `4000k` | 映像ビットレート (GTX1650 上限目安: 8000k) |
| `AUDIO_BITRATE` | `128k` | 音声ビットレート |
| `HLS_SEGMENT_TIME` | `0.5` | セグメント長 (秒)。小さいほど低遅延、負荷増 |
| `HLS_PART_DURATION` | `0.1` | LL-HLS パーツ長 (秒)。最小配信単位 |
| `HLS_LIST_SIZE` | `6` | プレイリストに保持するセグメント数 |
| `API_PORT` | `8080` | HLS 配信 HTTP ポート |
| `LOG_LEVEL` | `info` | ログレベル (debug/info/warning/error) |

---

## ディレクトリ構成

```
SYCS-StreamServer/
├── docker-compose.yml
├── .env.example              ← コピーして .env を作成
├── .gitignore
├── media-server/
│   ├── Dockerfile            ← nvidia/cuda ベース + nginx-rtmp + FFmpeg
│   ├── nginx.conf            ← RTMP 受信設定
│   ├── publish.sh            ← FFmpeg NVENC + LL-HLS 生成スクリプト
│   └── entrypoint.sh
├── api-server/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py               ← FastAPI HLS 配信 + playlist パッチ処理
└── cloudflared/
    └── config.yml.example    ← コピーして config.yml を作成
```

---

## トラブルシューティング

### GPU が認識されない

```bash
# コンテナ内から確認
docker exec sycs-media-server nvidia-smi

# ホスト側の確認
nvidia-smi
docker info | grep -i runtime
```

`nvidia-smi` が失敗する場合は NVIDIA Container Toolkit の設定を確認:
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### OBS が接続できない

```bash
# RTMP ポートの確認
docker compose logs media-server
nc -zv サーバーIP 1935
```

ファイアウォールで 1935/tcp が開いているか確認してください。

### 遅延が大きい / 遅延が増え続ける

- `HLS_SEGMENT_TIME=0.5`、`HLS_PART_DURATION=0.1` になっているか `.env` を確認
- `docker compose logs api-server` でエラーがないか確認
- Cloudflare の Cache Rules で `/hls/*` が **No Store** になっているか確認

### libx264 フォールバックログが出る

```
[publish] NVENC unavailable — falling back to libx264
```

GPU がコンテナに渡されていません。`docker-compose.yml` の `deploy.resources.reservations.devices` と Docker の NVIDIA ランタイム設定を確認してください。

---

## ライセンス

MIT
