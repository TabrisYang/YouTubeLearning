# YouTube 課程策展系統 — 程式架構說明 v2.1

> 產品定位:使用者自訂主題,系統從 YouTube 搜集、篩選、排序影片,產出由淺入深的完整學習課程,透過 LINE 交付。B2C、繁中市場優先。
>
> v2.0 變更:拉式交付取代預設推播、LLM 抽象層與多認證模式(點數制/BYOK/OAuth 測試旗標)、計費引擎與可調加成公式、定價顯示策略。
>
> **v2.1 變更(資安強化)**:
> 1. **使用者 API key 一律不儲存** —— 不進 Firestore、不加密落地、不進 log。只暫放服務記憶體(keyvault,帶 TTL,預設 24h),過期或容器重啟即消失,使用者重新輸入即可。Firestore 只存非敏感偏好(品牌、選定模型)。
> 2. **設定流程改為逐步引導狀態機**:選計費模式 → 選 LLM 品牌 → 貼 key(或訂閱制,僅開發者名單可見)→ 列出該品牌「當下」最新可用模型 → 選模型 → 自動測試連線 → 成功即啟用。
> 3. **YouTube API key 同樣可自備**:同一設定介面貼 key → 驗證 → 之後搜尋用使用者自己的額度;一樣不儲存。

---

## 一、需求界定

### 功能性需求(v1 必做)
1. 使用者輸入「主題 + 堂數」發起課程生成
2. 系統搜尋 YouTube、篩選高品質影片、依難易度排序
3. 每堂課包含:影片連結(原站)、AI 重寫的 120 字內摘要、學習目標、3 題檢核題
4. 課綱一次性回覆;「今日課程」由使用者從圖文選單主動拉取(免費)
5. 學習進度追蹤(完成回報 → 發檢核題)
6. **計費雙軌**:點數制(預設)/ BYOK API key(使用者自選品牌、自付 token)
7. **測試階段限定**:OAuth 一鍵授權模式(feature flag 控制,正式上線前關閉)
8. 後台計費儀表:逐筆成本精算、模型切換、加成係數可調

### 功能性需求(v1.5 之後)
- 每日主動推播(付費方案專屬功能)
- 介面語言切換(繁中以外)
- 檢核題 AI 批改回饋
- 課程分享連結

### 非功能性需求
| 項目 | 目標 |
|---|---|
| 課程生成時間 | < 3 分鐘(非同步,完成後通知) |
| 初期規模 | < 500 使用者 |
| 月固定營運成本 | < NT$1,500(變動成本由點數/BYOK 覆蓋) |

### 硬性約束
- 影片一律以 YouTube 原站連結呈現,不下載、不重製、不轉貼字幕全文
- 字幕僅供後端分析,摘要必須是 AI 重新撰寫的濃縮描述
- **使用者的 API key 一律不儲存**(v2.1):只暫放記憶體 keyvault(TTL 過期),永不寫入資料庫、永不出現在 log 與回覆訊息中(回顯一律遮罩)
- OAuth 模式僅限開發者本人測試,**正式上線時 feature flag 必須為 off**

---

## 二、高階架構

```
使用者 (LINE App)
     │ 圖文選單:開課 / 今日課程 / 我的點數 / 設定
     ▼
┌──────────────────────────────────────────────┐
│  LINE Messaging API (Webhook / Reply 為主)    │
└─────────────────┬────────────────────────────┘
                  ▼
┌──────────────────────────────────────────────┐
│  FastAPI on Cloud Run                        │
│  ┌───────────┐   ┌─────────────────────┐     │
│  │ webhook   │   │ course_worker(背景)  │     │
│  └─────┬─────┘   └────────┬────────────┘     │
│        │                  │                   │
│  ┌─────▼──────────────────▼──────────────┐   │
│  │ Pipeline                              │   │
│  │ searcher → filters → transcript       │   │
│  │        → curator → formatter          │   │
│  └──────────────────┬────────────────────┘   │
│                     ▼                         │
│  ┌───────────────────────────────────────┐   │
│  │ llm/ 抽象層(所有 LLM 呼叫唯一出口)      │   │
│  │  ├─ provider 路由(Anthropic/OpenAI/…) │   │
│  │  ├─ 認證解析(平台key/使用者key/OAuth)  │   │
│  │  └─ usage 攔截 → billing 計費引擎      │   │
│  └───────────────────────────────────────┘   │
└─────────────────┬────────────────────────────┘
                  ▼
┌──────────────────────────────────────────────┐
│  Firestore                                   │
│  users / courses / lessons / progress        │
│  usage_logs / points_ledger / config         │
└──────────────────────────────────────────────┘
```

