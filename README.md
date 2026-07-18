# YouTube 課程策展系統

使用者自訂主題，系統從 YouTube 搜集、篩選、排序影片，產出由淺入深的完整學習課程，透過 LINE 交付。
完整架構見專案根目錄的架構說明文件 v2.0。

## 最快的測試方式：雙擊啟動

在 Finder 雙擊 **`啟動測試.command`**，瀏覽器自動開啟 **http://127.0.0.1:8787/dev** —— 一個模擬 LINE 的聊天介面。走的是與正式 webhook **完全相同**的指令處理邏輯，訊息只進瀏覽器、不會發到 LINE。

兩種測法（對應兩種計費模式）：

| | key 從哪來 | 測的是誰的體驗 |
|---|---|---|
| **A. 介面中輸入（免 .env）** | 「設定」→ 自備 API key + YouTube key | BYOK 使用者：自付 token、不扣點 |
| **B. 填 `.env`** | 平台的 `ANTHROPIC_API_KEY`、`YOUTUBE_API_KEY` | 點數制使用者（一般大眾預設）：先 `儲值 100`（dev 限定指令）再開課扣點 |

之後流程相同：開課 → 確認 → 等完成通知 → 今日課程 → 完成 → 答案。

## 本機快速開始

```bash
cd yt-course-curator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 ANTHROPIC_API_KEY 與 YOUTUBE_API_KEY
```

### D1 驗收：印出候選清單
```bash
export $(grep -v '^#' .env | xargs)
python cli.py candidates "AI工作流"
```

### D3 驗收：產出完整課綱（Markdown + 逐筆成本）
```bash
python cli.py course "AI工作流" 10 > course.md
```

### 跑測試（離線，不需任何 API key）
```bash
pytest tests/ -v
```

### 啟動 API（LINE webhook + 後台設定）
```bash
uvicorn main:app --reload --port 8080
# webhook:  POST /webhook（需 LINE signature）
# 後台設定: GET/PATCH /admin/llm-settings（改 markup / 模型即時生效）
# 任務掃描: POST /admin/scan-jobs（Cloud Scheduler 每小時打一次：
#           重跑卡住 >10 分的 generating、補跑 waitlisted，沿用原扣點）
# 死鏈巡檢: POST /admin/check-links（Cloud Scheduler 每日一次：
#           失效/轉私人的影片標記 dead，課程列表顯示 ⚠️）
```

## 保護機制（皆已實作 + 測試）

| 機制 | 行為 |
|---|---|
| 免費每日配額 | points 模式每人每日 `free_daily_quota` 次（預設 1，後台可調）；BYOK/訂閱不受限 |
| YouTube quota | 平台額度每日累計，預估不足時課程進 waitlist 隔日補跑（不退點不重扣）；使用者自備 key 免檢 |
| 卡住任務 | 容器重啟導致 generating 卡住 >10 分 → `/admin/scan-jobs` 重跑 |
| 生成失敗 | 已扣點全額自動退回 + 通知 |
| 清除 key | 設定選單第 4 項：三種 key 立即從記憶體移除、切回點數制 |

## 模組地圖

| 目錄 | 職責 |
|---|---|
| `llm/` | **全系統唯一的 LLM 出入口**。router 依（purpose × billing_mode）決定模型與憑證；usage 一律攔截記帳 |
| `pipeline/` | searcher（主題展開+搜尋）→ filters（規則粗篩）→ transcript（字幕 fallback）→ curator（兩段式策展） |
| `billing/` | price_table（單價表，含查核日期）、meter（usage→USD）、points（帳本）、quote（預估） |
| `delivery/` | LINE Reply 優先（免費）；Push 僅限完成通知與付費推播 |
| `storage/` | Firestore；未設 GCP_PROJECT 時自動用 in-memory（本機開發） |
| `keyvault.py` | 記憶體級 key 保管（TTL 過期）；**使用者 key 永不落地** |
| `delivery/settings_flow.py` | 設定狀態機：模式 → 品牌 → 貼 key → 選最新模型 → 測試連線 |

## 重要約束（動手前先讀）

1. `llm/` 以外禁止 import 任何廠商 SDK 或直接打 LLM API
2. 影片一律原站連結；字幕僅供分析，摘要必須 AI 重寫
3. **使用者 API key 一律不儲存**：只在 keyvault 記憶體（TTL 24h），不進 Firestore、不進 log、回顯一律遮罩
4. `ENABLE_OAUTH` 正式上線必為 false；訂閱制入口只對 `DEVELOPER_LINE_USER_IDS` 顯示
5. Push 訊息只有兩個合法場景：課程完成通知、付費每日推播

## 部署（Cloud Run）

```bash
gcloud run deploy yt-course-curator --source . \
  --region asia-east1 --allow-unauthenticated \
  --set-env-vars GCP_PROJECT=<your-project> \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-key:latest,LINE_CHANNEL_SECRET=line-secret:latest,..."
```
