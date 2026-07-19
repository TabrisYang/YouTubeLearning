# YouTube 課程策展系統

使用者輸入一個想學的主題，系統從 YouTube 搜集影片、篩選品質、判斷難度，
編排成一門**由淺入深的完整課程**（含 AI 重寫的摘要、學習目標、檢核題），透過 LINE 交付。

> YouTube 什麼都有，就是沒有課表、進度、考試 —— 這個系統補上那些。

**核心技術特色**：AI 逐支「看完」候選影片再評估（Gemini 影片理解），不是靠標題猜內容。

---

## 30 秒理解它做什麼

```
你：開課
系統：想學什麼主題？        → 你：AI工作流
系統：目前程度？（1新手/2有基礎/3進階）→ 你：1
系統：幾堂課？              → 你：5
系統：[約 30-60 分鐘後] 🎓 課程完成！5 堂，15/25 支候選影片經 AI 深度分析
你：今日課程 → 收到第 1 堂（影片連結 + 摘要 + 學習目標）
你：完成     → 收到 3 題檢核題 + 🔥 連續學習 1 天 + 下一堂預告
```

---

## 快速開始

### 1. 安裝

```bash
git clone https://github.com/TabrisYang/YouTubeLearning.git
cd YouTubeLearning
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. 準備 API keys（全部有免費額度）

| Key | 用途 | 去哪申請 | 必要性 |
|---|---|---|---|
| `YOUTUBE_API_KEY` | 搜尋影片（免費 10,000 units/天 ≈ 20 門課） | ① [啟用 API](https://console.cloud.google.com/apis/library/youtube.googleapis.com) → ② [建立金鑰](https://console.cloud.google.com/apis/credentials) | **必要** |
| `GOOGLE_API_KEY` | Gemini 影片理解（免費層每天約 2 門課） | [Google AI Studio](https://aistudio.google.com/apikey) → Create API key | **必要** |
| `ANTHROPIC_API_KEY` | 課綱編排（也可改用 OpenAI / Gemini） | [Anthropic Console](https://console.anthropic.com/settings/keys) | 三選一 |
| `OPENAI_API_KEY` | 同上（替代方案） | [OpenAI Platform](https://platform.openai.com/api-keys) | 三選一 |

把 key 填進 `.env` 對應欄位即可。想改用 OpenAI/Gemini 當編排模型，改 `config.py` 的
`DEFAULT_LLM_SETTINGS`（或部署後用 `PATCH /admin/llm-settings` 熱調）。

> 💡 只想先看看能不能跑？填 `YOUTUBE_API_KEY` + `GOOGLE_API_KEY` 兩把就能生成課程
> （編排也可走 Gemini）。單元測試則完全不需要任何 key。

### 3. 啟動測試介面

**macOS**：Finder 雙擊 `啟動測試.command`
**其他系統**：
```bash
export $(grep -v '^#' .env | xargs) && export DEV_MODE=true
uvicorn main:app --port 8787
```

瀏覽器開 **http://127.0.0.1:8787/dev** —— 一個模擬 LINE 的聊天介面，
走的是與正式 webhook **完全相同**的邏輯，訊息只進瀏覽器、不會發到 LINE。

輸入「開課」開始，或先點「⚙️ 設定」試 BYOK 流程。

### 4. 跑測試（不需任何 key）

```bash
pytest tests/ -q     # 73 個單元測試
```

---

## 指令一覽（LINE 與測試介面通用）

| 指令 | 功能 |
|---|---|
| `開課` | 逐步引導：主題 → 程度 → 堂數 → 確認 |
| `開課 <主題> <堂數>` | 一行式（熟手用） |
| `今日課程` / `課程列表` / `第 3 堂` | 上課、看全部堂數、跳看任一堂 |
| `完成` / `答案` / `複習` | 完成回報→檢核題、對答案、隨機抽考已完成內容 |
| `讚` / `爛` | 評價剛完成的課（累積策展品質數據） |
| `進階開課` | 同主題往更深學（自動排除已上過的影片） |
| `重試` | 上次生成失敗後一鍵重新生成 |
| `我的課程` / `我的點數` / `設定` | 進度查詢、餘額、設定選單 |
| 其他文字 | LLM 智慧客服（知道你的狀態，能回答「為什麼失敗」） |

---

## 架構

```
LINE / 測試介面
    ↓
