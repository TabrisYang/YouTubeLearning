"""兩段式策展（產品的靈魂）。

第一段（評估，purpose=eval，便宜模型）：逐支影片 → 評估 JSON。
第二段（編排，purpose=curate，強模型）：依知識依賴排序、去重、產出課綱。
LLM 回傳非法 JSON → retry 2 次，仍失敗剔除該影片（評估）或整體失敗（編排）。
"""
from __future__ import annotations

import json
import logging

import llm
from pipeline.models import (CoursePlan, LearningContract, Lesson, QuizItem,
                             TranscriptResult, UserContext, VideoCandidate,
                             VideoEvaluation)
from pipeline.transcript import sample_for_eval

logger = logging.getLogger(__name__)

MAX_JSON_RETRIES = 2

_EVAL_JSON_BASE = """\
{"difficulty": 1-5 整數（1=零基礎可看, 5=需深厚先備知識）,
 "quality_score": 0-10 數字（講解清晰度、結構、資訊密度）,
 "topics_covered": ["涵蓋的子主題", ...],
 "teaching_style": "簡短描述（如：實作演示 / 概念講解 / 案例分析）",
 "is_outdated": true/false（內容是否已過時）"""

_EVAL_JSON_CONTRACT_EXTRA = """,
 "contract_fit": 0-10 數字（影片內容與學習契約的吻合度：涵蓋「必須涵蓋」給高分、
   觸及「排除」清單或偏離主題給低分。違反語言要求 → 最高只能給 2：
   契約寫「排除簡體」時，就算標題是繁體，只要影片內容為簡體中文
   （旁白用語、字幕、畫面文字）就算違反）,
 "is_promotional": true/false（主要目的是導流、賣課、帶單、推銷服務，而非教學；
   財經投資領域請特別嚴格：後見之明的「神準預測」、喊單、誘導加群組都算 true）"""


def _eval_system(contract: LearningContract | None) -> str:
    head = "你是課程策展的影片評估員。根據影片的字幕節錄（或 metadata）與統計資料，輸出評估 JSON。\n"
    if contract is None:
        return (head + "只輸出 JSON 物件，不要任何其他文字，格式：\n"
                + _EVAL_JSON_BASE + "}")
    return (head
            + "評估必須逐條對照這份學習契約（它是使用者確認過的選片憲法）：\n"
            + "\n".join(contract.summary_lines()) + "\n"
            + "只輸出 JSON 物件，不要任何其他文字，格式：\n"
            + _EVAL_JSON_BASE + _EVAL_JSON_CONTRACT_EXTRA + "}")


# 舊介面相容（無契約時的預設系統提示詞）
_EVAL_SYSTEM = _eval_system(None)

_CURATE_SYSTEM = """\
你是課程總編。以下是一批已評估的 YouTube 影片，請編排成由淺入深的課程。
規則：
1. 依知識依賴關係排序：先修內容在前。難度從起始難度開始整體遞增，
   相鄰兩堂可持平、禁止下降；不可為了湊堂數打亂先修鏈
2. 同主題重複的影片只留 quality_score 最高者（有 contract_fit 時以它為優先依據）
3. 每堂課輸出：AI 重新撰寫的摘要（繁體中文，120 字內，不可抄字幕）、
   2-3 條學習目標、3 題檢核題（附答案）、與上一堂的銜接說明（第一堂免）——
   銜接說明必須講清楚「這堂用到上一堂的什麼知識」，不是排在這裡的空話
4. 湊不滿要求堂數時誠實回報，在 honest_note 說明建議堂數，禁止灌水湊數
只輸出 JSON 物件，不要任何其他文字，格式：
{"lessons": [{"video_id": "...", "summary": "...", "learning_goals": ["..."],
  "quiz": [{"q": "...", "a": "..."}], "bridge_note": "..."}],
 "honest_note": "湊不滿時的說明，否則空字串"}"""


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    # 容忍前後雜訊：取第一個 { 到最後一個 }
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _complete_json(purpose: str, user_prompt: str, user_ctx: UserContext,
                   system: str, job_id: str | None, max_tokens: int = 4096) -> dict:
    """呼叫 LLM 並解析 JSON，非法時 retry（最多 MAX_JSON_RETRIES 次）。"""
    last_err: Exception | None = None
    for attempt in range(1 + MAX_JSON_RETRIES):
        text = llm.complete(purpose, [{"role": "user", "content": user_prompt}],
                            user_ctx, system=system, max_tokens=max_tokens, job_id=job_id)
        try:
            return json.loads(_extract_json(text))
        except json.JSONDecodeError as e:
            last_err = e
            logger.warning("%s 回傳非法 JSON（第 %d 次）", purpose, attempt + 1)
    raise ValueError(f"LLM 連續回傳非法 JSON: {last_err}")


# ---------- 第一段：評估 ----------

