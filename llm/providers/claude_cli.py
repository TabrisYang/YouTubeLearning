"""Claude 訂閱制 provider：把本機已登入的 Claude Code CLI 包成 LLM 供應商。

⚠ 測試階段限定（ENABLE_OAUTH gate）。
- 憑證由 CLI 自行管理（~/.claude），本系統從頭到尾不經手、也讀不到
- 對話 = 開 `claude -p` 子進程，用量計入開發者自己的訂閱額度
- 只在 CLI 與本服務同機時可用（開發機）；Cloud Run 上偵測不到 → 退回貼 token 模式
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pipeline.models import LLMUsage

from .base import LLMProvider, LLMResponse

# 訂閱制走模型別名（由 CLI 解析為當下最新版本）
CLI_MODELS = ["sonnet", "opus", "haiku"]
# 別名探測的候選清單（探測失敗的直接略過，例如方案不含 opus）
PROBE_ALIASES = ["sonnet", "opus", "haiku", "fable"]

_TIMEOUT_SEC = 600   # 編排階段 opus 產完整課綱 JSON 可能超過 3 分鐘，給足裕度
_PROBE_TIMEOUT_SEC = 60
_DISCOVER_CACHE_TTL = 6 * 3600   # 探測會燒一點訂閱額度，成功結果快取 6 小時

_discover_lock = threading.Lock()
_discover_cache: tuple[float, dict[str, str]] | None = None


def resolve_alias(alias: str) -> str | None:
    """別名探測法：對別名發最小成本的真實請求，讀 modelUsage 得知實際路由到的模型 ID。

    這是計費層記錄的真實路由結果（不是叫模型自報 ID），不會幻覺；
    cwd 切到系統暫存目錄，避開專案 CLAUDE.md 省 token。失敗回 None。
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", "hi", "--model", alias, "--output-format", "json"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC,
            cwd=tempfile.gettempdir(),
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            return None
        return next(iter(data.get("modelUsage") or {}), None)
    except Exception:
        return None


def discover_models(force: bool = False) -> dict[str, str]:
    """併發探測所有別名，回傳 {別名: 實測可用的具體模型 ID}。

    只有探測成功的別名會出現在結果 —— 這些是「你的訂閱方案實測真的能用」的模型。
    全部失敗時回空 dict 且不寫快取（避免污染），由呼叫端 fallback 靜態別名清單。
    """
    global _discover_cache
    with _discover_lock:
        now = time.monotonic()
        if not force and _discover_cache and now < _discover_cache[0]:
            return dict(_discover_cache[1])

        with ThreadPoolExecutor(max_workers=len(PROBE_ALIASES)) as pool:
            resolved = dict(zip(PROBE_ALIASES, pool.map(resolve_alias, PROBE_ALIASES)))
        results = {alias: mid for alias, mid in resolved.items() if mid}

        if results:
            _discover_cache = (now + _DISCOVER_CACHE_TTL, results)
        return dict(results)


class ClaudeCLIProvider(LLMProvider):
    name = "claude_cli"

    def __init__(self, api_key: str = "local", auth_scheme: str = "api_key"):
        # 不需要憑證；傳佔位字串滿足基底類別的非空檢查
        super().__init__(api_key or "local", auth_scheme)

    @staticmethod
    def is_available() -> bool:
        """本機是否裝有 claude CLI。是否已登入留給實際測試呼叫驗證。"""
        return shutil.which("claude") is not None

    def complete(self, messages, model, system="", max_tokens=4096, temperature=0.3) -> LLMResponse:
        # CLI 無 messages 陣列介面：把多輪對話攤平成單一 prompt
        prompt = "\n\n".join(
            (f"[assistant]\n{m['content']}" if m["role"] == "assistant" else m["content"])
            for m in messages
        )
        cmd = ["claude", "-p", "--model", model, "--output-format", "json"]
        if system:
            cmd += ["--append-system-prompt", system]

        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=_TIMEOUT_SEC)
        if proc.returncode != 0:
            # stderr 可能含登入提示，取前 200 字即可（不會有憑證內容）
            raise RuntimeError(f"claude CLI 執行失敗: {proc.stderr[:200]}")

        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI 回報錯誤: {str(data.get('result', ''))[:200]}")

        usage_raw = data.get("usage", {})
        # CLI 的 usage 把快取 token 分開列（cache_creation/cache_read）——
        # 不加總的話，命中快取的呼叫會被記成 0 成本（曾造成 curate 計費歸零）
        usage = LLMUsage(
            input_tokens=(usage_raw.get("input_tokens", 0)
                          + usage_raw.get("cache_creation_input_tokens", 0)
                          + usage_raw.get("cache_read_input_tokens", 0)),
            output_tokens=usage_raw.get("output_tokens", 0),
        )
        result_text = data.get("result", "")
        if result_text and not usage.output_tokens:
            import logging

            logging.getLogger(__name__).warning(
                "claude CLI 回傳內容但 usage 為 0 —— 計費紀錄可能失真，請檢查 CLI 版本輸出格式")
        return LLMResponse(result_text, usage)

    def list_models(self) -> list[str]:
        return list(CLI_MODELS)