### 關鍵決策與取捨
| 決策 | 選擇 | 理由 |
|---|---|---|
| 介面 | LINE Bot | 台灣 B2C 滲透率、既有實作經驗 |
| 交付模式 | **拉式為主** | LINE 計費規則:Reply 免費無上限、Push 計費。免費版用圖文選單拉取,推播留給付費方案 |
| 後端 | FastAPI + Cloud Run | 熟悉、長時任務友善 |
| 資料庫 | Firestore | Serverless、免費額度、無關聯查詢需求 |
| LLM | 抽象層 + 設定檔路由 | 模型不寫死;成本控制、A/B 測試、多品牌 BYOK 都靠它 |
| 計費 | 點數制為主、BYOK 為輔 | 點數制對一般使用者零門檻;BYOK 給進階者自付 token,兩者共用同一套 usage 精算 |

---

## 三、專案結構

```
yt-course-curator/
├── main.py                  # FastAPI 進入點、LINE webhook
├── worker.py                # course_worker 背景生成流程
├── cli.py                   # 本機驗收 CLI(D1/D3)
├── config.py                # 環境變數、feature flags
├── keyvault.py              # ★ 記憶體級 key 保管(TTL);key 永不落地
├── security.py              # 顯示遮罩工具(回顯 key 一律 mask)
├── requirements.txt / Dockerfile
│
├── pipeline/
│   ├── searcher.py          # 主題展開 + YouTube Data API 搜尋
│   ├── filters.py           # 規則粗篩(不花 LLM 錢的那層)
│   ├── transcript.py        # 字幕抓取(zh-TW→zh→en fallback)
│   ├── curator.py           # 兩段式策展(評估→編排)
│   └── models.py            # Pydantic 資料模型
│
├── llm/                     # ★ 抽象層:全系統唯一的 LLM 出入口
│   ├── router.py            # 依(用途, 使用者計費模式)決定 provider+model+憑證
│   ├── providers/
│   │   ├── base.py          # 統一介面:complete(msgs) → (text, usage)
│   │   ├── anthropic_p.py / openai_p.py / google_p.py
│   ├── model_catalog.py     # 呼叫各家 /models 端點列出當下可用模型
│   └── oauth_flow.py        # ⚠ 測試階段限定,受 ENABLE_OAUTH flag 控制
│
├── billing/
│   ├── price_table.py       # 各模型單價表(手動維護,含查核日期)
│   ├── meter.py             # usage → USD 成本,逐筆寫 usage_logs
│   ├── points.py            # 點數帳本:儲值、扣點、餘額、退點
│   └── quote.py             # 生成前費用預估(動態計價模式用)
│
├── delivery/
│   ├── line_client.py       # Reply 優先;Push 僅限付費推播功能
│   ├── settings_flow.py     # ★ 設定狀態機(模式→品牌→key→模型→測試連線)
│   └── formatter.py         # Markdown 課綱 + Flex Message 版型
│
├── storage/
│   └── firestore_repo.py    # Firestore;本機自動 fallback in-memory
│
└── tests/
    └── test_pipeline.py
```

---

## 四、核心管線設計

### 1. searcher.py
- Claude 先把主題展開成 3-5 組搜尋關鍵字(含中英文、入門與進階詞彙),每組取前 15 筆
- `search.list`(100 units/次)+ `videos.list` 補 metadata
- Quota 預算:免費 10,000 units/天 ≈ 20 次課程生成/天;超量進 waitlist 隔日補跑

### 2. filters.py(規則粗篩,60-75 支 → 25 支)
- 時長 4–45 分鐘;觀看數/訂閱比與按讚率門檻;兩年內優先
- 語言:標題/描述含中文優先,不足時放行英文

### 3. transcript.py
- `youtube-transcript-api`,zh-TW → zh → en → 自動字幕依序 fallback
- 無字幕影片降級用「標題+描述+熱門留言」分析,標記 `analysis_basis: "metadata"`

### 4. curator.py(兩段式,產品的靈魂)
- **第一段(評估,用便宜模型)**:每支影片給字幕前 3000 字 + metadata,輸出 JSON:difficulty(1-5)、quality_score(1-10)、topics_covered、teaching_style、is_outdated
- **第二段(編排,用強模型)**:依知識依賴關係排序(先修在前)、同主題去重留高分者、產出每堂的學習目標/摘要/3 題檢核題/銜接說明;湊不滿堂數時誠實回報建議堂數,不灌水
- 評估/編排分別用哪個模型由 `config/llm_settings` 決定,可隨時後台切換

### 5. delivery(拉式交付)
| 情境 | 機制 | LINE 成本 |
|---|---|---|
| 課綱總覽 | 生成完成後 Push 一則通知(每課程僅 1 則) | 極低 |
| 今日課程 | 使用者點圖文選單 →Reply 回覆 | 免費 |
| 完成回報+檢核題 | 使用者傳「完成」→ Reply | 免費 |
| 每日定時推播 | Push(**v1.5 付費方案專屬**) | 計費,由訂閱價覆蓋 |

