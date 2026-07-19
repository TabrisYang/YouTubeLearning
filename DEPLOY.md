# 部署指南

把系統從「本機測試」變成「真人可以用 LINE 加好友使用」。
預估時間：**約 1 小時**（多數在等 GCP 建置與 LINE 帳號審核）。

---

## 前置需求

| 項目 | 說明 | 費用 |
|---|---|---|
| Google Cloud 專案 | 跑服務與 Firestore | 有免費額度，小流量幾乎為 0 |
| `gcloud` CLI | [安裝說明](https://cloud.google.com/sdk/docs/install) | 免費 |
| LINE 官方帳號 | 使用者加好友的對象 | 免費方案含 200 則推播/月 |
| 三把 API key | 見 [README](README.md#2-準備-api-keys全部有免費額度) | 皆有免費額度 |

---

## 步驟 1：建立 LINE 官方帳號與 Channel

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 用 LINE 帳號登入
2. 建立 **Provider**（例如你的名字或品牌名）
3. 在該 Provider 下建立 **Messaging API Channel**
   - Channel 名稱＝使用者看到的官方帳號名稱
   - 類別選「教育」
4. 進入 Channel 後取得兩個值：
   - **Channel secret**（Basic settings 分頁）
   - **Channel access token**（Messaging API 分頁 → Issue）
5. 在 Messaging API 分頁關掉「**Auto-reply messages**」與「**Greeting messages**」
   （否則會和系統的回覆打架）

把兩個值填進本機 `.env`：
```bash
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
```

---

## 步驟 2：一鍵部署

```bash
gcloud auth login          # 首次
./deploy.sh
```

腳本會自動完成：啟用 GCP API → 建立 Firestore → 把 `.env` 的 key 同步到
Secret Manager（**不會出現在部署指令或 log**）→ 部署 Cloud Run → 建立兩個排程任務。

完成後會印出服務網址，例如 `https://yt-course-curator-xxxxx.run.app`。

### 關鍵設定說明（自行部署時務必比照）

| 參數 | 為什麼必要 |
|---|---|
| `--no-cpu-throttling` | **最重要**。Cloud Run 預設只在處理請求時給 CPU，背景生成任務會停擺、永遠跑不完 |
| `--timeout 3600` | 生成需 30-60 分鐘，預設 5 分鐘會被中斷 |
| `--max-instances 3` | 控制成本；使用者自備 key 存在單一 instance 記憶體，實例過多會找不到 key |
| `--memory 1Gi` | 影片評估與課綱編排的暫存需求 |
| `ENABLE_OAUTH=false` | 上線鐵則，測試通道必須關閉 |

---

## 步驟 3：接上 Webhook

1. 回到 LINE Developers Console → Messaging API 分頁
2. **Webhook URL** 填：`<你的服務網址>/webhook`
3. 按 **Verify** → 應顯示 Success
4. 開啟 **Use webhook**

現在用手機掃 QR code 加好友，傳「開課」就會有反應。

---

## 步驟 4（可選）：圖文選單

Messaging API 分頁 → Rich menu → 建立 2×2 或 2×3 選單，
每格設為「傳送文字訊息」，文字分別填：

| 建議格子 | 送出文字 |
|---|---|
| 📚 開課 | `開課` |
| 📖 今日課程 | `今日課程` |
| ✅ 完成 | `完成` |
| 📋 我的課程 | `我的課程` |
| 🧠 複習 | `複習` |
| ⚙️ 設定 | `設定` |

---

## 上線前檢查清單

- [ ] `ENABLE_OAUTH=false`，確認 LINE 選單沒有訂閱制入口
- [ ] `/healthz` 回 `{"ok": true}`
- [ ] 手機實測完整流程：開課 → 確認 → 收到完成通知 → 今日課程 → 完成 → 檢核題
- [ ] 生成失敗 → 自動退點 → 「重試」可用
- [ ] Cloud Logging 全文搜尋，確認**沒有任何明文 API key**
- [ ] `price_table.py` 的查核日期在 30 天內
- [ ] 定價依 `usage_logs` 實際平均成本校準
- [ ] 兩個 Scheduler 任務都能成功執行（Cloud Scheduler 頁面手動 Run once 測試）

---

## 常見問題

**Q：課程一直卡在「生成中」不會完成**
A：99% 是漏了 `--no-cpu-throttling`。用 `gcloud run services update <服務名> --no-cpu-throttling --region <區域>` 補上。

**Q：使用者說「API key 已過期」但他剛設定完**
A：多個 instance 造成 —— key 只存在單一 instance 的記憶體。降 `--max-instances 1`，
或未來改用 Memorystore（Redis，同樣有 TTL 不落磁碟）承接 keyvault。

**Q：Gemini 回 429**
A：免費層每日影片處理額度用完（約 2 門課/天）。升級付費層，
或引導使用者自備 Gemini key（「設定」→ 6），走各自的免費額度。

**Q：YouTube 搜尋回「今日名額已滿」**
A：每日 10,000 units 用完（約 20 門課）。課程會自動排 waitlist 隔日補跑；
要提高上限可向 Google 申請額度提升，或啟用熱門主題快取。

**Q：成本會失控嗎**
A：每次 LLM 呼叫都逐筆記入 `usage_logs`（含模型、token、美金成本）。
查 `GET /admin/llm-settings` 可看目前模型設定，`PATCH` 可即時換更便宜的模型，不需重新部署。
