# 專案工作守則

> 給在這個 repo 工作的人與 AI。**每次推送前必讀「推送檢查清單」**。

## 🚨 推送檢查清單（每次都要跑，不可略過）

**觸發條件：使用者以任何方式提及提交或推送，就完整執行這份清單 —— 不需要他明講「照清單走」。**
包括但不限於：「推送 github」「commit」「上傳」「更新 repo」「存檔」「同步」「push 一下」。
使用者沒提但你完成了一段有意義的修改時，可以主動詢問是否要推送（不要擅自推）。

```bash
# 1. 測試全綠 —— 防「改 A 壞 B」的主防線
pytest tests/ -q

# 2. 掃描金鑰洩漏（應該無輸出）
grep -rlE "AIzaSy[A-Za-z0-9_-]{20}|sk-ant-[a-zA-Z0-9]{10}|sk-proj-[a-zA-Z0-9]{10}" \
  --exclude-dir=.venv --exclude=".env" .

# 3. 確認敏感檔案沒被加入（應該無輸出）
git status --short | grep -E "^A.*(\.env$|d4_reports/)"

# 4. 提交並推送
git add -A && git commit -m "..." && git push origin main
```

**測試沒過就不推**。掃描有輸出就停下來查清楚。

## ⛔ 永遠不進版控

| 項目 | 為什麼 | 已由 .gitignore 擋住 |
|---|---|---|
| `.env` | 裝著真實 API keys | ✅ |
| `d4_reports/` | 課綱壓測產出（含商業判斷素材） | ✅ |
| `.venv/`、`__pycache__/` | 環境產物 | ✅ |

## ⚠️ 提交前要先問使用者的內容

repo 是**公開**的（MIT）。以下內容如果要寫進程式碼或文件，**先問**：

- **商業策略**：markup 係數的推導、毛利率、定價心法、客群分析
- **成本結構**：實際成本數字、單位經濟模型
- **灰色地帶備忘**：ToS 風險討論、非官方管道的細節（如 OAuth 訂閱通道）
- **未公開的營運數據**：使用者數、轉換率、A/B 測試結果

判準：**「這段話被競爭對手或潛在使用者看到，會不會造成損害或誤解？」**

> 歷史教訓：這些內容曾被提交後才發現，需要 `git commit --amend` + `git push --force`
> 才能從歷史中清除。**推上去再刪是沒用的** —— 舊 commit 仍看得到。

## 🏗️ 架構原則（改程式前先讀）

這是**模組化單體（Modular Monolith）＋ 插件式供應商** —— 白話叫「積木式」。
防止「改 A 壞 B」靠三根柱子，動手時請維護它們：

1. **唯一出入口**：`llm/` 是全系統唯一的 LLM 呼叫出口。
   其他模組禁止 import 任何廠商 SDK 或直接打 LLM API
2. **介面契約**：`BaseRepo`（儲存）、`LLMProvider`（廠商）、Pydantic models（資料）
   —— 加新積木時實作介面，不要改主體
3. **測試網**：每個修改後跑 `pytest tests/ -q`

### 其他硬性約束

- 影片一律原站連結；內容僅供分析，摘要必須 AI 重寫，不重製、不轉貼原文
- **使用者 API key 一律不儲存**：只在 `keyvault.py` 記憶體（TTL），
  不進資料庫、不進 log、回顯一律 `security.mask()`
- **log 永不記錄使用者訊息內容**（設定流程中可能含 API key）
- `ENABLE_OAUTH` 正式上線必為 false
- Push 訊息只有兩個合法場景：課程完成通知、付費每日推播（Reply 免費，優先用）

## 🧪 改動後的驗證要求

| 改了什麼 | 除了跑測試，還要做 |
|---|---|
| README 安裝步驟 | **fresh clone 實測**：clone 到乾淨目錄照文件走一遍<br>（歷史教訓：曾發生文件承諾「2 把 key 可跑」但實際被預檢擋下） |
| `filters.py` 評分邏輯 | 跑一次 `python cli.py candidates "某主題"` 看實際排序 |
| 管線 / 提示詞 | 生成一份完整課綱，人工檢查品質 |
| `deploy.sh` | `bash -n deploy.sh` 至少驗證語法 |
| LINE 互動邏輯 | 用測試介面（`啟動測試.command`）走一次真實對話 |

## 📝 Commit message 慣例

繁體中文，第一行說「做了什麼」，內文說「為什麼」。有實測數據就附上。

```
開箱即用：自動採用已配置金鑰對應的模型

實測 fresh clone 發現 README 承諾與實際行為不符 ——
預設模型是 Claude，未填 ANTHROPIC_API_KEY 會被預檢擋下。

- config.adapt_settings_to_available_keys()：自動切換到已配置廠商的模型
- 預檢訊息區分自架者與一般使用者
```

## 🔑 本機開發

```bash
source .venv/bin/activate
pytest tests/ -q                      # 76 個測試，不需任何 key
./啟動測試.command                     # 瀏覽器測試介面（模擬 LINE）
python cli.py course "主題" 5          # CLI 生成課綱
```

三把 key 的分工見 [README](README.md#2-準備-api-keys全部有免費額度)：
搜尋（YouTube）→ 看影片（Gemini）→ 編排（Claude/GPT）。

## 📌 目前狀態與下一步

- ✅ 管線端到端可運行，76 個測試全綠
- ✅ 開源基礎建設齊備（LICENSE、README、DEPLOY.md、deploy.sh）
- ⬜ **部署上線**（`./deploy.sh` + LINE 官方帳號，約 1 小時）— 這是「程式碼 → 產品」的最後一哩
- ⬜ **真人驗證**：10 個使用者走完流程 → 問付費意願（比再加功能重要）

> 工程紀律：驗證有結果之前，**只修 bug 不加功能**。
