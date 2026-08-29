"""規則粗篩（不花 LLM 錢的那層）：60-75 支 → 25 支。

硬性條件：時長 4–45 分鐘。
軟性評分：按讚率、觀看/訂閱比、兩年內優先、中文優先。
中文影片不足 target 時放行英文補位（依分數）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pipeline.models import LearningContract, VideoCandidate

MIN_DURATION_SEC = 4 * 60
MAX_DURATION_SEC = 45 * 60
TARGET_COUNT = 25
MIN_SHORTLIST_FOR_RECENCY = 8   # 時效硬篩後少於這個數就放寬（避免冷門主題被篩到空）

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


def _age_days(published_at: str, now: datetime | None = None,
              default: float = 365.0) -> float:
    """影片年齡（天）。無法解析日期時回 default —— 觀看門檻把無資料當舊片
    （default=365，吃標準門檻），時效硬篩則放行（default=0，不因缺資料誤殺）。"""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return default
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).days)


def _hard_pass(c: VideoCandidate, min_sec: int = MIN_DURATION_SEC,
               max_sec: int = MAX_DURATION_SEC, now: datetime | None = None,
               skip_views: bool = False) -> bool:
    if not (min_sec <= c.duration_sec <= max_sec):
        return False
    if skip_views:                  # 放寬階梯第一級：候選不足時先放存量門檻
        return True
    # 觀看門檻按影片年齡調整：新聞時效類的新片存量本來就低，硬門檻會殺掉
    # 時效性最好的那批 —— 7 天內免門檻，其餘看「存量 or 流速」擇一達標
    age = _age_days(c.published_at, now)
    if age <= 7:
        return True
    if c.view_count < 500 and c.view_count / max(age, 1.0) < 30:
        return False
    return True


def _months_old(published_at: str, now: datetime | None = None) -> float:
    return _age_days(published_at, now, default=0.0) / 30.44


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


def score(c: VideoCandidate, now: datetime | None = None,
          low_trust_popularity: bool = False,
          prioritize: tuple[str, ...] = ()) -> float:
    """0~1 綜合分。預設權重：按讚率 35% / 觀看訂閱比 25% / 新鮮度 25% / 中文 15%。

    low_trust_popularity（財經/健康等污染領域）：流量訊號在這些領域常是反指標
    （導流帶單片流量最高），降為 按讚率 15% / 觀看訂閱比 10% / 新鮮度 45% / 中文 30%，
    品質判斷交給後段 AI 實看影片。prioritize：頻道名稱命中口袋名單者加分。
    """
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

    w = (0.15, 0.10, 0.45, 0.30) if low_trust_popularity else (0.35, 0.25, 0.25, 0.15)
    base = (w[0] * like_score + w[1] * vs_score
            + w[2] * _recency_score(c.published_at, now) + w[3] * lang_score)
    if prioritize and any(p and p.lower() in c.channel_title.lower() for p in prioritize):
        base += 0.2               # 口袋頻道加分（分數可破 1，只影響排序）
    return base


# 放寬階梯（worker 在候選不足時逐級套用）的白話說明，回報給使用者用
RELAX_NOTES = {
    1: "放寬了觀看數門檻",
    2: "放寬了觀看數門檻與發布時間限制",
    3: "放寬了觀看數門檻、發布時間與語言限制（英文補位）",
}


def apply(candidates: list[VideoCandidate], target: int = TARGET_COUNT,
          now: datetime | None = None, exclude_ids: set[str] | None = None,
          prefs: dict | None = None,
          contract: LearningContract | None = None,
          relax: int = 0) -> list[VideoCandidate]:
    """硬篩 → 評分 → 語言偏好 → 頻道多樣性上限 → 取前 target 支。

    prefs（使用者可調，見設定選單「課程偏好」）：
      max_per_channel: int | None  同一頻道最多幾支（None = 不限）
      language: "zh_first"（預設，中文優先英文補位）| "zh_only" | "any"
      chinese_script: "any"（預設）| "no_simplified"（排除簡體，純繁中）
    exclude_ids: 進階開課時排除已上過的影片。
    contract: 學習契約（片長/時效/頻道黑白名單/語言/流量降權）；語言設定以契約優先。
    relax: 放寬階梯 0-3（候選不足時由 worker 逐級升）：
      ≥1 免觀看門檻；≥2 免時效硬篩；≥3 zh_only 放成 zh_first（英文補位）。
      「排除簡體」與「排除頻道」永不放寬——那是使用者的明確意志。
    """
    prefs = prefs or {}
    exclude_ids = exclude_ids or set()
    max_per_channel = prefs.get("max_per_channel")
    lang_mode = prefs.get("language", "zh_first")
    script_mode = prefs.get("chinese_script", "any")
    min_sec, max_sec = MIN_DURATION_SEC, MAX_DURATION_SEC
    low_trust, prioritize = False, ()
    if contract is not None:
        lang_mode, script_mode = contract.language, contract.chinese_script
        min_sec = contract.min_duration_min * 60
        max_sec = contract.max_duration_min * 60
        low_trust = contract.low_trust_popularity
        prioritize = tuple(contract.channel_prioritize)
    if relax >= 3 and lang_mode == "zh_only":
        lang_mode = "zh_first"

    passed = [c for c in candidates
              if _hard_pass(c, min_sec, max_sec, now, skip_views=relax >= 1)
              and c.video_id not in exclude_ids]
    if contract is not None and contract.channel_blocklist:
        blocked = [b.lower() for b in contract.channel_blocklist if b]
        passed = [c for c in passed
                  if not any(b in c.channel_title.lower() for b in blocked)]
    if contract is not None and contract.recency_months and relax < 2:
        fresh = [c for c in passed
                 if _months_old(c.published_at, now) <= contract.recency_months]
        if len(fresh) >= MIN_SHORTLIST_FOR_RECENCY:   # 篩到太少就放寬，靠評分排序
            passed = fresh
    if lang_mode == "zh_only":
        passed = [c for c in passed if is_chinese(c)]
    if script_mode == "no_simplified":
        passed = [c for c in passed if not is_simplified(c)]
    ranked = sorted(passed, key=lambda c: score(c, now, low_trust, prioritize),
                    reverse=True)

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