---

## 五、LLM 抽象層與認證模式

### 統一介面
所有模組一律呼叫 `llm.complete(purpose, messages, user_ctx)`,禁止直接 import 任何廠商 SDK。router 依兩個維度解析:
```
purpose(eval / curate / intent)──決定──▶ 用哪個模型(讀 config)
user_ctx.billing_mode ──────────決定──▶ 用誰的憑證、成本記到誰頭上
```

### 三種認證/計費模式
| 模式 | 憑證來源 | 成本承擔 | 可用階段 |
|---|---|---|---|
| `points`(預設) | 平台自己的 API key | 平台付,向使用者扣點 | 永久 |
| `byok_api_key` | 使用者貼上的 API key(加密存放) | 使用者自付 token | 永久 |
| `oauth` | 使用者帳號 OAuth token | 使用者訂閱額度 | **僅測試階段** |

### BYOK 設定流程(正式功能,v2.1 版)
1. 使用者在「設定」選擇計費模式 → 選擇品牌(Anthropic / OpenAI / Google)
2. 依指引貼上該品牌 API key → **只存入記憶體 keyvault(TTL 預設 24h),不落地**
3. `model_catalog` 呼叫該品牌 /models 端點,列出**當下實際可用**的最新模型供選擇
4. 用選定模型發一次最小測試呼叫(「回覆 OK」)→ 回報連線成功與該次 token 成本示意
5. 之後該使用者的所有生成走他的 key;usage 照樣記錄(供他查詢用量),但不扣點
6. key 過期/容器重啟後,開課前會偵測並引導重新輸入(Firestore 記得他的品牌與模型,重貼 key 即可)

### 自備 YouTube API key(v2.1 新增)
- 「設定」選單第 4 項:貼上 YouTube Data API v3 key → 以 1-unit 端點驗證 → 存 keyvault
- 之後該使用者的搜尋走自己的 10,000 units/天額度,不占平台 quota;同樣不儲存

### OAuth 模式(測試階段限定)
- 開發者本人測試/比較模型用的內部通道,受 `ENABLE_OAUTH` feature flag 控制
- **正式上線部署時設為 false**,相關入口在 LINE 選單直接消失,不對一般使用者開放
- 若未來官方正式開放第三方授權,再補齊正式流程並重新評估

---

## 六、計費引擎

### 成本精算(meter.py)
- 每次 LLM 呼叫的回應含 usage(input/output tokens)
- `price_table.py` 維護各模型單價(USD / 1M tokens),**含「查核日期」欄位,廠商調價時手動更新**
- 單次生成任務的所有呼叫加總 = 該單實際成本,寫入 usage_logs

### 加成公式(後台可調,不需重新部署)
```
config/llm_settings
  ├── eval_model:   "claude-haiku-4-5"
  ├── curate_model: "claude-sonnet-4-6"
  ├── markup: 3.0            # ★ 可調係數
  ├── usd_to_twd: 32.5       # 匯率,手動或定期更新
  ├── pricing_mode: "fixed"  # fixed | dynamic
  ├── fixed_points_per_course: 20
  ├── free_daily_quota: 1    # 免費使用者每日生成上限(止血閥)

點數換算:成本(USD) × usd_to_twd × markup = 應扣點數(1 點 = NT$1)
```

### 定價顯示策略
| 模式 | 使用者看到什麼 | 適用 |
|---|---|---|
| `fixed`(建議預設) | 「本次生成扣 N 點」,固定明碼 | 一般使用者;定價依 usage_logs 歷史平均成本定期校準 |
| `dynamic` | 生成前 quote.py 依候選影片內容量預估區間,使用者確認後才執行 | 主題長尾差異大、或未來企業客製情境 |

### 失敗處理與點數
- 生成失敗:已扣點全額退回 points_ledger(type: refund);平台自行吸收沉沒 token 成本
- BYOK 失敗:重試 2 次後中止並告知,提示使用者檢查 key 額度

---

## 七、資料模型(Firestore)

