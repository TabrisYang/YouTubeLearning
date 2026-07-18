"""記憶體級敏感資訊保管庫。

資安原則（v2.1）：使用者的 API key / OAuth token **一律不落地** ——
不進 Firestore、不進 log、不出現在回覆訊息。只存在於服務程序記憶體，
帶 TTL 自動過期；容器重啟即消失，使用者需重新輸入（這是刻意的取捨）。

Firestore 只存非敏感的偏好：billing_mode、byok.provider、byok.selected_model。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional


class TTLStore:
    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if time.monotonic() > expires:
                del self._data[key]
                return None
            return value

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


def _key_ttl() -> float:
    from config import get_settings

    return get_settings().byok_key_ttl_hours * 3600


# --- API keys（kind: "llm" | "youtube" | "oauth"） ---
_api_keys: TTLStore | None = None


def _keys() -> TTLStore:
    global _api_keys
    if _api_keys is None:
        _api_keys = TTLStore(_key_ttl())
    return _api_keys


def set_api_key(user_id: str, kind: str, value: str) -> None:
    _keys().set(f"{user_id}:{kind}", value)


def get_api_key(user_id: str, kind: str) -> Optional[str]:
    return _keys().get(f"{user_id}:{kind}")


def clear_api_key(user_id: str, kind: str) -> None:
    _keys().delete(f"{user_id}:{kind}")


# --- 設定流程的對話狀態（15 分鐘未完成即失效） ---
sessions = TTLStore(ttl_seconds=15 * 60)
