"""「設定」逐步引導流程（對話狀態機）。

流程（v2.1）：
  設定 → ① 選計費模式（點數 / 自備 API key / 訂閱制※僅開發者可見 / YouTube key）
      → ② 選 LLM 品牌
      → ③ 貼上 API key（只進記憶體 keyvault，不儲存）
      → ④ 列出該品牌「當下」最新可用模型，選一個
      → ⑤ 自動測試連線（最小呼叫）→ 成功即啟用

YouTube key 走同一介面：貼 key → 驗證 → 啟用（同樣不儲存）。
對話狀態放 keyvault.sessions（15 分鐘未完成自動失效）；
Firestore 只寫非敏感偏好：billing_mode、byok.provider、byok.selected_model。
"""
from __future__ import annotations

import logging

import httpx

import keyvault
import security
from config import get_settings
from llm import model_catalog
from storage.firestore_repo import get_repo

logger = logging.getLogger(__name__)

import re as _re

_BRANDS = ["anthropic", "openai", "google"]

# 模型名稱長相：ASCII 英數 + -._:[]，3~64 字（擋掉「訂閱」「取消」誤觸等中文輸入）
_MODEL_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{2,63}$")
_BRAND_LABELS = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI (GPT)", "google": "Google (Gemini)"}
MODEL_LIST_LIMIT = 8

_SECURITY_NOTE = (
    "🔒 資安說明：你的 key 不會被儲存 —— 只暫放服務記憶體 "
    f"{get_settings().byok_key_ttl_hours:.0f} 小時且系統重啟即消失，"
    "不寫入資料庫、不出現在任何紀錄。過期後重新輸入即可。"
)


def _oauth_available(user_id: str) -> bool:
    """訂閱制入口的可見條件：flag 開啟，且是開發者（名單內，或本機測試介面）。"""
    from delivery import line_client

    return get_settings().enable_oauth and (
        user_id in get_settings().developer_line_user_ids or line_client.dev_mode()
    )


def start(user_id: str) -> list[str]:
    """進入設定選單。"""
    user = get_repo().get_user(user_id) or {}
    mode_label = {"points": "點數制", "byok_api_key": "自備 API key", "oauth": "訂閱制"}.get(
        user.get("billing_mode", "points"), "點數制")
    options = [
        "1️⃣ 點數制（預設，免設定）",
        "2️⃣ 自備 API key（自付 token，不扣點）",
        "3️⃣ 設定 YouTube API key（自備搜尋額度）",
        "4️⃣ 清除我的 key（立即從記憶體移除）",
        "5️⃣ 課程偏好（頻道多樣性、語言、繁簡）",
        "6️⃣ 設定 Gemini key（影片深度分析）",
    ]
    keyvault.sessions.set(user_id, {"step": "menu"})
    return [
        f"⚙️ 目前計費模式：{mode_label}\n\n請選擇（回覆數字）：\n" + "\n".join(options)
        + "\n\n隨時回覆「取消」離開設定"
    ]


def in_session(user_id: str) -> bool:
    return keyvault.sessions.get(user_id) is not None


def handle(user_id: str, text: str) -> list[str]:
    """處理設定會話中的一則訊息，回傳要 Reply 的訊息。"""
    text = text.strip()
    if text in ("取消", "cancel"):
        keyvault.sessions.delete(user_id)
        return ["已離開設定 👋"]

    session = keyvault.sessions.get(user_id) or {"step": "menu"}
    step = session["step"]

    try:
        if step == "menu":
            return _step_menu(user_id, text, session)
        if step == "brand":
            return _step_brand(user_id, text, session)
        if step == "llm_key":
            return _step_llm_key(user_id, text, session)
        if step == "model":
            return _step_model(user_id, text, session)
        if step == "model_version":
            return _step_model_version(user_id, text, session)
        if step == "yt_key":
            return _step_yt_key(user_id, text)
        if step == "oauth_token":
            return _step_oauth_token(user_id, text, session)
        if step == "oauth_model":
            return _step_oauth_model(user_id, text, session)
        if step == "pref_channel":
            return _step_pref_channel(user_id, text, session)
        if step == "pref_lang":
            return _step_pref_lang(user_id, text, session)
        if step == "pref_script":
            return _step_pref_script(user_id, text, session)
        if step == "gemini_key":
            return _step_gemini_key(user_id, text)
    except Exception:
        logger.exception("設定流程錯誤（step=%s）", step)  # 注意：永不 log 使用者輸入內容
        keyvault.sessions.delete(user_id)
        return ["設定過程發生錯誤，請重新輸入「設定」再試一次 🙏"]

    keyvault.sessions.delete(user_id)
    return ["已離開設定。"]


