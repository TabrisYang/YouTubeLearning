"""呼叫各家 /models 端點，列出當下實際可用的模型（BYOK 設定流程用）。"""
from __future__ import annotations

from .providers.anthropic_p import AnthropicProvider
from .providers.google_p import GoogleProvider
from .providers.openai_p import OpenAIProvider

_CLASSES = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "google": GoogleProvider}

# BYOK 選單只列聊天用模型，過濾掉 embedding / tts / 圖像等
_CHAT_HINTS = {
    "anthropic": ("claude",),
    "openai": ("gpt-", "o1", "o3", "o4"),
    "google": ("gemini",),
}


import re

# 版本後綴：-20260101 / -2024-08-06 / -001 / -latest / -preview-06-05 等
_VERSION_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|\d{3}|latest|preview(?:-\d{2}-\d{2})?)$")


def group_by_family(models: list[str]) -> dict[str, list[str]]:
    """把完整模型 ID 依「系列」分組（去掉版本後綴），保留原始順序（最新在前）。

    例：claude-sonnet-4-6-20260101, claude-sonnet-4-6-20250901
        → {"claude-sonnet-4-6": [兩個完整 ID]}
    """
    groups: dict[str, list[str]] = {}
    for m in models:
        base = _VERSION_SUFFIX.sub("", m)
        groups.setdefault(base, []).append(m)
    return groups


def list_models(provider: str, api_key: str, limit: int | None = None) -> list[str]:
    """保留廠商 API 的回傳順序（慣例為最新在前），只過濾非聊天模型。"""
    if provider not in _CLASSES:
        raise ValueError(f"不支援的 provider: {provider}")
    models = _CLASSES[provider](api_key).list_models()
    hints = _CHAT_HINTS[provider]
    chat_models = [m for m in models if m.startswith(hints)]
    return chat_models[:limit] if limit else chat_models


def test_connection(provider: str, api_key: str, model: str) -> dict:
    """最小測試呼叫（「回覆 OK」），回報連線成功與該次 token 用量示意。"""
    from billing import meter
    from pipeline.models import LLMUsage

    p = _CLASSES[provider](api_key)
    resp = p.complete([{"role": "user", "content": "回覆 OK"}], model=model, max_tokens=16)
    usage = LLMUsage(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
    return {
        "ok": bool(resp.text.strip()),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": meter.cost_usd(model, usage),
    }
