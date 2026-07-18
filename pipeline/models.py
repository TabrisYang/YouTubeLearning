"""Pydantic 資料模型：管線各階段的資料契約。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BillingMode = Literal["points", "byok_api_key", "oauth"]


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
