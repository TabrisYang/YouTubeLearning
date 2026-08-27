"""測試全域護欄：單元測試不需任何 key、離線可跑。

grill 式開課訪談（intake_flow）讓引導流程會嘗試呼叫 LLM，這裡預設把
llm.complete 換成直接 raise —— 走 LLM 的路徑在測試裡一律進退路
（固定三題流程），不打網路。需要測 LLM 行為的測試自行
monkeypatch.setattr(llm, "complete", 假函式) 覆蓋即可。
（llm.analyze_video 不在此擋：它的路由測試在 provider 層自行 mock。）
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    import llm

    def _blocked(*args, **kwargs):
        raise RuntimeError("LLM disabled in unit tests（測試不打真實 API）")

    monkeypatch.setattr(llm, "complete", _blocked)
