"""LINE Messaging API。Reply 優先（免費）；Push 僅限「生成完成通知」與付費推播功能。"""
from __future__ import annotations

import logging

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"

# --- dev 模式：訊息不送 LINE，收進 outbox 給本機測試介面輪詢 ---
DEV_OUTBOX: list[dict] = []


def dev_mode() -> bool:
    s = get_settings()
    return s.dev_mode or not s.line_channel_access_token


def drain_dev_outbox() -> list[dict]:
    out = DEV_OUTBOX[:]
    DEV_OUTBOX.clear()
    return out


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_settings().line_channel_access_token}",
        "Content-Type": "application/json",
    }


def _normalize(messages: list) -> list[dict]:
    return [{"type": "text", "text": m} if isinstance(m, str) else m for m in messages][:5]


def reply(reply_token: str, messages: list) -> None:
    """Reply 免費無上限 —— 一律優先用這個。"""
    msgs = _normalize(messages)
    if dev_mode():
        DEV_OUTBOX.extend(msgs)
        return
    r = httpx.post(f"{API_BASE}/message/reply", headers=_headers(),
                   json={"replyToken": reply_token, "messages": msgs}, timeout=15)
    if r.status_code >= 400:
        logger.error("LINE reply 失敗 %d: %s", r.status_code, r.text[:300])


def push(user_id: str, messages: list, *, reason: str) -> None:
    """Push 計費。目前唯一合法 reason: course_ready（每課程 1 則）。
    daily_push 需 ENABLE_DAILY_PUSH flag（v1.5 付費方案）。"""
    if reason == "daily_push" and not get_settings().enable_daily_push:
        logger.warning("daily_push 未啟用，略過 push")
        return
    if reason not in ("course_ready", "daily_push"):
        raise ValueError(f"不允許的 push reason: {reason}")
    msgs = _normalize(messages)
    if dev_mode():
        DEV_OUTBOX.extend(msgs)
        return
    r = httpx.post(f"{API_BASE}/message/push", headers=_headers(),
                   json={"to": user_id, "messages": msgs}, timeout=15)
    if r.status_code >= 400:
        logger.error("LINE push 失敗 %d: %s", r.status_code, r.text[:300])