def _step_menu(user_id: str, text: str, session: dict) -> list[str]:
    if text == "1":
        keyvault.sessions.delete(user_id)
        get_repo().upsert_user(user_id, {"billing_mode": "points"})
        return ["✅ 已切換為點數制。輸入「開課 主題 堂數」即可開始！"]

    if text == "2":
        session["step"] = "brand"
        keyvault.sessions.set(user_id, session)
        brands = "\n".join(f"{i}. {_BRAND_LABELS[b]}" for i, b in enumerate(_BRANDS, 1))
        return [f"請選擇 LLM 品牌（回覆數字）：\n{brands}"]

    if text == "3":
        session["step"] = "yt_key"
        keyvault.sessions.set(user_id, session)
        return [
            "請貼上你的 YouTube Data API v3 key。\n\n"
            "還沒有 key？兩步驟免費申請：\n"
            "1️⃣ 啟用 API：\n"
            "https://console.cloud.google.com/apis/library/youtube.googleapis.com\n"
            "2️⃣ 建立金鑰：\n"
            "https://console.cloud.google.com/apis/credentials\n\n"
            + _SECURITY_NOTE
        ]

    if text == "5":
        prefs = (get_repo().get_user(user_id) or {}).get("prefs", {})
        cur_ch = prefs.get("max_per_channel")
        cur_lang = {"zh_first": "中文優先", "zh_only": "只要中文", "any": "不限"}.get(
            prefs.get("language", "zh_first"))
        session["step"] = "pref_channel"
        keyvault.sessions.set(user_id, session)
        return [
            "🎛️ 課程偏好（影響選片依據）\n"
            f"目前：同頻道上限 {cur_ch or '不限'}｜語言 {cur_lang}\n\n"
            "第 1 題：同一個頻道最多選幾支影片？\n"
            "1. 最多 2 支（推薦，來源更多元）\n"
            "2. 最多 4 支\n"
            "3. 不限（品質優先，可能整門課同一頻道）"
        ]

    if text == "4":
        for kind in ("llm", "youtube", "oauth", "gemini"):
            keyvault.clear_api_key(user_id, kind)
        keyvault.sessions.delete(user_id)
        get_repo().upsert_user(user_id, {"billing_mode": "points"})
        return [
            "🗑️ 已清除你的所有 key（LLM / YouTube / Gemini / 訂閱 token），"
            "計費模式切回點數制。\n提醒：如果 key 本身要作廢，請到該廠商後台撤銷。"
        ]

    if text == "6":
        user = get_repo().get_user(user_id) or {}
        mode = user.get("billing_mode", "points")
        if mode != "byok_api_key" and not keyvault.get_api_key(user_id, "gemini"):
            intro = ("ℹ️ 你目前是「" + ("訂閱制" if mode == "oauth" else "點數制")
                     + "」—— 影片深度分析已由平台提供，**不需要**自備 Gemini key。\n"
                     "仍想用自己的額度？照下面步驟貼上即可（可隨時清除）。\n\n")
        else:
            intro = ("🎬 BYOK 模式的影片深度分析需自備 Gemini key"
                     "（否則課程改用較弱的 metadata 分析）。\n"
                     "免費層每天約可深度分析 2 門課，個人使用綽綽有餘。\n\n")
        session["step"] = "gemini_key"
        keyvault.sessions.set(user_id, session)
        return [
            intro
            + "兩步驟免費申請：\n"
            "1️⃣ 開啟 https://aistudio.google.com/apikey\n"
            "2️⃣ 登入 Google 帳號 → Create API key → 複製貼過來\n\n"
            + _SECURITY_NOTE
        ]

    return ["請回覆選單上的數字，或「取消」離開。"]


