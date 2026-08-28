"""Pydantic 資料模型：管線各階段的資料契約。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BillingMode = Literal["points", "byok_api_key", "oauth"]


class LearningContract(BaseModel):
    """grill 式開課對話收斂出的「學習契約」——搜尋、評估、編排共同服從的規格。

    七個維度中，開課對話只主動問前三個（主題含/不含、語言、起始難度），
    其餘由 agent 依主題性質給預設值，在確認契約時一次呈現讓使用者修改。
    """
    topic: str                                  # 收斂後的主題（比使用者原話更精確）
    include: list[str] = []                     # 必須涵蓋的子主題
    exclude: list[str] = []                     # 明確排除的子主題
    language: Literal["zh_first", "zh_only", "any"] = "zh_first"
    chinese_script: Literal["any", "no_simplified"] = "any"
    start_difficulty: int = Field(default=1, ge=1, le=5)
    min_duration_min: int = Field(default=4, ge=1)
    max_duration_min: int = Field(default=45, ge=1)
    # 影片須在 N 個月內；可用小數表達天級時效（3 天 ≈ 0.1，新聞時事類用得到）
    recency_months: Optional[float] = Field(default=None, ge=0)
    channel_blocklist: list[str] = []           # 頻道名稱包含即排除
    channel_prioritize: list[str] = []          # 優先頻道（口袋名單）
    teaching_style_pref: str = ""               # 例：實作跟打型 > 投影片講解型
    # 財經/健康/加密貨幣等「高流量≠高品質」的污染領域：流量訊號降權，
    # 改以 AI 實看影片的教學誠意判斷為主導
    low_trust_popularity: bool = False

    def _recency_label(self) -> str:
        if not self.recency_months:
            return "不限（以新為佳）"
        if self.recency_months < 1:                       # 天級時效（新聞時事類）
            return f"約 {max(1, round(self.recency_months * 30.44))} 天內"
        return f"{self.recency_months:g} 個月內"

    def summary_lines(self) -> list[str]:
        """給確認訊息與 LLM 提示詞共用的人話版契約。"""
        lang = {"zh_first": "繁中優先、英文補位", "zh_only": "只要中文", "any": "不限"}[self.language]
        if self.chinese_script == "no_simplified":
            lang += "（排除簡體）"
        lines = [
            f"主題：{self.topic}",
            f"必須涵蓋：{'、'.join(self.include) if self.include else '（依主題自動）'}",
            f"排除：{'、'.join(self.exclude) if self.exclude else '無'}",
            f"語言：{lang}",
            f"起始難度：{self.start_difficulty}（1=零基礎～5=進階）",
            f"影片長度：{self.min_duration_min}–{self.max_duration_min} 分鐘",
            f"發布時間：{self._recency_label()}",
        ]
        if self.channel_blocklist:
            lines.append(f"排除頻道：{'、'.join(self.channel_blocklist)}")
        if self.channel_prioritize:
            lines.append(f"優先頻道：{'、'.join(self.channel_prioritize)}")
        if self.teaching_style_pref:
            lines.append(f"教學形式：{self.teaching_style_pref}")
        if self.low_trust_popularity:
            lines.append("品質判斷：此領域流量≠品質，以 AI 實看影片的教學誠意為主")
        return lines


class VideoCandidate(BaseModel):
    """searcher 產出、filters 消費的候選影片。"""
    video_id: str
    title: str
    description: str = ""
    channel_title: str = ""
    channel_id: str = ""
    duration_sec: int = 0
    view_count: int = 0
    like_count: int = 0
    subscriber_count: int = 0
    published_at: str = ""            # ISO 8601
    search_keyword: str = ""          # 由哪組關鍵字搜到（除錯用）

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


class TranscriptResult(BaseModel):
    video_id: str
    text: str = ""
    language: str = ""
    analysis_basis: Literal["transcript", "metadata"] = "transcript"


class VideoEvaluation(BaseModel):
    """curator 第一段（評估）輸出。"""
    video_id: str
    difficulty: int = Field(ge=1, le=5)
    quality_score: float = Field(ge=0, le=10)
    topics_covered: list[str] = []
    teaching_style: str = ""
    is_outdated: bool = False
    # 學習契約對照（有契約時才有值）：吻合度 0-10；導流/賣課/帶單片直接剔除
    contract_fit: Optional[float] = Field(default=None, ge=0, le=10)
    is_promotional: bool = False
    # video = Gemini 直接看影片（最可信）；transcript = 字幕節錄；metadata = 降級
    analysis_basis: Literal["transcript", "metadata", "video"] = "transcript"


class QuizItem(BaseModel):
    q: str
    a: str


class Lesson(BaseModel):
    """curator 第二段（編排）輸出的單堂課。"""
    order: int
    video_id: str
    video_url: str
    title: str
    channel: str = ""
    duration_sec: int = 0
    difficulty: int = 3
    summary: str = ""                 # AI 重寫，120 字內
    learning_goals: list[str] = []
    quiz: list[QuizItem] = []
    bridge_note: str = ""             # 與上一堂的銜接說明


class CoursePlan(BaseModel):
    topic: str
    requested_lessons: int
    lessons: list[Lesson]
    honest_note: str = ""             # 湊不滿堂數時的誠實說明


class UserContext(BaseModel):
    """llm.router 認證解析所需的最小使用者脈絡。"""
    user_id: str
    billing_mode: BillingMode = "points"
    byok_provider: Optional[str] = None       # anthropic | openai | google
    byok_api_key: Optional[str] = None        # 來自 keyvault 記憶體；永不落 log
    byok_model: Optional[str] = None
    oauth_token: Optional[str] = None
    oauth_source: Optional[str] = None        # "cli"（本機 Claude Code CLI）| "token"（貼 setup-token）
    oauth_model: Optional[str] = None         # CLI 模式選的模型別名（sonnet/opus/haiku）
    youtube_api_key: Optional[str] = None     # 使用者自備的 YouTube key（同樣不儲存）


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMCallRecord(BaseModel):
    """meter 寫入 usage_logs.calls[] 的單筆紀錄。"""
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
