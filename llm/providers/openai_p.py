"""OpenAI provider。"""
from __future__ import annotations

import httpx

from pipeline.models import LLMUsage

from .base import LLMProvider, LLMResponse

API_BASE = "https://api.openai.com/v1"


class OpenAIProvider(LLMProvider):
    name = "openai"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"}

    def complete(self, messages, model, system="", max_tokens=4096, temperature=0.3) -> LLMResponse:
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        r = httpx.post(f"{API_BASE}/chat/completions", headers=self._headers(), json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"].get("content", "") or ""
        usage = LLMUsage(
            input_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=data.get("usage", {}).get("completion_tokens", 0),
        )
        return LLMResponse(text, usage)

    def list_models(self) -> list[str]:
        r = httpx.get(f"{API_BASE}/models", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