def _step_brand(user_id: str, text: str, session: dict) -> list[str]:
    idx = int(text) - 1 if text.isdigit() else -1
    if not 0 <= idx < len(_BRANDS):
        return ["請回覆 1-3 選擇品牌，或「取消」離開。"]
    session.update(step="llm_key", provider=_BRANDS[idx])
    keyvault.sessions.set(user_id, session)
    # 訂閱制入口在「貼 key」這一步出現（僅開發者可見）
    oauth_hint = ("\n\n🧪 或回覆「訂閱」改用此品牌的訂閱制連線（開發者測試）"
                  if _oauth_available(user_id) else "")
    return [
        f"已選擇 {_BRAND_LABELS[_BRANDS[idx]]}。\n請直接貼上你的 API key。\n\n"
        + _SECURITY_NOTE + oauth_hint
    ]


def _step_llm_key(user_id: str, text: str, session: dict) -> list[str]:
    provider = session["provider"]

    # 開發者測試：改走訂閱制
    if text == "訂閱" and _oauth_available(user_id):
        # 優先：偵測本機已登入的 Claude Code CLI（系統完全不經手憑證）
        if provider == "anthropic":
            from llm.providers import claude_cli
            from llm.providers.claude_cli import CLI_MODELS, ClaudeCLIProvider

            if ClaudeCLIProvider.is_available():
                # 別名探測法：實測各別名路由到哪個模型（＝方案掛保證可用；快取 6 小時）
                resolved = claude_cli.discover_models()
                if resolved:
                    aliases = list(resolved)
                    listing = "\n".join(
                        f"{i}. {a} → {resolved[a]}（實測可用）"
                        for i, a in enumerate(aliases, 1))
                    note = "以下是用你的訂閱方案實測確認可用的模型"
                else:
                    # 探測全失敗（例如逾時）：退回靜態別名清單，選定後照常測試
                    aliases, listing = list(CLI_MODELS), "\n".join(
                        f"{i}. {m}" for i, m in enumerate(CLI_MODELS, 1))
                    note = "（無法完成模型探測，列出通用別名，選定後會立即測試）"
                session.update(step="oauth_model", models=aliases, resolved=resolved)
                keyvault.sessions.set(user_id, session)
                return [
                    "✅ 偵測到本機的 Claude Code CLI！\n"
                    "將直接使用它的登入狀態 —— 憑證由 CLI 自行管理，"
                    "本系統從頭到尾不經手、也讀不到。\n\n"
                    f"{note}：\n{listing}\n\n"
                    "回覆數字選擇（別名自動跟隨最新版本），"
                    "或直接輸入完整模型名稱鎖定特定版本。"
                ]

        # 退回：貼 setup-token（CLI 不在同一台機器時，例如部署到伺服器後）
        session["step"] = "oauth_token"
        keyvault.sessions.set(user_id, session)
        howto = ""
        if provider == "anthropic":
            howto = ("取得方式：終端機執行 claude setup-token，"
                     "登入你的 Claude 帳號後把產出的 token 貼過來。\n")
        return [
            f"（開發者測試）本機未偵測到已登入的 CLI，"
            f"請貼上 {_BRAND_LABELS[provider]} 的訂閱制 token。\n"
            + howto
            + "⚠️ 此管道非官方開放，僅供開發者本人測試，可能隨時失效。\n"
            + _SECURITY_NOTE
        ]

    try:
        models = model_catalog.list_models(provider, text, limit=MODEL_LIST_LIMIT)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return ["❌ 這把 key 驗證失敗（401/403），請確認後重貼，或「取消」離開。"]
        return [f"❌ 查詢模型清單失敗（HTTP {e.response.status_code}），請稍後重貼一次。"]
    except Exception:
        return ["❌ 無法連線到該品牌的 API，請確認 key 是否正確後重貼。"]

    if not models:
        return ["❌ 這把 key 查不到任何可用模型，請確認權限後重貼。"]

    # key 只進記憶體保管庫
    keyvault.set_api_key(user_id, "llm", text)

    # 兩層選擇：先列「模型系列」，選定後再列該系列的可用版本
    groups = model_catalog.group_by_family(models)
    families = list(groups)[:MODEL_LIST_LIMIT]
    session.update(step="model", families=families, groups=groups)
    keyvault.sessions.set(user_id, session)
    listing = "\n".join(
        f"{i}. {fam}" + (f"（{len(groups[fam])} 個版本）" if len(groups[fam]) > 1 else "")
        for i, fam in enumerate(families, 1)
    )
    return [
        f"✅ key 已收到（{security.mask(text)}），查到目前可用的模型系列：\n{listing}\n\n"
        "請回覆數字選擇系列，或直接輸入完整模型名稱（清單外的也可以）。"
    ]


