"""字幕抓取：zh-TW → zh-Hant → zh → zh-Hans → en → 自動字幕依序 fallback。

無字幕影片降級用 metadata（標題+描述），標記 analysis_basis="metadata"。
字幕僅供後端分析，不重製、不轉貼全文 —— 下游摘要必須是 AI 重寫。
"""
from __future__ import annotations

import logging
import re

from pipeline.models import TranscriptResult, VideoCandidate

logger = logging.getLogger(__name__)

_LANG_PRIORITY = ["zh-TW", "zh-Hant", "zh", "zh-Hans", "en"]
MAX_CHARS_FOR_EVAL = 3000  # curator 第一段只吃前 3000 字


def sample_for_eval(text: str, budget: int = MAX_CHARS_FOR_EVAL) -> str:
    """頭＋中＋尾三段取樣，取代「只看前 3000 字」。

    教學影片開頭最雷同（自介、預告），只看開頭會誤判難度與品質；
    同樣的 token 預算分給開頭 50%、中段 25%、結尾 25%，涵蓋實質內容與總結。
    """
    if len(text) <= budget:
        return text
    head = text[:budget // 2]
    mid_at = len(text) // 2
    mid = text[mid_at:mid_at + budget // 4]
    tail = text[-(budget // 4):]
    return f"{head}\n…（以下為影片中段節錄）…\n{mid}\n…（以下為影片結尾節錄）…\n{tail}"


def fetch(candidate: VideoCandidate) -> TranscriptResult:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        from config import get_settings

        proxy_url = get_settings().transcript_proxy_url
        if proxy_url:
            from youtube_transcript_api.proxies import GenericProxyConfig

            api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(
                http_url=proxy_url, https_url=proxy_url))
        else:
            api = YouTubeTranscriptApi()
        transcript_list = api.list(candidate.video_id)

        # 手動字幕優先，再退自動字幕
        for finder in (transcript_list.find_manually_created_transcript,
                       transcript_list.find_generated_transcript):
            try:
                t = finder(_LANG_PRIORITY)
                snippets = t.fetch()
                text = " ".join(s.text for s in snippets)
                if text.strip():
                    return TranscriptResult(
                        video_id=candidate.video_id, text=text,
                        language=t.language_code, analysis_basis="transcript",
                    )
            except Exception:
                continue
    except Exception as e:
        logger.info("影片 %s 字幕抓取失敗（%s），降級 metadata", candidate.video_id, type(e).__name__)

    # 降級：標題 + 描述 + 章節（章節 = 免費的內容大綱）
    meta_text = f"標題: {candidate.title}\n頻道: {candidate.channel_title}\n描述: {candidate.description}"
    chapters = extract_chapters(candidate.description)
    if chapters:
        meta_text += "\n影片章節:\n" + "\n".join(f"- {ch}" for ch in chapters)
    return TranscriptResult(
        video_id=candidate.video_id, text=meta_text,
        language="", analysis_basis="metadata",
    )


_CHAPTER_LINE = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s*[-–—]?\s*(\S.*)$", re.MULTILINE)


def extract_chapters(description: str) -> list[str]:
    """從描述欄解析時間軸章節（如「03:15 什麼是均線」），教學影片常見的免費結構訊號。"""
    return [m.group(1).strip() for m in _CHAPTER_LINE.finditer(description or "")][:20]