```
users/{line_user_id}
  ├── display_name, created_at, language: "zh-TW"
  ├── billing_mode: "points" | "byok_api_key" | "oauth"
  ├── byok: { provider, selected_model }   # 僅 BYOK;★ 不含 key(key 只在記憶體 keyvault)
  ├── points_balance: 42        # 冗餘欄位,真相在 ledger
points_ledger/{entry_id}
  ├── user_id, type: "topup"|"charge"|"refund"
  ├── points, related_job_id, created_at
usage_logs/{job_id}
  ├── user_id, course_id, billing_mode
  ├── calls: [{purpose, provider, model, input_tokens, output_tokens, cost_usd}]
  ├── total_cost_usd, charged_points, created_at
courses/{course_id}
  ├── owner, topic, lesson_count
  ├── status: "generating"|"ready"|"failed", created_at
courses/{course_id}/lessons/{order}
  ├── video_id, video_url, title, channel, duration_sec, difficulty
  ├── summary, learning_goals: [], quiz: [{q, a}]
progress/{line_user_id}_{course_id}
  ├── current_lesson, completed: [], active: true
config/llm_settings          # 見第六節
config/feature_flags
  ├── ENABLE_OAUTH: true     # ★ 正式上線改 false
  ├── ENABLE_DAILY_PUSH: false
```

---

## 八、LINE 互動契約

| 使用者輸入(圖文選單/文字) | 系統行為 |
|---|---|
| `開課 AI工作流 10` | 檢查點數/配額 → 顯示「扣 20 點,確認?」→ 建 course、背景生成、完成後 Push 通知 |
| `今日課程` | Reply 當前堂(免費) |
| `完成` | 標記完成、Reply 檢核題、預告下一堂 |
| `我的課程` / `我的點數` | Reply 清單/餘額與近期扣點紀錄 |
| `設定` | 計費模式切換;BYOK 品牌/模型設定流程;(測試期)OAuth 入口 |
| 其他文字 | 小範圍 NLU 對應到上述指令 |

---

## 九、錯誤處理

| 失敗點 | 策略 |
|---|---|
| YouTube quota 用盡 | 「今日名額已滿」+ waitlist 隔日自動補跑 |
| 字幕全抓不到 | 降級 metadata 分析並在課綱標註 |
| LLM 回傳非法 JSON | retry 2 次,仍失敗剔除該影片 |
| 生成中途容器重啟 | status 卡在 generating >10 分 → Scheduler 每小時掃描重跑 |
| 扣點後生成失敗 | 自動全額退點 + 道歉訊息 |
| BYOK key 失效/額度不足 | 明確告知原因,引導檢查,不重複燒他的重試 |

---

## 十、LINE 官方帳號成本(拉式設計後)

- 輕用量免費方案 200 則/月:因 Reply 免費,計費訊息只剩「生成完成通知」(每課程 1 則)→ 免費額度可撐每月 200 次課程生成的通知量,v1 綽綽有餘
- 未來開每日推播(付費功能):500 訂閱戶 × 30 天 ≈ 15,000 則/月 → 高用量 NT$1,200 + 加購 ≈ NT$3,000/月,攤提每人 NT$6/月,由訂閱價覆蓋

---

## 十一、建置順序(第一、二週)

| 天 | 交付物 | 驗收 |
|---|---|---|
| D1 | llm/ 抽象層骨架 + searcher + filters | 輸入主題印出 25 支候選清單;LLM 呼叫已走統一介面 |
| D2 | transcript + curator 第一段 + meter 基礎 | 每支影片有評估 JSON;每次呼叫的 cost_usd 已入 log |
| D3 | curator 第二段 + Markdown 課綱輸出 | 完整課綱,**自己照著上第一堂** |
| D4 | 三主題壓測策展品質 ★品質閘門 | 3 份課綱自己願付 NT$60;不過關回頭修提示詞,禁止進 D5 |
| D5 | Firestore + 點數帳本 + BYOK/OAuth 設定流程 | 模式切換、貼 key、列模型、測試連線全通 |
| D6 | LINE webhook + Flex Message + 圖文選單 | 手機走完「開課→確認扣點→收到課綱→今日課程→完成→檢核題」 |
| D7 | 部署 Cloud Run + 卡住任務掃描 + 後台設定驗證 | 線上改 markup/模型設定即時生效 |

---

## 十二、上線前檢查清單(從測試切正式)

- [ ] `ENABLE_OAUTH` → false,確認 LINE 選單無 OAuth 入口
- [ ] 免費每日生成配額生效(free_daily_quota)
- [ ] 定價依 usage_logs 實際平均成本重新校準
- [ ] price_table 查核日期在 30 天內
- [ ] key 不落地驗證:Firestore users 文件掃描無 key 欄位 + log 全文掃描無明文 key
- [ ] BYOK key 過期(TTL/重啟)→ 開課前偵測並引導重輸入 流程實測
- [ ] 扣點→失敗→退點 全流程實測
- [ ] LINE webhook signature 驗證開啟

## 十三、隨規模成長再審視(現在不做)

- 背景任務抽成 Cloud Tasks(>50 併發)
- 熱門主題的搜尋/評估結果快取(重複主題成本可降 ~70%)
- 金流正式串接(驗證期先用最簡收款)
- 課程模板市集、多語言介面
- 官方第三方 OAuth 開放時:補正式授權流程、重開 flag