def _step_model(user_id: str, text: str, session: dict) -> list[str]:
    """第一層：選模型系列。單一版本直接測試；多版本進入第二層選版本。"""
    families = session.get("families", [])
    groups = session.get("groups", {})

    if not text.isdigit():
        if not _MODEL_NAME_RE.match(text):
            return [f"「{text}」看起來不是模型名稱。請回覆 1-{len(families)} 選擇系列，"
                    "或輸入完整模型名稱，或「取消」離開。"]
        # 直接輸入完整模型名稱 → 跳過分層，直接測試
        return _finish_byok(user_id, session, text)

    idx = int(text) - 1
    if not 0 <= idx < len(families):
        return [f"請回覆 1-{len(families)}、直接輸入模型名稱，或「取消」離開。"]
    family = families[idx]
    versions = groups.get(family, [family])

    if len(versions) == 1:
        return _finish_byok(user_id, session, versions[0])

    # 第二層：列出該系列的可用版本（最新在前）
    session.update(step="model_version", versions=versions, family=family)
    keyvault.sessions.set(user_id, session)
    listing = "\n".join(
        f"{i}. {v}" + ("（最新）" if i == 1 else "") for i, v in enumerate(versions, 1)
    )
    return [f"「{family}」目前可用的版本：\n{listing}\n\n請回覆數字選擇版本。"]


def _step_model_version(user_id: str, text: str, session: dict) -> list[str]:
    """第二層：選定版本 → 測試連線。"""
    versions = session.get("versions", [])
    if text.isdigit():
        idx = int(text) - 1
        if not 0 <= idx < len(versions):
            return [f"請回覆 1-{len(versions)} 選擇版本，或「取消」離開。"]
        model = versions[idx]
    elif _MODEL_NAME_RE.match(text):
        model = text  # 也接受直接輸入完整名稱
    else:
        return [f"「{text}」看起來不是模型名稱。請回覆 1-{len(versions)} 選擇版本，或「取消」離開。"]
    return _finish_byok(user_id, session, model)


def _finish_byok(user_id: str, session: dict, model: str) -> list[str]:
    """測試連線 → 成功即啟用 BYOK 模式（Firestore 只存品牌與模型，不含 key）。"""
    provider = session["provider"]
    api_key = keyvault.get_api_key(user_id, "llm")
    if not api_key:
        keyvault.sessions.delete(user_id)
        return ["key 已過期，請重新輸入「設定」再走一次流程。"]

    try:
        result = model_catalog.test_connection(provider, api_key, model)
    except Exception:
        return [f"❌ 用 {model} 測試連線失敗，換一個試試（回覆數字或模型名稱），或「取消」離開。"]

    keyvault.sessions.delete(user_id)
    get_repo().upsert_user(user_id, {
        "billing_mode": "byok_api_key",
        "byok": {"provider": provider, "selected_model": model},  # 不含 key
    })
    return [
        f"🎉 測試連線成功！\n模型：{model}\n"
        f"該次測試用量：輸入 {result['input_tokens']} / 輸出 {result['output_tokens']} tokens"
        f"（≈ ${result['cost_usd']:.6f} USD）\n\n"
        "之後的課程生成都會走你的 key、不扣點。輸入「開課 主題 堂數」開始！"
    ]


