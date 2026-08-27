"""grill 式開課訪談：一次一題、附建議答案，把模糊需求收斂成 LearningContract。

流程（由 main._handle_course_flow 驅動）：
  topic（使用者給主題）→ grill（LLM 逐題追問，最多 MAX_GRILL_TURNS 輪）
  → contract_confirm（呈現契約，可用自然語言修改）→ count / confirm（原流程）

LLM 不可用時由呼叫端退回舊版固定三題流程（level）——品質妥協但功能不斷。
"""
from __future__ import annotations

import json
import logging

import llm
from pipeline.models import LearningContract, UserContext

logger = logging.getLogger(__name__)

MAX_GRILL_TURNS = 5
_JSON_RETRIES = 1

_CONTRACT_SCHEMA = """\
{"topic": "收斂後的主題描述（比使用者原話更精確）",
 "include": ["必須涵蓋的子主題", ...],
 "exclude": ["明確排除的子主題", ...],
 "language": "zh_first(繁中優先英文補位) | zh_only | any",
 "chinese_script": "any | no_simplified(排除簡體)",
 "start_difficulty": 1-5 整數（1=零基礎～5=進階）,
 "min_duration_min": 影片最短分鐘數（預設 4）,
 "max_duration_min": 影片最長分鐘數（預設 45）,
 "recency_months": 影片須在 N 個月內（工具/軟體類 12-24；理論通識類 null）,
 "channel_blocklist": ["使用者點名排除的頻道", ...],
 "channel_prioritize": ["使用者點名的口袋頻道", ...],
 "teaching_style_pref": "教學形式偏好，例：實作跟打型優先；沒特別偏好給空字串",
 "low_trust_popularity": true/false}"""

_GRILL_SYSTEM = f"""\
你是課程策展系統的「開課訪談員」。使用者給了一個想學的主題，你的任務是像資深顧問一樣，
用最少的問題把模糊的學習需求收斂成明確的「學習契約」。

訪談規則：
1. 一次只問一個問題，每題必附你的建議答案（使用者可能回「建議」「都可以」表示採納你的建議）
2. 只主動問三件事，依序：
   a) 主題的確切範圍——含什麼、不含什麼（最重要；主題越大越要先問這題收斂）
   b) 起始難度——探測使用者現有程度（問具體經驗，不要問「你覺得自己是新手嗎」）
   c) 語言偏好（繁中優先/只要中文/不限）
3. 最多問 {MAX_GRILL_TURNS} 題；已能判斷就提早收斂，不要為問而問
4. 其餘維度（片長、時效、頻道、教學形式、流量可信度）由你依主題性質直接給預設值
5. 財經投資、健康醫療、加密貨幣等領域：low_trust_popularity 必須為 true
   （這些領域高流量常是導流帶單片，流量不可信）
6. 工具/軟體類主題：recency_months 給 12-24（版本會過時）；理論/通識類給 null

輸出格式：只輸出一個 JSON 物件，不要任何其他文字。兩種格式擇一：
還需要問 → {{"type": "ask", "question": "問題本體（繁體中文，口語）",
             "recommendation": "你的建議答案與一句理由（50 字內）"}}
可以收斂 → {{"type": "contract", "contract": {_CONTRACT_SCHEMA}}}"""