def evaluate(candidate: VideoCandidate, transcript: TranscriptResult,
             user_ctx: UserContext, job_id: str | None = None,
             contract: LearningContract | None = None) -> VideoEvaluation | None:
    """單支影片評估。JSON 失敗 retry 後仍失敗 → 回 None（剔除該影片）。"""
    prompt = (
        f"影片標題: {candidate.title}\n"
        f"頻道: {candidate.channel_title}\n"
        f"時長: {candidate.duration_sec // 60} 分鐘 | 觀看: {candidate.view_count} | 按讚: {candidate.like_count}\n"
        f"發布: {candidate.published_at}\n"
        f"分析依據: {'字幕節錄（頭/中/尾三段取樣）' if transcript.analysis_basis == 'transcript' else '僅 metadata'}\n"
        f"---\n{sample_for_eval(transcript.text)}"
    )
    try:
        data = _complete_json("eval", prompt, user_ctx, _eval_system(contract),
                              job_id, max_tokens=800)
        return VideoEvaluation(video_id=candidate.video_id,
                               analysis_basis=transcript.analysis_basis, **data)
    except Exception as e:
        logger.warning("影片 %s 評估失敗，剔除：%s", candidate.video_id, e)
        return None


_VIDEO_EVAL_HEAD = """\
請看完這支影片後進行課程策展評估。影片基本資料：
標題: {title}
頻道: {channel}｜時長: {minutes} 分鐘｜觀看: {views}｜發布: {published}
"""


def _video_eval_prompt(candidate: VideoCandidate,
                       contract: LearningContract | None) -> str:
    head = _VIDEO_EVAL_HEAD.format(
        title=candidate.title, channel=candidate.channel_title,
        minutes=candidate.duration_sec // 60, views=candidate.view_count,
        published=candidate.published_at,
    )
    if contract is not None:
        head += ("\n評估必須逐條對照這份學習契約（使用者確認過的選片憲法）：\n"
                 + "\n".join(contract.summary_lines()) + "\n")
    schema = _EVAL_JSON_BASE + (_EVAL_JSON_CONTRACT_EXTRA if contract else "") + "}"
    return head + "\n只輸出 JSON 物件，不要任何其他文字，格式：\n" + schema


def evaluate_by_video(candidate: VideoCandidate, user_ctx: UserContext,
                      job_id: str | None = None,
                      contract: LearningContract | None = None) -> VideoEvaluation | None:
    """Gemini 直接看影片評估（取代已被封鎖的字幕路線）。失敗回 None，由呼叫端降級。"""
    prompt = _video_eval_prompt(candidate, contract)
    last_err: Exception | None = None
    for attempt in range(1 + MAX_JSON_RETRIES):
        try:
            # Gemini 3 有內建思考（吃輸出預算），要給足空間避免 JSON 被截斷
            text = llm.analyze_video(candidate.url, prompt, user_ctx,
                                     max_tokens=3000, job_id=job_id)
            data = json.loads(_extract_json(text))
            return VideoEvaluation(video_id=candidate.video_id,
                                   analysis_basis="video", **data)
        except Exception as e:
            last_err = e
    logger.warning("影片 %s 的 Gemini 影片評估失敗（%s），將降級 metadata",
                   candidate.video_id, type(last_err).__name__)
    return None


# ---------- 第二段：編排 ----------

def curate(topic: str, lesson_count: int,
           candidates: list[VideoCandidate], evaluations: list[VideoEvaluation],
           user_ctx: UserContext, job_id: str | None = None,
           extra_note: str = "", contract: LearningContract | None = None) -> CoursePlan:
    by_id = {c.video_id: c for c in candidates}
    lines = []
    for ev in evaluations:
        c = by_id[ev.video_id]
        row = {
            "video_id": ev.video_id, "title": c.title, "channel": c.channel_title,
            "duration_min": c.duration_sec // 60, "difficulty": ev.difficulty,
            "quality_score": ev.quality_score, "topics_covered": ev.topics_covered,
            "teaching_style": ev.teaching_style, "is_outdated": ev.is_outdated,
            "analysis_basis": ev.analysis_basis,
        }
        if ev.contract_fit is not None:
            row["contract_fit"] = ev.contract_fit
        lines.append(json.dumps(row, ensure_ascii=False))

    contract_block = ""
    if contract is not None:
        contract_block = ("學習契約（編排必須服從；起始難度即第一堂的難度基準）:\n"
                          + "\n".join(contract.summary_lines()) + "\n")
    prompt = (
        f"學習主題: {topic}\n要求堂數: {lesson_count}\n"
        + contract_block
        + (f"特別要求: {extra_note}\n" if extra_note else "")
        + "已評估影片（每行一支）:\n" + "\n".join(lines)
    )
    data = _complete_json("curate", prompt, user_ctx, _CURATE_SYSTEM, job_id, max_tokens=8192)

    lessons: list[Lesson] = []
    for i, raw in enumerate(data.get("lessons", []), start=1):
        c = by_id.get(raw.get("video_id"))
        if c is None:                       # 模型幻覺出的 video_id → 剔除
            logger.warning("編排結果含未知 video_id %s，剔除", raw.get("video_id"))
            continue
        ev = next((e for e in evaluations if e.video_id == c.video_id), None)
        lessons.append(Lesson(
            order=len(lessons) + 1, video_id=c.video_id, video_url=c.url,
            title=c.title, channel=c.channel_title, duration_sec=c.duration_sec,
            difficulty=ev.difficulty if ev else 3,
            summary=str(raw.get("summary", ""))[:200],
            learning_goals=[str(g) for g in raw.get("learning_goals", [])][:3],
            quiz=[QuizItem(q=str(q.get("q", "")), a=str(q.get("a", "")))
                  for q in raw.get("quiz", [])][:3],
            bridge_note=str(raw.get("bridge_note", "")),
        ))

    return CoursePlan(topic=topic, requested_lessons=lesson_count,
                      lessons=lessons, honest_note=str(data.get("honest_note", "")))
