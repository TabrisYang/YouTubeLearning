#!/bin/bash
# 雙擊即可啟動本機測試介面（macOS）
# 第一次執行會自動建立虛擬環境、安裝依賴、產生 .env 供你填 API key
set -e
cd "$(dirname "$0")"

echo "═══════════════════════════════════════"
echo "  🎓 YT 課程策展系統 — 本機測試啟動器"
echo "═══════════════════════════════════════"

# 1. 虛擬環境
if [ ! -d .venv ]; then
  echo "▸ 建立 Python 虛擬環境（僅第一次）…"
  python3 -m venv .venv
fi
echo "▸ 檢查依賴套件…"
.venv/bin/pip install -q -r requirements.txt

# 2. .env（選填：只有測「點數制」時才需要平台 key）
if [ ! -f .env ]; then
  cp .env.example .env
fi
set -a; source .env; set +a
export DEV_MODE=true
# 測試環境打開訂閱制入口（正式部署不經過此啟動器，flag 由部署設定控制、必為 false）
export ENABLE_OAUTH=true

if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$GOOGLE_API_KEY" ]; then
  echo ""
  echo "ℹ️  尚未設定平台 API key（.env）—— 沒關係，兩種測法："
  echo "   A. 介面中點「設定」→ 自備 API key + YouTube key（測 BYOK 模式，免填 .env）"
  echo "   B. 填 .env 的 ANTHROPIC_API_KEY 與 YOUTUBE_API_KEY（測「點數制」模式）"
fi

# 4. 啟動伺服器 + 自動開瀏覽器
PORT=8787
echo ""
echo "▸ 啟動中… 瀏覽器將自動開啟 http://127.0.0.1:$PORT/dev"
echo "▸ 關閉：在此視窗按 Ctrl+C，或直接關閉視窗"
echo ""
( sleep 2 && open "http://127.0.0.1:$PORT/dev" ) &
.venv/bin/uvicorn main:app --port $PORT