_REVISE_SYSTEM = f"""\
你是課程策展系統的契約修訂員。使用者對既有的「學習契約」提出修改要求，
請輸出修改後的完整契約。只改使用者要求的部分，其他欄位保持原值。
只輸出一個 JSON 物件（完整契約，不要包 type），不要任何其他文字，格式：
{_CONTRACT_SCHEMA}"""


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _grill_turn(topic: str, qa: list[dict], user_ctx: UserContext,
                job_id: str | None, force_contract: bool = False) -> dict:
    """跑一輪訪談。回傳解析後的 {"type": "ask"|"contract", ...}。"""
    lines = [f"使用者想學的主題：{topic}"]
    if qa:
        lines.append("目前為止的訪談記錄：")
        lines += [f"問：{item['q']}\n答：{item['a']}" for item in qa]
    else:
        lines.append("（訪談剛開始，還沒問過任何問題）")
    if force_contract:
        lines.append("題數已達上限，禁止再問，必須依現有資訊輸出 contract。")
    prompt = "\n".join(lines)

    last_err: Exception | None = None
    for _ in range(1 + _JSON_RETRIES):
        text = llm.complete("intent", [{"role": "user", "content": prompt}], user_ctx,
                            system=_GRILL_SYSTEM, max_tokens=1500, job_id=job_id)
        try:
            data = json.loads(_extract_json(text))
            if data.get("type") == "contract":
                data["contract"] = LearningContract(**data["contract"])  # 立刻驗證
            elif data.get("type") != "ask":
                raise ValueError(f"未知 type: {data.get('type')}")
            return data
        except Exception as e:  # 非法 JSON / 欄位驗證失敗 → retry
            last_err = e
    raise ValueError(f"訪談員連續輸出非法結果: {last_err}")


def _ask_message(data: dict, turn: int) -> str:
    return (f"❓ {data['question']}\n\n"
            f"💡 建議：{data['recommendation']}\n"
            f"（回「建議」直接採納；隨時回「取消」離開｜{turn}/{MAX_GRILL_TURNS}）")


def render_confirm(contract: LearningContract) -> str:
    return ("📋 學習契約確認（搜尋、選片、排課都會照這份走）\n\n"
            + "\n".join(f"・{line}" for line in contract.summary_lines())
            + "\n\n回覆「確認」進入堂數選擇；想調整就直接說"
              "（例：片長上限改 30 分鐘、加排除某頻道）；「取消」離開。")


def start(topic: str, user_ctx: UserContext, job_id: str | None = None
          ) -> tuple[list[str], dict]:
    """訪談開場。回傳 (要回覆的訊息, session 更新)。LLM 失敗時 raise，由呼叫端退回舊流程。"""
    data = _grill_turn(topic, [], user_ctx, job_id)
    if data["type"] == "contract":   # 主題已夠明確，一題都不用問
        return ([render_confirm(data["contract"])],
                {"step": "contract_confirm", "contract": data["contract"].model_dump()})
    return ([_ask_message(data, 1)],
            {"step": "grill", "pending_q": data["question"], "qa": []})


def answer(session: dict, text: str, user_ctx: UserContext, job_id: str | None = None
           ) -> tuple[list[str], dict]:
    """處理訪談中的一則使用者回答。回傳 (訊息, session 更新)。"""
    qa = list(session.get("qa", []))
    qa.append({"q": session.get("pending_q", ""), "a": text})
    force = len(qa) >= MAX_GRILL_TURNS
    data = _grill_turn(session["topic"], qa, user_ctx, job_id, force_contract=force)
    if data["type"] == "contract":
        return ([render_confirm(data["contract"])],
                {"step": "contract_confirm", "contract": data["contract"].model_dump()})
    return ([_ask_message(data, len(qa) + 1)],
            {"step": "grill", "pending_q": data["question"], "qa": qa})


def revise(contract_data: dict, instruction: str, user_ctx: UserContext,
           job_id: str | None = None) -> LearningContract:
    """依使用者的自然語言指示修訂契約。失敗時 raise，由呼叫端請使用者換個說法。"""
    prompt = ("目前的學習契約：\n" + json.dumps(contract_data, ensure_ascii=False)
              + f"\n\n使用者的修改要求：{instruction}")
    last_err: Exception | None = None
    for _ in range(1 + _JSON_RETRIES):
        text = llm.complete("intent", [{"role": "user", "content": prompt}], user_ctx,
                            system=_REVISE_SYSTEM, max_tokens=1500, job_id=job_id)
        try:
            return LearningContract(**json.loads(_extract_json(text)))
        except Exception as e:
            last_err = e
    raise ValueError(f"契約修訂失敗: {last_err}")


def default_contract(topic: str, level: str | None = None) -> LearningContract:
    """舊版固定三題流程（LLM 不可用的退路）與一行式開課用的預設契約。"""
    start = {"beginner": 1, "some": 2, "advanced": 3}.get(level or "", 1)
    return LearningContract(topic=topic, start_difficulty=start)
