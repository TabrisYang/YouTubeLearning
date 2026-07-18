"""usage → USD 成本精算，逐筆寫 usage_logs。所有 LLM 呼叫的計費出口。"""
from __future__ import annotations

import logging

from pipeline.models import LLMCallRecord, LLMUsage

from .price_table import get_price

logger = logging.getLogger(__name__)


def cost_usd(model: str, usage: LLMUsage) -> float:
    price = get_price(model)
    if price is None:
        logger.warning("price_table 缺少模型 %s 的單價，本次成本記 0 — 請補表", model)
        return 0.0
    return round(
        usage.input_tokens / 1_000_000 * price["input"]
        + usage.output_tokens / 1_000_000 * price["output"],
        8,
    )


def record_call(user_id: str, billing_mode: str, job_id: str | None, record: LLMCallRecord) -> None:
    """逐筆寫入 usage_logs；BYOK 也照記（供使用者查詢用量），但不扣點。"""
    from storage.firestore_repo import get_repo

    get_repo().append_usage_call(
        user_id=user_id, billing_mode=billing_mode,
        job_id=job_id or "adhoc", call=record.model_dump(),
    )


def job_total_cost_usd(job_id: str) -> float:
    from storage.firestore_repo import get_repo

    log = get_repo().get_usage_log(job_id)
    return round(sum(c["cost_usd"] for c in log.get("calls", [])), 8) if log else 0.0


def usd_to_points(total_usd: float, llm_settings: dict) -> int:
    """成本(USD) × usd_to_twd × markup = 應扣點數（1 點 = NT$1），無條件進位。"""
    import math

    return math.ceil(total_usd * llm_settings["usd_to_twd"] * llm_settings["markup"])
