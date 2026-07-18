"""⚠ 測試階段限定，受 ENABLE_OAUTH flag 控制。

僅供開發者本人測試用的內部通道；正式上線部署時 flag 必為 off。
"""
from __future__ import annotations

from config import get_settings


def assert_enabled() -> None:
    if not get_settings().enable_oauth:
        raise PermissionError("OAuth 模式未啟用（ENABLE_OAUTH=false）")


def start_authorization(provider: str, user_id: str) -> str:
    """回傳授權 URL。實作待各廠商流程確認後補上（測試期手動貼 token 亦可）。"""
    assert_enabled()
    raise NotImplementedError("OAuth 授權流程尚未實作；測試期請於設定流程直接貼 token")
