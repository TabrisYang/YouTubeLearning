"""各模型單價表（USD / 1M tokens），手動維護。

★ 廠商調價時更新數字並改 checked_date；上線前檢查清單要求查核日期在 30 天內。
★ 未列於表中的模型 → cost 記 0 並打 warning log，提醒補表（不擋呼叫）。
"""
from __future__ import annotations

PRICE_TABLE: dict[str, dict] = {
    # --- Anthropic ---
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00,  "checked_date": "2026-07-17"},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "checked_date": "2026-07-17"},
    "claude-opus-4-8":   {"input": 15.00, "output": 75.00, "checked_date": "2026-07-17"},
    # --- OpenAI ---
    "gpt-4o-mini":       {"input": 0.15,  "output": 0.60,  "checked_date": "2026-07-17"},
    "gpt-4o":            {"input": 2.50,  "output": 10.00, "checked_date": "2026-07-17"},
    # --- Google ---
    "gemini-2.0-flash":  {"input": 0.10,  "output": 0.40,  "checked_date": "2026-07-17"},
    "gemini-2.5-pro":    {"input": 1.25,  "output": 10.00, "checked_date": "2026-07-17"},
    # preview 定價未定，先以 Flash 級行情估（正式定價公布後更新）
    "gemini-3-flash-preview": {"input": 0.30, "output": 2.50, "checked_date": "2026-07-17"},
}


# 訂閱制 CLI 用的模型別名 → 對應正式模型的單價（僅供用量示意，訂閱制不實際計費）
_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5",
}


def get_price(model: str) -> dict | None:
    """完全比對優先；別名次之；最後嘗試去掉日期後綴的寬鬆比對。"""
    model = _ALIASES.get(model, model)
    if model in PRICE_TABLE:
        return PRICE_TABLE[model]
    for known in PRICE_TABLE:
        if model.startswith(known):
            return PRICE_TABLE[known]
    return None
