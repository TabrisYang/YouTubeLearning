"""主題展開 + YouTube Data API 搜尋。

流程：LLM 把主題展開成 3-5 組關鍵字（中英、入門與進階）
     → search.list 每組取前 15 筆（100 units/次）
     → videos.list + channels.list 補 metadata（各 1 unit/50 筆）
Quota：免費 10,000 units/天 ≈ 20 次課程生成/天，超量由呼叫端進 waitlist。
"""
from __future__ import annotations

import json
import logging

import httpx

import llm
from config import get_settings
from pipeline.models import LearningContract, UserContext, VideoCandidate

logger = logging.getLogger(__name__)

YT_API = "https://www.googleapis.com/youtube/v3"

_EXPAND_SYSTEM = """\
你是課程策展助手。使用者給你一個學習主題（可能附學習契約），請展開成 3-5 組
YouTube 搜尋關鍵字。只輸出 JSON 陣列，不要任何其他文字。

核心原則：用「創作者下標題的語言」，不是主題的書面描述。
影片標題寫的是產品名、口語動詞與吸睛詞，沒有人會複述抽象主題：
・具體實體優先：品牌/產品/工具/指標名（GPT、Claude、n8n、布林通道、MACD）
・口語動詞：教學、實測、開箱、評測、比較、解析、發表會、實戰
・禁止把主題原文照抄或改寫成關鍵字
・禁止在關鍵字寫死年份或具體版本號（你的知識可能過時）——用品牌名不帶版本
  （「GPT 實測」而非「GPT-4 實測 2024」），時效交給系統的時間過濾器處理

範例輸入：LLM 及大型語言模型的最新技術進展
範例輸出：["GPT-5 實測", "Claude 新功能 評測", "Gemini 模型 比較", "AI 發表會 解析", "大語言模型 突破"]

範例輸入：AI工作流
範例輸出：["n8n 教學", "AI automation workflow", "AI agent 入門", "Make 自動化 實戰", "LangChain tutorial"]

若附有「學習契約」，關鍵字展開是它的第一道執行者：
・「搜尋種子」裡的詞優先入選（那是使用者親口提過的實體）
・關鍵字服從「必須涵蓋」的語意，禁止產生指向「排除」清單的關鍵字
・語言為 zh_only 時只出中文關鍵字；zh_first 出中文為主、至多 1 組英文
・起始難度 3 以上時偏進階實戰詞彙，1-2 時偏入門教學詞彙"""


class QuotaExceeded(Exception):
    """YouTube 每日 quota 用盡；呼叫端應回覆「今日名額已滿」並排 waitlist。"""


def expand_topic(topic: str, user_ctx: UserContext, job_id: str | None = None,
                 advanced: bool = False,
                 contract: LearningContract | None = None) -> list[str]:
    """LLM 展開主題為 3-5 組搜尋關鍵字。advanced=True 時偏向進階/深入詞彙。

    非法輸出重試 2 次並帶錯誤回饋（這是管線第一步，死在這裡整門課直接失敗）。
    """
    base = (f"{topic}（使用者已完成入門課程，請給進階、深入、實戰導向的關鍵字）"
            if advanced else topic)
    if contract is not None:
        base += "\n學習契約：\n" + "\n".join(contract.summary_lines())

    last_err: Exception | None = None
    for _ in range(3):
        content = base if last_err is None else (
            base + f"\n\n（你上一次的輸出無法解析：{str(last_err)[:200]}。"
                   "請只輸出一個合法的 JSON 字串陣列。）")
        text = llm.complete(
            "intent",
            [{"role": "user", "content": content}],
            user_ctx,
            system=_EXPAND_SYSTEM,
            max_tokens=2000,   # Gemini 內建思考吃輸出預算，給足空間防 JSON 截斷
            job_id=job_id,
        )
        try:
            keywords = json.loads(_extract_json(text))
            if not isinstance(keywords, list) or not keywords:
                raise ValueError(f"結果不是非空陣列: {text[:200]}")
            return [str(k) for k in keywords[:5]]
        except Exception as e:
            last_err = e
    raise ValueError(f"主題展開連續失敗: {last_err}")


