"""規則粗篩（不花 LLM 錢的那層）：60-75 支 → 25 支。

硬性條件：時長 4–45 分鐘。
軟性評分：按讚率、觀看/訂閱比、兩年內優先、中文優先。
中文影片不足 target 時放行英文補位（依分數）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pipeline.models import VideoCandidate

MIN_DURATION_SEC = 4 * 60
MAX_DURATION_SEC = 45 * 60
TARGET_COUNT = 25

_CJK = re.compile(r"[一-鿿]")

# 繁簡分流：兩岸用字差異最大的常用字集（出現哪邊的字多，就判定為哪種）
_SIMPLIFIED_CHARS = set("们这来时对说学习电让问间东车长万业从众发变听国图场头实币师带帮开张当录忆态"
                        "总战择办议记词证识话语误读调请课谁谢贝费资输达过还进连门闻队险页风飞马验体为")
_TRADITIONAL_CHARS = set("們這來時對說學習電讓問間東車長萬業從眾發變聽國圖場頭實幣師帶幫開張當錄憶態"
                         "總戰擇辦議記詞證識話語誤讀調請課誰謝貝費資輸達過還進連門聞隊險頁風飛馬驗體為")


def is_chinese(c: VideoCandidate) -> bool:
    return bool(_CJK.search(c.title)) or bool(_CJK.search(c.description[:200]))


def is_simplified(c: VideoCandidate) -> bool:
    """標題+描述中簡體專用字多於繁體專用字 → 判定為簡體內容。"""
    text = c.title + c.description[:200]
    s = sum(1 for ch in text if ch in _SIMPLIFIED_CHARS)
    t = sum(1 for ch in text if ch in _TRADITIONAL_CHARS)
    return s > t


def _hard_pass(c: VideoCandidate) -> bool:
    if not (MIN_DURATION_SEC <= c.duration_sec <= MAX_DURATION_SEC):
        return False
    if c.view_count < 500:          # 太冷門直接排除
        return False
    return True


def _recency_score(published_at: str, now: datetime | None = None) -> float:
    """兩年內給滿分，之後線性衰減至 4 年 0 分。"""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    now = now or datetime.now(timezone.utc)
    age_days = (now - dt).days
    if age_days <= 730:
        return 1.0
    return max(0.0, 1.0 - (age_days - 730) / 730)


def score(c: VideoCandidate, now: datetime | None = None) -> float:
    """0~1 綜合分。權重：按讚率 35% / 觀看訂閱比 25% / 新鮮度 25% / 中文 15%。"""
    like_ratio = c.like_count / c.view_count if c.view_count else 0.0
    like_score = min(like_ratio / 0.04, 1.0)          # 4% 按讚率即滿分

    if c.subscriber_count > 0:
        vs = c.view_count / c.subscriber_count
        vs_score = min(vs / 0.5, 1.0)                 # 觀看數達訂閱數一半即滿分
    else:
        vs_score = 0.3                                 # 無資料給保守分

    if not is_chinese(c):
        lang_score = 0.0
    elif is_simplified(c):
        lang_score = 0.4          # 簡中：仍算中文但降權（繁中市場優先）
    else:
        lang_score = 1.0
    return (
        0.35 * like_score
        + 0.25 * vs_score
        + 0.25 * _recency_score(c.published_at, now)
        + 0.15 * lang_score
    )


def apply(candidates: list[VideoCandidate], target: int = TARGET_COUNT,
          now: datetime | None = None, exclude_ids: set[str] | None = None,
          prefs: dict | None = None) -> list[VideoCandidate]:
    """硬篩 → 評分 → 語言偏好 → 頻道多樣性上限 → 取前 target 支。

    prefs（使用者可調，見設定選單「課程偏好」）：
      max_per_channel: int | None  同一頻道最多幾支（None = 不限）
      language: "zh_first"（預設，中文優先英文補位）| "zh_only" | "any"
      chinese_script: "any"（預設）| "no_simplified"（排除簡體，純繁中）
    exclude_ids: 進階開課時排除已上過的影片。
    """
    prefs = prefs or {}
    exclude_ids = exclude_ids or set()
    max_per_channel = prefs.get("max_per_channel")
    lang_mode = prefs.get("language", "zh_first")

    passed = [c for c in candidates if _hard_pass(c) and c.video_id not in exclude_ids]
    if lang_mode == "zh_only":
        passed = [c for c in passed if is_chinese(c)]
    if prefs.get("chinese_script") == "no_simplified":
        passed = [c for c in passed if not is_simplified(c)]
    ranked = sorted(passed, key=lambda c: score(c, now), reverse=True)

    if lang_mode == "zh_first":
        ordered = [c for c in ranked if is_chinese(c)] + [c for c in ranked if not is_chinese(c)]
    else:
        ordered = ranked

    result: list[VideoCandidate] = []
    channel_count: dict[str, int] = {}
    for c in ordered:
        if max_per_channel and channel_count.get(c.channel_id, 0) >= max_per_channel:
            continue
        result.append(c)
        channel_count[c.channel_id] = channel_count.get(c.channel_id, 0) + 1
        if len(result) >= target:
            break
    return result