FastAPI (main.py) ──→ course_worker（背景生成）
    ↓                      ↓
  pipeline/  searcher → filters → 三層評估 → curator
                              ↓
              ┌───────────────────────────────┐
              │ 1. 字幕可用 → 文字評估         │
              │ 2. 前 15 支 → Gemini 看影片 ⭐ │
              │ 3. 其餘/失敗 → metadata+章節+留言│
              └───────────────────────────────┘
    ↓
  llm/ 抽象層（全系統唯一 LLM 出入口，usage 逐筆記帳）
    ↓
  Firestore（未設 GCP_PROJECT 時自動用 in-memory）
```

### 模組地圖

| 目錄 | 職責 |
|---|---|
| `llm/` | **全系統唯一的 LLM 出入口**。router 依（purpose × billing_mode）決定模型與憑證 |
| `pipeline/` | searcher（主題展開+搜尋）→ filters（規則粗篩）→ transcript → curator（兩段式策展） |
| `billing/` | price_table（單價表含查核日期）、meter（usage→USD）、points（帳本）、quote（預估） |
| `delivery/` | LINE Reply 優先（免費）；settings_flow（設定狀態機）；dev_ui（測試介面） |
| `storage/` | Firestore repo；本機自動 fallback in-memory |
| `keyvault.py` | 記憶體級 key 保管（TTL）；**使用者 key 永不落地** |

完整設計見 [ARCHITECTURE.md](ARCHITECTURE.md)。

### 為什麼是「AI 看影片」而不是抓字幕

初版用 `youtube-transcript-api` 抓字幕分析，實測發現 **YouTube 已封鎖非官方字幕請求**
（連住宅 IP 都回 `IpBlocked`）。與其走代理繞過（灰色地帶且是軍備競賽），
改用 **Gemini 官方支援的 YouTube 影片理解** —— 合法、穩定，而且模型是真的「看完」整支影片
（含畫面），評估品質比只讀字幕前 3000 字更可信。

---

## 選片依據（透明公開）

**硬篩**：時長 4–45 分鐘、觀看數 ≥500
**評分**：按讚率 35% ＋ 觀看/訂閱比 25% ＋ 新鮮度 25%（兩年內滿分）＋ 語言 15%（繁中滿分、簡中 0.4）
**多樣性**：同頻道上限可調（預設不限，建議設 2）
**AI 評估**：逐支判斷 difficulty(1-5)、quality_score(0-10)、是否過時
**AI 編排**：依知識依賴排序、同主題去重、湊不滿堂數時誠實回報不灌水

使用者可在「設定 → 5」調整頻道多樣性、語言、繁簡偏好。

---

## 保護機制

| 機制 | 行為 |
|---|---|
| 使用者 key 不落地 | 只在記憶體 keyvault（TTL 24h），不進資料庫、不進 log、回顯遮罩 |
| 每日配額 | points 模式每人每日 `free_daily_quota` 次；BYOK/訂閱不受限 |
| YouTube quota | 每日累計追蹤，不足時課程進 waitlist 隔日補跑（不退點不重扣） |
| 卡住任務 | generating 卡住 >10 分 → `/admin/scan-jobs` 重跑（沿用原扣點） |
| 生成失敗 | 已扣點全額自動退回 + 白話原因 + 「重試」一鍵重來 |
| 死鏈巡檢 | `/admin/check-links` 每日掃描，失效影片標記 ⚠️ |

---

## 部署（Cloud Run）

詳細步驟見 **[DEPLOY.md](DEPLOY.md)**，或直接執行：

```bash
./deploy.sh
```

⚠️ **重要**：Cloud Run 預設只在處理請求時分配 CPU，會讓背景生成任務停擺 ——
部署腳本已包含 `--no-cpu-throttling`，自行部署時請務必加上。

---

## 開發約束（貢獻前先讀）

1. `llm/` 以外禁止 import 任何廠商 SDK 或直接打 LLM API
2. 影片一律原站連結；內容僅供分析，摘要必須 AI 重寫，不重製、不轉貼原文
3. **使用者 API key 一律不儲存**，回顯一律遮罩，log 永不記錄訊息內容（可能含 key）
4. `ENABLE_OAUTH` 正式上線必為 false
5. Push 訊息只有兩個合法場景：課程完成通知、付費每日推播（Reply 免費，優先用）
6. 改完跑 `pytest tests/ -q`，全綠才提交

---

## 授權

[MIT](LICENSE) © TabrisYang
