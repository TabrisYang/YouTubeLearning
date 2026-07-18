"""llm 抽象層的心臟：全系統唯一的 LLM 呼叫入口。

    llm.complete(purpose, messages, user_ctx) → text

路由兩個維度：
  purpose (eval / curate / intent)  ──決定──▶ 用哪個模型（讀 config/llm_settings）
  user_ctx.billing_mode             ──決定──▶ 用誰的憑證、成本記到誰頭上

所有呼叫的 usage 一律經 billing.meter 攔截記帳。
"""
from __future__ import annotations

from config import get_settings
from pipeline.models import LLMCallRecord, UserContext

from .providers.anthropic_p import AnthropicProvider
from .providers.base import LLMProvider, LLMResponse
from .providers.claude_cli import ClaudeCLIProvider
from .providers.google_p import GoogleProvider
from .providers.openai_p import OpenAIProvider

_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
    "claude_cli": ClaudeCLIProvider,   # 訂閱制（本機 CLI 模式，測試限定）
}

_PURPOSE_TO_SETTING = {
    "eval": "eval_model",
    "curate": "curate_model",
    "intent": "intent_model",
}


def provider_of_model(model: str) -> str:
    """由模型 id 推斷廠商。"""
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    raise ValueError(f"無法判斷模型所屬廠商: {model}")


def _platform_key(provider: str) -> str:
    s = get_settings()
    return {
        "anthropic": s.anthropic_api_key,
        "openai": s.openai_api_key,
        "google": s.google_api_key,
    }[provider]


def resolve(purpose: str, user_ctx: UserContext, llm_settings: dict) -> tuple[str, str, str]:
    """回傳 (provider, model, api_key)。api_key 僅在此函式與 provider 內部流動。"""
    if purpose not in _PURPOSE_TO_SETTING:
        raise ValueError(f"未知的 purpose: {purpose}")

    mode = user_ctx.billing_mode

    if mode == "byok_api_key":
        if not (user_ctx.byok_provider and user_ctx.byok_api_key and user_ctx.byok_model):
            raise ValueError("BYOK 模式缺少 provider / key / model 設定")
        return user_ctx.byok_provider, user_ctx.byok_model, user_ctx.byok_api_key

    if mode == "oauth":
        if not get_settings().enable_oauth:
            raise PermissionError("OAuth 模式未啟用（ENABLE_OAUTH=false）")
        # 本機 CLI 模式：不經手任何憑證，由 Claude Code CLI 自行認證。
        # 評估/意圖走 haiku（25 支影片逐支呼叫，速度差 5-10 倍），編排才用使用者選的模型
        if user_ctx.oauth_source == "cli":
            if purpose in ("eval", "intent"):
                return "claude_cli", "haiku", "local"
            return "claude_cli", user_ctx.oauth_model or "sonnet", "local"
        if not user_ctx.oauth_token:
            raise ValueError("OAuth 模式缺少 token")
        model = llm_settings[_PURPOSE_TO_SETTING[purpose]]
        return provider_of_model(model), model, user_ctx.oauth_token

    # points（預設）：平台憑證、平台模型設定
    model = llm_settings[_PURPOSE_TO_SETTING[purpose]]
    provider = provider_of_model(model)
    key = _platform_key(provider)
    if not key:
        raise ValueError(f"平台未設定 {provider} 的 API key（points 模式需要）")
    return provider, model, key


def complete(
    purpose: str,
    messages: list[dict],
    user_ctx: UserContext,
    *,
    system: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    job_id: str | None = None,
) -> str:
    """統一入口。回傳純文字；usage 自動記帳到 job_id（無則只記使用者層級）。"""
    # 延遲 import 避免循環相依（billing 也可能需要 llm 型別）
    from billing import meter
    from storage.firestore_repo import get_repo

    llm_settings = get_repo().get_llm_settings()
    provider_name, model, api_key = resolve(purpose, user_ctx, llm_settings)

    auth_scheme = "oauth" if user_ctx.billing_mode == "oauth" else "api_key"
    provider = _PROVIDER_CLASSES[provider_name](api_key, auth_scheme=auth_scheme)
    resp: LLMResponse = provider.complete(
        messages, model=model, system=system, max_tokens=max_tokens, temperature=temperature
    )

    record = LLMCallRecord(
        purpose=purpose,
        provider=provider_name,
        model=model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        cost_usd=meter.cost_usd(model, resp.usage),
    )
    meter.record_call(user_id=user_ctx.user_id, billing_mode=user_ctx.billing_mode,
                      job_id=job_id, record=record)
    return resp.text


def analyze_video(youtube_url: str, prompt: str, user_ctx: UserContext, *,
                  max_tokens: int = 1024, job_id: str | None = None) -> str:
    """影片理解入口（purpose=video_eval）。目前僅 Gemini 支援直接看 YouTube 影片。

    憑證：BYOK-Google 使用者用自己的 key；其餘一律走平台 GOOGLE_API_KEY。
    usage 照樣經 meter 記帳（影片畫面/聲音的 token 計入 input_tokens）。
    """
    import keyvault
    from billing import meter
    from storage.firestore_repo import get_repo

    llm_settings = get_repo().get_llm_settings()
    model = llm_settings.get("video_eval_model", "gemini-2.0-flash")

    # 憑證優先序：使用者自備 Gemini key → BYOK-Google 的 key →
    # （BYOK 使用者不得燒平台額度）→ 平台 key（訂閱制/點數制由平台吸收）
    api_key = keyvault.get_api_key(user_ctx.user_id, "gemini")
    if not api_key and user_ctx.billing_mode == "byok_api_key":
        if user_ctx.byok_provider == "google" and user_ctx.byok_api_key:
            api_key = user_ctx.byok_api_key
        else:
            raise ValueError("BYOK 模式的影片深度分析需自備 Gemini key（「設定」→ 6），"
                             "未設定時課程改用 metadata 分析")
    if not api_key:
        api_key = get_settings().google_api_key
    if not api_key:
        raise ValueError("影片理解需要 Google API key（平台未設定 GOOGLE_API_KEY）")

    resp = GoogleProvider(api_key).complete_with_video(
        youtube_url, prompt, model=model, max_tokens=max_tokens)
    record = LLMCallRecord(
        purpose="video_eval", provider="google", model=model,
        input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
        cost_usd=meter.cost_usd(model, resp.usage),
    )
    meter.record_call(user_id=user_ctx.user_id, billing_mode=user_ctx.billing_mode,
                      job_id=job_id, record=record)
    return resp.text
