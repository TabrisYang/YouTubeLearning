"""環境變數與 feature flags。所有設定的唯一讀取入口。

固定不變的部署層設定放這裡（環境變數）；
可隨時後台調整的營運參數（模型、markup、匯率…）放 Firestore config/llm_settings，
由 storage.firestore_repo 讀取、billing 與 llm.router 消費。

v2.1：使用者 API key 一律不儲存（keyvault 記憶體 + TTL），移除加密金鑰設定。
"""
import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    # --- 平台自有憑證（points 模式使用） ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""          # Gemini
    youtube_api_key: str = ""         # YouTube Data API v3（平台預設；使用者可自備覆蓋）

    # --- LINE ---
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # --- GCP / Firestore ---
    gcp_project: str = ""             # 空字串 → 本機開發用 in-memory repo

    # --- 使用者自備 key 的記憶體保管 TTL（小時） ---
    byok_key_ttl_hours: float = 24.0

    # --- 字幕抓取代理（雲端 IP 常被 YouTube 擋字幕；填住宅代理 URL 可解） ---
    transcript_proxy_url: str = ""

    # --- Feature flags（環境變數層級；正式上線 ENABLE_OAUTH 必為 false） ---
    enable_oauth: bool = False
    enable_daily_push: bool = False
    dev_mode: bool = False            # 本機測試介面；未設 LINE token 時自動啟用

    # --- 訂閱制（OAuth）入口僅對這些 LINE user id 顯示（逗號分隔） ---
    developer_line_user_ids: list[str] = []

    # --- YouTube quota 預算 ---
    youtube_daily_quota: int = 10_000


@lru_cache
def get_settings() -> Settings:
    def _bool(name: str, default: str = "false") -> bool:
        return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")

    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""),
        line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
        line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
        gcp_project=os.environ.get("GCP_PROJECT", ""),
        byok_key_ttl_hours=float(os.environ.get("BYOK_KEY_TTL_HOURS", "24")),
        transcript_proxy_url=os.environ.get("TRANSCRIPT_PROXY_URL", ""),
        enable_oauth=_bool("ENABLE_OAUTH"),
        enable_daily_push=_bool("ENABLE_DAILY_PUSH"),
        dev_mode=_bool("DEV_MODE"),
        developer_line_user_ids=[
            s.strip() for s in os.environ.get("DEVELOPER_LINE_USER_IDS", "").split(",") if s.strip()
        ],
        youtube_daily_quota=int(os.environ.get("YOUTUBE_DAILY_QUOTA", "10000")),
    )


# config/llm_settings 的預設值（Firestore 無資料時 fallback；也是文件的真相來源）
DEFAULT_LLM_SETTINGS: dict = {
    "eval_model": "claude-haiku-4-5",
    "curate_model": "claude-sonnet-4-6",
    "intent_model": "claude-haiku-4-5",
    "video_eval_model": "gemini-3-flash-preview",  # 影片理解：免費層現行的 Flash 級
    "video_eval_limit": 15,                        # 只送預排序前 N 支給 Gemini（省成本）
    "markup": 3.0,
    "usd_to_twd": 32.5,
    "pricing_mode": "fixed",
    "fixed_points_per_course": 20,
    "free_daily_quota": 1,
}
