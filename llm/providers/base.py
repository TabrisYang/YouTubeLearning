"""Provider 統一介面：complete(msgs) → (text, usage)。

llm/ 以外的模組禁止 import 任何廠商 SDK 或直接打廠商 API。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pipeline.models import LLMUsage


class LLMResponse:
    def __init__(self, text: str, usage: LLMUsage):
        self.text = text
        self.usage = usage


class LLMProvider(ABC):
    """各家 provider 的最小共同介面。"""

    name: str = "base"

    def __init__(self, api_key: str, auth_scheme: str = "api_key"):
        """auth_scheme: "api_key"（一般）| "oauth"（訂閱制 token，改用 Bearer 標頭）"""
        if not api_key:
            raise ValueError(f"{self.name}: 缺少 API key")
        self._api_key = api_key  # 永不 log、永不出現在錯誤訊息
        self._auth_scheme = auth_scheme

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        model: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """messages: [{"role": "user"|"assistant", "content": str}, ...]"""

    @abstractmethod
    def list_models(self) -> list[str]:
        """呼叫該廠商 /models 端點，回傳當下實際可用的模型 id 清單。"""
