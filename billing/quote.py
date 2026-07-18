"""生成前費用預估（dynamic 計價模式用）。

fixed 模式直接回 fixed_points_per_course；
dynamic 模式依候選影片字幕總長估 token，用 eval/curate 模型單價換算點數區間。
"""
from __future__ import annotations

from billing import meter
from billing.price_table import get_price

# 粗估：中文字幕 1 字 ≈ 1.6 tokens；輸出（評估 JSON + 課綱）相對固定
_TOKENS_PER_CHAR = 1.6
_EVAL_OUTPUT_TOKENS_PER_VIDEO = 300
_CURATE_INPUT_BASE = 6_000
_CURATE_OUTPUT_PER_LESSON = 600


def estimate_points(
    transcript_total_chars: int,
    video_count: int,
    lesson_count: int,
    llm_settings: dict,
) -> dict:
    """回傳 {"mode", "points" | "points_low"/"points_high"}。"""
    if llm_settings.get("pricing_mode", "fixed") == "fixed":
        return {"mode": "fixed", "points": llm_settings["fixed_points_per_course"]}

    eval_price = get_price(llm_settings["eval_model"]) or {"input": 0, "output": 0}
    curate_price = get_price(llm_settings["curate_model"]) or {"input": 0, "output": 0}

    eval_in = transcript_total_chars * _TOKENS_PER_CHAR
    eval_out = video_count * _EVAL_OUTPUT_TOKENS_PER_VIDEO
    curate_in = _CURATE_INPUT_BASE + video_count * 400
    curate_out = lesson_count * _CURATE_OUTPUT_PER_LESSON

    usd = (
        eval_in / 1e6 * eval_price["input"] + eval_out / 1e6 * eval_price["output"]
        + curate_in / 1e6 * curate_price["input"] + curate_out / 1e6 * curate_price["output"]
    )
    mid = meter.usd_to_points(usd, llm_settings)
    return {
        "mode": "dynamic",
        "points_low": max(1, round(mid * 0.85)),
        "points_high": max(1, round(mid * 1.25)),
    }
