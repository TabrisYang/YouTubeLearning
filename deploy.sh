#!/bin/bash
# 一鍵部署到 Google Cloud Run
# 前置：安裝 gcloud CLI、已 gcloud auth login、有一個 GCP 專案
# 用法：./deploy.sh            （互動式，會問缺少的設定）
set -euo pipefail
cd "$(dirname "$0")"

SERVICE="${SERVICE:-yt-course-curator}"
REGION="${REGION:-asia-east1}"

echo "═══════════════════════════════════════"
echo "  🚀 部署 YouTube 課程策展系統 → Cloud Run"
echo "═══════════════════════════════════════"

# --- 1. 專案 ---
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  read -rp "GCP 專案 ID: " PROJECT
  gcloud config set project "$PROJECT"
fi
echo "▸ 專案：$PROJECT｜區域：$REGION"

# --- 2. 啟用必要 API ---
echo "▸ 啟用必要的 GCP API（首次約需 1 分鐘）…"
gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com --project "$PROJECT" --quiet

# --- 3. Firestore（不存在才建立）---
if ! gcloud firestore databases describe --database='(default)' --project "$PROJECT" &>/dev/null; then
  echo "▸ 建立 Firestore 資料庫…"
  gcloud firestore databases create --location="$REGION" --project "$PROJECT" --quiet
fi

# --- 4. 密鑰：從本機 .env 同步到 Secret Manager ---
if [ ! -f .env ]; then
  echo "❌ 找不到 .env —— 請先 cp .env.example .env 並填入 key"
  exit 1
fi
set -a; source .env; set +a

sync_secret() {
  local name="$1" value="$2"
  [ -z "$value" ] && return 0
  if gcloud secrets describe "$name" --project "$PROJECT" &>/dev/null; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project "$PROJECT" --quiet >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --project "$PROJECT" --quiet >/dev/null
  fi
  echo "  ✓ $name"
}

echo "▸ 同步密鑰到 Secret Manager（不會出現在部署指令與 log）…"
sync_secret "anthropic-api-key"  "${ANTHROPIC_API_KEY:-}"
sync_secret "openai-api-key"     "${OPENAI_API_KEY:-}"
sync_secret "google-api-key"     "${GOOGLE_API_KEY:-}"
sync_secret "youtube-api-key"    "${YOUTUBE_API_KEY:-}"
sync_secret "line-channel-secret" "${LINE_CHANNEL_SECRET:-}"
sync_secret "line-access-token"  "${LINE_CHANNEL_ACCESS_TOKEN:-}"

# 組出 --set-secrets 參數（只掛有值的）
SECRETS=""
add_secret_ref() { [ -n "$2" ] && SECRETS="${SECRETS:+$SECRETS,}$1=$3:latest"; }
add_secret_ref ANTHROPIC_API_KEY "${ANTHROPIC_API_KEY:-}" anthropic-api-key
add_secret_ref OPENAI_API_KEY "${OPENAI_API_KEY:-}" openai-api-key
add_secret_ref GOOGLE_API_KEY "${GOOGLE_API_KEY:-}" google-api-key
add_secret_ref YOUTUBE_API_KEY "${YOUTUBE_API_KEY:-}" youtube-api-key
add_secret_ref LINE_CHANNEL_SECRET "${LINE_CHANNEL_SECRET:-}" line-channel-secret
add_secret_ref LINE_CHANNEL_ACCESS_TOKEN "${LINE_CHANNEL_ACCESS_TOKEN:-}" line-access-token

# 授權 Cloud Run 服務帳號讀取密鑰
SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null 2>&1 || true

# --- 5. 部署 ---
echo "▸ 部署中（首次約需 3-5 分鐘）…"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --memory 1Gi \
  --timeout 3600 \
  --max-instances 3 \
  --set-env-vars "GCP_PROJECT=$PROJECT,ENABLE_OAUTH=false,ENABLE_DAILY_PUSH=false" \
  --set-secrets "$SECRETS" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"

# --- 6. 排程任務 ---
echo "▸ 設定排程任務…"
create_job() {
  local name="$1" schedule="$2" path="$3"
  gcloud scheduler jobs delete "$name" --location "$REGION" --project "$PROJECT" --quiet &>/dev/null || true
  gcloud scheduler jobs create http "$name" \
    --location "$REGION" --project "$PROJECT" \
    --schedule "$schedule" --time-zone "Asia/Taipei" \
    --uri "$URL$path" --http-method POST --quiet >/dev/null
  echo "  ✓ $name（$schedule）"
}
create_job "${SERVICE}-scan-jobs" "0 * * * *" "/admin/scan-jobs"
create_job "${SERVICE}-check-links" "30 4 * * *" "/admin/check-links"

echo ""
echo "═══════════════════════════════════════"
echo "✅ 部署完成！"
echo ""
echo "   服務網址：$URL"
echo "   健康檢查：$URL/healthz"
echo ""
echo "📌 下一步：到 LINE Developers Console 設定 Webhook URL"
echo "   $URL/webhook"
echo "═══════════════════════════════════════"