def _step_yt_key(user_id: str, text: str) -> list[str]:
    # 驗證：打一個 1 unit 的最便宜端點
    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/i18nLanguages",
            headers={"x-goog-api-key": text}, params={"part": "snippet"}, timeout=15,
        )
        r.raise_for_status()
    except Exception:
        return ["❌ 這把 YouTube API key 驗證失敗，請確認已啟用 YouTube Data API v3 後重貼。"]

    keyvault.set_api_key(user_id, "youtube", text)
    keyvault.sessions.delete(user_id)
    return [
        f"✅ YouTube API key 驗證成功（{security.mask(text)}）！\n"
        "之後的影片搜尋會用你自己的額度（每天 10,000 units，約 20 次課程生成）。"
    ]


def _step_pref_channel(user_id: str, text: str, session: dict) -> list[str]:
    mapping = {"1": 2, "2": 4, "3": None}
    if text not in mapping:
        return ["請回覆 1-3 選擇，或「取消」離開。"]
    session.update(step="pref_lang", max_per_channel=mapping[text])
    keyvault.sessions.set(user_id, session)
    return [
        "第 2 題：影片語言偏好？\n"
        "1. 中文優先（預設，不夠時用英文補）\n"
        "2. 只要中文（可能找不滿堂數）\n"
        "3. 不限（純看品質分數）"
    ]


def _step_pref_lang(user_id: str, text: str, session: dict) -> list[str]:
    mapping = {"1": "zh_first", "2": "zh_only", "3": "any"}
    if text not in mapping:
        return ["請回覆 1-3 選擇，或「取消」離開。"]
    session.update(step="pref_script", language=mapping[text])
    keyvault.sessions.set(user_id, session)
    return [
        "第 3 題：簡體中文影片？\n"
        "1. 排除（純繁中，可選內容較少）\n"
        "2. 接受但降權（預設，繁中優先、簡中補位）"
    ]


def _step_pref_script(user_id: str, text: str, session: dict) -> list[str]:
    mapping = {"1": "no_simplified", "2": "any"}
    if text not in mapping:
        return ["請回覆 1-2 選擇，或「取消」離開。"]
    prefs = {
        "max_per_channel": session.get("max_per_channel"),
        "language": session.get("language", "zh_first"),
        "chinese_script": mapping[text],
    }
    keyvault.sessions.delete(user_id)
    get_repo().upsert_user(user_id, {"prefs": prefs})
    ch = prefs["max_per_channel"]
    lang = {"zh_first": "中文優先", "zh_only": "只要中文", "any": "不限"}[prefs["language"]]
    script = {"no_simplified": "排除簡中", "any": "簡中降權補位"}[prefs["chinese_script"]]
    return [
        f"✅ 課程偏好已更新！\n同頻道上限：{ch or '不限'}｜語言：{lang}｜繁簡：{script}\n"
        "下次開課（含進階開課）就會套用。\n\n"
        "📐 目前的選片依據供參考：按讚率 35%＋觀看/訂閱比 25%＋新鮮度 25%＋語言 15%"
        "（繁中滿分、簡中 0.4 倍），時長限 4-45 分鐘，再由 AI 逐支深度分析後編排。"
    ]


