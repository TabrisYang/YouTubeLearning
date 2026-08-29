"""Google Gemini provider（generativelanguage REST）。"""
from __future__ import annotations

import httpx

from pipeline.models import LLMUsage

from .base import LLMProvider, LLMResponse

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GoogleProvider(LLMProvider):
    name = "google"

    def _auth(self) -> tuple[dict, dict]:
        """回傳 (params, headers)。api_key 一律走 x-goog-api-key header ——
        禁止放 query param：URL 會出現在 httpx 的例外訊息與各層 log 裡，
        任何 4xx/5xx 都等於把 key 明文外洩（違反「log 永不含 key」鐵則）。"""
        if self._auth_scheme == "oauth":
            return {}, {"Authorization": f"Bearer {self._api_key}"}
        return {}, {"x-goog-api-key": self._api_key}

    def complete(self, messages, model, system="", max_tokens=4096, temperature=0.3) -> LLMResponse:
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        params, headers = self._auth()
        r = httpx.post(
            f"{API_BASE}/models/{model}:generateContent",
            params=params,
            headers=headers,
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata", {})
        usage = LLMUsage(
            input_tokens=meta.get("promptTokenCount", 0),
            output_tokens=meta.get("candidatesTokenCount", 0),
        )
        return LLMResponse(text, usage)

    def complete_with_video(self, youtube_url: str, prompt: str, model: str,
                            max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        """影片理解：Gemini 官方支援直接以 YouTube 網址為輸入（file_data.file_uri）。

        這是唯一「合法看完整支影片」的管道 —— 不下載、不爬字幕，
        取代已被 YouTube 封鎖的 transcript 抓取路線。
        """
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"file_data": {"file_uri": youtube_url}},
                    {"text": prompt},
                ],
            }],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                # 低解析度：影片 token 約降至 1/3，課程評估不需要逐格畫質
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            },
        }
        params, headers = self._auth()
        r = httpx.post(
            f"{API_BASE}/models/{model}:generateContent",
            params=params, headers=headers, json=payload,
            timeout=300,  # 影片處理較久
        )
        if r.status_code == 400 and "mediaResolution" in r.text:
            # 模型不支援此參數 → 退回標準解析度重試一次
            payload["generationConfig"].pop("mediaResolution")
            r = httpx.post(f"{API_BASE}/models/{model}:generateContent",
                           params=params, headers=headers, json=payload, timeout=300)
        r.raise_for_status()
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata", {})
        usage = LLMUsage(
            input_tokens=meta.get("promptTokenCount", 0),
            output_tokens=meta.get("candidatesTokenCount", 0),
        )
        return LLMResponse(text, usage)

    def list_models(self) -> list[str]:
        params, headers = self._auth()
        r = httpx.get(f"{API_BASE}/models", params=params, headers=headers, timeout=30)
        r.raise_for_status()
        return [m["name"].removeprefix("models/") for m in r.json().get("models", [])]
