"""敏感資訊顯示規則。

v2.1 起 API key 一律不儲存（見 keyvault.py），本模組只剩顯示用工具：
任何要回顯給使用者的 key 一律先過 mask()，絕不回明文。
"""
from __future__ import annotations


def mask(key: str) -> str:
    """sk-ant-…xY9Z 形式的遮罩。"""
    if len(key) <= 8:
        return "****"
    return f"{key[:6]}…{key[-4:]}"