def _extract_json(text: str) -> str:
    """容忍模型輸出被 ```json fence 包住或前後帶雜訊：取第一個 [ 到最後一個 ]。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _parse_duration(iso: str) -> int:
    """ISO 8601 duration（PT1H2M3S）→ 秒。"""
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def search_videos(keywords: list[str], per_keyword: int = 15,
                  quota_used: int = 0, api_key: str | None = None) -> tuple[list[VideoCandidate], int]:
    """依關鍵字搜尋並補齊 metadata。回傳 (候選清單, 本次消耗的 quota units)。

    api_key：使用者自備的 YouTube key（用他的額度）；未提供則用平台 key。
    """
    api_key = api_key or get_settings().youtube_api_key
    if not api_key:
        raise RuntimeError("未設定 YouTube API key（平台未配置，且使用者未自備）")

    budget = get_settings().youtube_daily_quota
    used = quota_used
    seen: dict[str, VideoCandidate] = {}

    with httpx.Client(timeout=30) as client:
        for kw in keywords:
            if used + 100 > budget:
                raise QuotaExceeded(f"quota 將超出預算（已用 {used}/{budget}）")
            r = client.get(f"{YT_API}/search", headers={"x-goog-api-key": api_key}, params={
                "part": "snippet", "q": kw, "type": "video",
                "maxResults": per_keyword, "relevanceLanguage": "zh-Hant",
                "safeSearch": "moderate",
            })
            r.raise_for_status()
            used += 100
            for item in r.json().get("items", []):
                vid = item["id"]["videoId"]
                if vid in seen:
                    continue
                sn = item["snippet"]
                seen[vid] = VideoCandidate(
                    video_id=vid, title=sn["title"], description=sn.get("description", ""),
                    channel_title=sn.get("channelTitle", ""), channel_id=sn.get("channelId", ""),
                    published_at=sn.get("publishedAt", ""), search_keyword=kw,
                )

        # videos.list 補時長/觀看/按讚（每 50 筆 1 unit）
        ids = list(seen)
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            r = client.get(f"{YT_API}/videos", headers={"x-goog-api-key": api_key}, params={
                "part": "contentDetails,statistics", "id": ",".join(batch),
            })
            r.raise_for_status()
            used += 1
            for item in r.json().get("items", []):
                c = seen[item["id"]]
                c.duration_sec = _parse_duration(item["contentDetails"].get("duration", ""))
                stats = item.get("statistics", {})
                c.view_count = int(stats.get("viewCount", 0))
                c.like_count = int(stats.get("likeCount", 0))

        # channels.list 補訂閱數（供 views/subscriber 比）
        ch_ids = list({c.channel_id for c in seen.values() if c.channel_id})
        subs: dict[str, int] = {}
        for i in range(0, len(ch_ids), 50):
            batch = ch_ids[i:i + 50]
            r = client.get(f"{YT_API}/channels", headers={"x-goog-api-key": api_key}, params={
                "part": "statistics", "id": ",".join(batch),
            })
            r.raise_for_status()
            used += 1
            for item in r.json().get("items", []):
                subs[item["id"]] = int(item.get("statistics", {}).get("subscriberCount", 0))
        for c in seen.values():
            c.subscriber_count = subs.get(c.channel_id, 0)

    logger.info("搜尋完成：%d 組關鍵字 → %d 支候選，quota 用量 %d", len(keywords), len(seen), used)
    return list(seen.values()), used


def fetch_top_comments(video_id: str, api_key: str | None = None, n: int = 5) -> list[str]:
    """熱門留言（1 unit/次）：metadata 降級分析的補強訊號。失敗（留言關閉等）回空。"""
    api_key = api_key or get_settings().youtube_api_key
    if not api_key:
        return []
    try:
        r = httpx.get(f"{YT_API}/commentThreads", headers={"x-goog-api-key": api_key}, params={
            "part": "snippet", "videoId": video_id,
            "maxResults": n, "order": "relevance", "textFormat": "plainText",
        }, timeout=15)
        r.raise_for_status()
        return [item["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:200]
                for item in r.json().get("items", [])]
    except Exception:
        return []
