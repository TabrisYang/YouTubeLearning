"""Anthropic provider（直接走 REST，避免 SDK 版本綁定）。"""
from __future__ import annotations

import httpx

from pipeline.models import LLMUsage

from .base import LLMProvider, LLMResponse

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def _headers(self) -> dict:
        if self._auth_scheme == "oauth":
            # 訂閱制 token（測試限定）：Bearer + oauth beta 標頭。
            # ⚠ 非官方開放管道，標頭規格可能隨時變動
            return {
                "Authorization": f"Bearer {self._api_key}",
                "anthropic-version": API_VERSION,
                "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json",
            }
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def complete(self, messages, model, system="", max_tokens=4096, temperature=0.3) -> LLMResponse:
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        r = httpx.post(f"{API_BASE}/messages", headers=self._headers(), json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = LLMUsage(
            input_tokens=data.get("usage", {}).get("input_tokens", 0),
            output_tokens=data.get("usage", {}).get("output_tokens", 0),
        )
        return LLMResponse(text, usage)

    def list_models(self) -> list[str]:
        r = httpx.get(f"{API_BASE}/models", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