def _step_gemini_key(user_id: str, text: str) -> list[str]:
    """驗證並載入使用者自備的 Gemini key（最小文字呼叫，不看影片省額度）。"""
    from llm.providers.google_p import GoogleProvider

    try:
        model = get_repo().get_llm_settings().get("video_eval_model", "gemini-3-flash-preview")
        resp = GoogleProvider(text).complete(
            [{"role": "user", "content": "回覆 OK"}], model=model, max_tokens=50)
        if not resp.text.strip():
            raise ValueError("空回應")
    except Exception:
        return ["❌ 這把 Gemini key 驗證失敗，請確認是從 aistudio.google.com/apikey 產生的後重貼，"
                "或「取消」離開。"]

    keyvault.set_api_key(user_id, "gemini", text)
    keyvault.sessions.delete(user_id)
    return [
        f"✅ Gemini key 驗證成功（{security.mask(text)}）！\n"
        "之後課程的影片深度分析會用你自己的額度（免費層每天約 2 門課）。"
    ]


def _step_oauth_model(user_id: str, text: str, session: dict) -> list[str]:
    """訂閱制（本機 CLI）：選模型 → 用 CLI 發最小測試呼叫 → 啟用。"""
    from billing import meter
    from llm.providers.claude_cli import ClaudeCLIProvider
    from pipeline.models import LLMUsage

    models = session.get("models", [])
    resolved: dict = session.get("resolved", {})
    if text.isdigit():
        idx = int(text) - 1
        if not 0 <= idx < len(models):
            return [f"請回覆 1-{len(models)}、直接輸入模型名稱，或「取消」離開。"]
        model = models[idx]
    elif _MODEL_NAME_RE.match(text):
        model = text  # 直接輸入完整模型名稱（如 claude-opus-4-8），由測試呼叫驗證
    else:
        return [f"「{text}」看起來不是模型名稱。請回覆 1-{len(models)} 選擇，"
                "或輸入完整模型名稱（例如 claude-opus-4-8），或「取消」離開。"]

    if model in resolved:
        # 探測時已真實跑過一次 → 免重測，直接啟用（省時間也省訂閱額度）
        detail = f"{model} → {resolved[model]}（探測時已實測）"
    else:
        try:
            resp = ClaudeCLIProvider().complete(
                [{"role": "user", "content": "回覆 OK"}], model=model, max_tokens=16)
        except Exception as e:
            msg = str(e)
            if "login" in msg.lower() or "api key" in msg.lower():
                return ["❌ CLI 尚未登入。請在終端機執行 claude，完成 /login 後再試一次（回覆數字）。"]
            return [
                f"❌ 用 {model} 測試失敗 —— 可能是模型名稱有誤，"
                "或你的訂閱方案不含此模型。\n換一個試試（回覆數字或模型名稱），或「取消」離開。"
            ]
        usage = LLMUsage(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
        detail = (f"{model}｜測試用量：輸入 {usage.input_tokens} / 輸出 {usage.output_tokens} tokens"
                  f"（若按 API 計費約 ${meter.cost_usd(model, usage):.6f} USD，實際計入你的訂閱額度）")

    keyvault.sessions.delete(user_id)
    get_repo().upsert_user(user_id, {
        "billing_mode": "oauth",
        "oauth_provider": "anthropic",
        "oauth_source": "cli",           # 憑證由本機 CLI 自管，系統不持有任何東西
        "oauth_model": model,
    })
    return [
        f"🎉 訂閱制連線成功！（本機 Claude Code CLI）\n模型：{detail}\n\n"
        "之後的課程生成都走你的訂閱、不扣點。\n"
        "（提速說明：逐支影片的評估階段自動用 haiku，課綱編排才用你選的模型）\n"
        "輸入「開課」開始！"
    ]


def _step_oauth_token(user_id: str, text: str, session: dict | None = None) -> list[str]:
    provider = (session or {}).get("provider", "")
    keyvault.set_api_key(user_id, "oauth", text)
    keyvault.sessions.delete(user_id)
    get_repo().upsert_user(user_id, {"billing_mode": "oauth", "oauth_source": "token",
                                     "oauth_provider": provider})  # 僅記品牌，不含 token
    label = _BRAND_LABELS.get(provider, provider or "所選品牌")
    return [f"✅（開發者測試）{label} 訂閱制 token 已載入記憶體，之後的生成走此連線。"]
