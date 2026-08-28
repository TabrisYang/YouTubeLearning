"""FastAPI 進入點：LINE webhook + 後台設定 API + 健康檢查。

LINE 互動契約（架構文件第八節）：
  開課 <主題> <堂數> / 今日課程 / 完成 / 答案 / 我的課程 / 我的點數 / 設定
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

import keyvault
import worker
from billing import points, quote
from config import get_settings
from delivery import formatter, line_client, settings_flow
from pipeline.models import Lesson
from storage.firestore_repo import get_repo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _QuietPollingFilter(logging.Filter):
    """dev 介面每 2 秒輪詢 /dev/outbox，access log 會刷屏淹沒重要訊息 —— 濾掉。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/dev/outbox" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_QuietPollingFilter())

app = FastAPI(title="yt-course-curator")

_OPEN_COURSE = re.compile(r"^開課\s+(.+?)\s+(\d{1,2})$")
_CONFIRM = re.compile(r"^確認開課\s+(.+?)\s+(\d{1,2})$")
_LESSON_N = re.compile(r"^第\s*(\d{1,2})\s*堂$")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- 後台設定（D7：線上改 markup / 模型即時生效） ----------

@app.get("/admin/llm-settings")
def get_llm_settings():
    return get_repo().get_llm_settings()


@app.patch("/admin/llm-settings")
def patch_llm_settings(patch: dict):
    allowed = {"eval_model", "curate_model", "intent_model", "markup", "usd_to_twd",
               "pricing_mode", "fixed_points_per_course", "free_daily_quota"}
    bad = set(patch) - allowed
    if bad:
        raise HTTPException(400, f"不允許的欄位: {bad}")
    return get_repo().set_llm_settings(patch)


# ---------- 卡住任務掃描（D7：Cloud Scheduler 每小時打一次） ----------

_STUCK_THRESHOLD_SEC = 10 * 60


def _age_seconds(created_at) -> float:
    """created_at 相容 in-memory 的 float 與 Firestore 的 datetime。"""
    import time as _time
    from datetime import datetime, timezone

    if isinstance(created_at, (int, float)):
        return _time.time() - created_at
    if isinstance(created_at, datetime):
        return (datetime.now(timezone.utc) - created_at).total_seconds()
    return 0.0


@app.post("/admin/scan-jobs")
def scan_jobs(background: BackgroundTasks):
    """重跑兩類課程：卡在 generating >10 分（容器重啟）、waitlisted（quota 隔日補跑）。

    重跑沿用原 course_id 與已扣點數，不重複扣點；waitlist 補跑時 worker 會再檢查
    當日 quota，仍不足就繼續留在 waitlist。
    """
    repo = get_repo()
    retried: list[str] = []
    stuck = [c for c in repo.list_courses_by_status("generating")
             if _age_seconds(c.get("created_at")) > _STUCK_THRESHOLD_SEC]
    for c in stuck + repo.list_courses_by_status("waitlisted"):
        background.add_task(
            worker.generate_course, c["owner"], c["topic"], c["lesson_count"],
            c.get("charged_points", 0), True, c["course_id"],
        )
        retried.append(c["course_id"])
    return {"retried": retried, "stuck": len(stuck)}


@app.post("/admin/check-links")
def check_links():
    """死鏈巡檢（Cloud Scheduler 每日打一次）：失效影片標記 lesson.dead，課程列表顯示 ⚠️。"""
    return worker.check_dead_links()


# ---------- 本機測試介面（dev 模式限定：未設 LINE token 或 DEV_MODE=true） ----------

DEV_USER = "dev-user"


def _require_dev() -> None:
    if not line_client.dev_mode():
        raise HTTPException(404)


@app.get("/dev", response_class=HTMLResponse)
def dev_page():
    _require_dev()
    from delivery.dev_ui import DEV_HTML

    return DEV_HTML


@app.post("/dev/message")
async def dev_message(payload: dict, background: BackgroundTasks):
    """模擬 LINE 訊息：走與正式 webhook 完全相同的 _handle_text。"""
    _require_dev()
    text = str(payload.get("text", "")).strip()
    if text:
        try:
            _handle_text(DEV_USER, "dev-reply-token", text, background)
        except Exception:
            logger.exception("dev 訊息處理失敗")
            line_client.DEV_OUTBOX.append({"type": "text", "text": "⚠️ 處理失敗，詳見終端機 log"})
    return {"messages": line_client.drain_dev_outbox()}


@app.get("/dev/outbox")
def dev_outbox():
    """輪詢背景訊息（生成完成的 Push 通知走這裡送達）。"""
    _require_dev()
    return {"messages": line_client.drain_dev_outbox()}


@app.get("/dev/export")
def dev_export():
    """把最近一門已完成課程匯出成 Markdown 下載（D4 驗收；本機記憶體模式防資料消失）。

    直接讀資料庫組 course_markdown（含影片連結、摘要、學習目標、檢核題與答案），
    不動學習進度 —— 與 export_course.py 的訊息爬取版不同，不會把課程標記完成。
    """
    from urllib.parse import quote

    from fastapi.responses import Response

    from pipeline.models import CoursePlan

    _require_dev()
    repo = get_repo()
    user = repo.get_user(DEV_USER) or {}
    course = None
    for cid in (user.get("active_course_id"), user.get("last_course_id")):
        if cid and (c := repo.get_course(cid)) and c.get("status") == "ready":
            course = c
            break
    if course is None:
        raise HTTPException(404, "沒有已完成的課程可匯出（先開課並等生成完成）")

    lessons = [Lesson(**l) for l in repo.get_lessons(course["course_id"])]
    plan = CoursePlan(topic=course["topic"], requested_lessons=course["lesson_count"],
                      lessons=lessons, honest_note=course.get("honest_note", ""))
    md = formatter.course_markdown(plan)
    if course.get("contract"):   # 附上實際生效的學習契約（驗收時對照用）
        from pipeline.models import LearningContract

        try:
            summary = LearningContract(**course["contract"]).summary_lines()
            md += "\n\n---\n\n## 本課程的學習契約\n" + "\n".join(f"- {s}" for s in summary)
        except Exception:
            pass
    fname = quote(f"課綱_{course['topic'][:30]}.md")
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"course.md\"; filename*=UTF-8''{fname}"})


# ---------- LINE webhook ----------

def _verify_signature(body: bytes, signature: str) -> bool:
    secret = get_settings().line_channel_secret.encode()
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_line_signature: str = Header(default="")):
    body = await request.body()
    if not _verify_signature(body, x_line_signature):
        raise HTTPException(403, "signature 驗證失敗")

    payload = await request.json()
    for event in payload.get("events", []):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            continue
        user_id = event["source"].get("userId", "")
        reply_token = event.get("replyToken", "")
        text = event["message"].get("text", "").strip()
        try:
            _handle_text(user_id, reply_token, text, background)
        except Exception:
            # 注意：永不 log 訊息內容 —— 設定流程中可能含使用者的 API key
            logger.exception("處理訊息失敗（user=%s）", user_id)
            line_client.reply(reply_token, ["系統忙碌中，請稍後再試 🙏"])
    return {"ok": True}


# ---------- 開課前預檢：缺什麼就直接教使用者設定 ----------

_YT_GUIDE = (
    "📺 還差一步：影片搜尋需要 YouTube API key（免費申請，約 3 分鐘）。\n\n"
    "1️⃣ 點此直接開啟啟用頁（登入 Google 後按「啟用」）：\n"
    "https://console.cloud.google.com/apis/library/youtube.googleapis.com\n"
    "2️⃣ 建立金鑰（憑證頁 → 建立憑證 → API 金鑰）：\n"
    "https://console.cloud.google.com/apis/credentials\n"
    "3️⃣ 回到這裡輸入「設定」→ 選 3 → 貼上 key\n\n"
    "📖 官方圖文教學：\n"
    "https://developers.google.com/youtube/v3/getting-started?hl=zh-tw"
)


def _readiness_problems(user_id: str) -> list[str]:
    """回傳阻擋生成的問題清單（附設定引導）；空清單 = 可以開跑。"""
    from llm.router import provider_of_model

    repo = get_repo()
    s = get_settings()
    user = repo.get_user(user_id) or {}
    mode = user.get("billing_mode", "points")
    problems: list[str] = []

    # LLM 憑證
    if mode == "points":
        llm_settings = repo.get_llm_settings()
        provider = provider_of_model(llm_settings["eval_model"])
        platform_key = {"anthropic": s.anthropic_api_key, "openai": s.openai_api_key,
                        "google": s.google_api_key}.get(provider, "")
        if not platform_key:
            problems.append(
                "⚙️ 尚未配置任何 LLM 金鑰。\n"
                "・自架者：在 .env 填入 ANTHROPIC_API_KEY、OPENAI_API_KEY 或 GOOGLE_API_KEY "
                "任一把即可（系統會自動採用該廠商的模型）\n"
                "・一般使用者：輸入「設定」→ 選 2 自備 API key（走你自己的額度、不扣點）")
    elif mode == "byok_api_key" and not keyvault.get_api_key(user_id, "llm"):
        problems.append(
            "🔑 你的 API key 已過期（安全起見系統不儲存 key）。\n"
            "請輸入「設定」→ 選 2 重新貼上 key。")
    elif (mode == "oauth" and user.get("oauth_source", "token") != "cli"
          and not keyvault.get_api_key(user_id, "oauth")):
        problems.append("🔑 訂閱制 token 已過期，請輸入「設定」重新授權。")

    # YouTube 搜尋憑證（使用者自備或平台擇一）
    if not keyvault.get_api_key(user_id, "youtube") and not s.youtube_api_key:
        problems.append(_YT_GUIDE)

    return problems


def _bump_streak(user_id: str) -> int:
    """連續學習天數：今天已算過不重複；昨天有學 +1；斷了歸 1。"""
    from datetime import date, timedelta

    repo = get_repo()
    user = repo.get_user(user_id) or {}
    today = date.today().isoformat()
    last = user.get("last_learn_date")
    streak = user.get("streak", 0)
    if last == today:
        return streak or 1
    streak = streak + 1 if last == (date.today() - timedelta(days=1)).isoformat() else 1
    repo.upsert_user(user_id, {"last_learn_date": today, "streak": streak})
    return streak


def _generating_line(course: dict) -> str:
    """生成中課程的進度描述：階段｜百分比｜已耗時｜預計剩餘。"""
    import math

    pct = int(course.get("progress_pct", 0))
    elapsed = _age_seconds(course.get("created_at"))
    mins = int(elapsed // 60)
    line = f"⏳ 「{course['topic']}」生成中：{course.get('stage', '處理中')}｜進度 {pct}%（已 {mins} 分鐘"
    if pct >= 15:  # 進度太低時外推會失真，不顯示
        remaining = math.ceil(elapsed * (100 - pct) / pct / 60)
        line += f"，預計還要約 {max(remaining, 1)} 分鐘"
    line += "）"
    return line


def _active_course(user_id: str) -> tuple[dict, dict] | None:
    """回傳 (course, progress)。目前一人一門進行中課程（v1 簡化）。"""
    repo = get_repo()
    user = repo.get_user(user_id) or {}
    course_id = user.get("active_course_id")
    if not course_id:
        return None
    course = repo.get_course(course_id)
    progress = repo.get_progress(user_id, course_id)
    if not course or not progress:
        return None
    return course, progress


def _handle_text(user_id: str, reply_token: str, text: str,
                 background: BackgroundTasks) -> None:
    repo = get_repo()
    repo.upsert_user(user_id, {})  # 確保 user 存在

    # --- 測試專用：儲值指令（僅 dev 模式，正式環境不存在） ---
    if line_client.dev_mode() and (m := re.match(r"^儲值\s+(\d+)$", text)):
        bal = points.topup(user_id, int(m.group(1)), note="dev-topup")
        line_client.reply(reply_token, [f"💰（測試）儲值成功，目前餘額 {bal} 點"])
        return

    # --- 設定流程（逐步引導狀態機）優先攔截 ---
    if text == "設定":
        keyvault.sessions.delete(f"course:{user_id}")   # 中途改設定就放棄開課會話
        line_client.reply(reply_token, settings_flow.start(user_id))
        return
    if settings_flow.in_session(user_id):
        line_client.reply(reply_token, settings_flow.handle(user_id, text))
        return

    # --- 逐步開課（輸入「開課」不帶參數 → 一題一題問） ---
    if text == "開課":
        if problems := _readiness_problems(user_id):
            line_client.reply(reply_token, problems)
            return
        keyvault.sessions.set(f"course:{user_id}", {"step": "topic"})
        line_client.reply(reply_token, [
            "好，我們來開一門課！📚\n請問你想學什麼主題？\n"
            "（例如：AI工作流、Python入門、影片剪輯…隨時回「取消」離開）"])
        return
    if session := keyvault.sessions.get(f"course:{user_id}"):
        _handle_course_flow(user_id, reply_token, text, session, background)
        return

    # --- 開課（先預檢，缺什麼直接引導；通過才報價） ---
    if m := _OPEN_COURSE.match(text):
        topic, n = m.group(1), int(m.group(2))
        if problems := _readiness_problems(user_id):
            line_client.reply(reply_token, problems)
            return
        user = repo.get_user(user_id) or {}
        if user.get("billing_mode", "points") != "points":
            line_client.reply(reply_token, [
                f"開課「{topic}」共 {n} 堂，將使用你自己的 API key 生成（不扣點）。\n"
                f"確認請回覆：確認開課 {topic} {n}"
            ])
            return
        settings = repo.get_llm_settings()
        q = quote.estimate_points(0, 25, n, settings)
        cost_str = (f"{q['points']} 點" if q["mode"] == "fixed"
                    else f"約 {q['points_low']}-{q['points_high']} 點")
        line_client.reply(reply_token, [
            f"開課「{topic}」共 {n} 堂，本次生成扣 {cost_str}。\n"
            f"目前餘額：{points.balance(user_id)} 點\n"
            f"確認請回覆：確認開課 {topic} {n}"
        ])
        return

    # --- 確認開課（一行式指令，熟手用；逐步流程最後也走同一個入口） ---
    if m := _CONFIRM.match(text):
        _start_generation(user_id, reply_token, m.group(1), int(m.group(2)), background)
        return

    # --- 今日課程（Reply 免費） ---
    if text == "今日課程":
        ac = _active_course(user_id)
        if not ac:
            line_client.reply(reply_token, ["還沒有進行中的課程。輸入「開課 主題 堂數」開始！"])
            return
        course, progress = ac
        lessons = repo.get_lessons(course["course_id"])
        cur = progress.get("current_lesson", 1)
        lesson = next((l for l in lessons if l["order"] == cur), None)
        if lesson is None:
            line_client.reply(reply_token, ["🎉 這門課已經全部完成了！輸入「開課」開新的一門。"])
            return
        line_client.reply(reply_token, [formatter.lesson_flex(Lesson(**lesson), len(lessons))])
        return

    # --- 課程列表（依序標註第幾堂，可跳選） ---
    if text == "課程列表":
        ac = _active_course(user_id)
        if not ac:
            line_client.reply(reply_token, ["還沒有進行中的課程。輸入「開課」開始！"])
            return
        course, progress = ac
        lessons = repo.get_lessons(course["course_id"])
        done, cur = set(progress.get("completed", [])), progress.get("current_lesson", 1)
        lines = [f"📚 {course['topic']} 課程列表："]
        for l in lessons:
            mark = "✅" if l["order"] in done else ("▶️" if l["order"] == cur else "▫️")
            dead = "⚠️影片已失效 " if l.get("dead") else ""
            lines.append(f"{mark} 第 {l['order']} 堂｜{dead}{l['title'][:25]}（{l['duration_sec'] // 60} 分）")
        lines.append("\n回覆「第 3 堂」可直接看該堂內容；「完成」只計目前進度的那一堂（▶️）。")
        line_client.reply(reply_token, ["\n".join(lines)])
        return

    # --- 第 N 堂（跳選任一堂） ---
    if m := _LESSON_N.match(text):
        ac = _active_course(user_id)
        if not ac:
            line_client.reply(reply_token, ["還沒有進行中的課程。輸入「開課」開始！"])
            return
        course, _ = ac
        lessons = repo.get_lessons(course["course_id"])
        lesson = next((l for l in lessons if l["order"] == int(m.group(1))), None)
        if lesson is None:
            line_client.reply(reply_token, [f"這門課只有 {len(lessons)} 堂喔，輸入「課程列表」看全部。"])
            return
        line_client.reply(reply_token, [formatter.lesson_flex(Lesson(**lesson), len(lessons))])
        return

    # --- 完成回報 → 檢核題 ---
    if text == "完成":
        ac = _active_course(user_id)
        if not ac:
            line_client.reply(reply_token, ["目前沒有進行中的課程喔。"])
            return
        course, progress = ac
        lessons = repo.get_lessons(course["course_id"])
        cur = progress.get("current_lesson", 1)
        lesson = next((l for l in lessons if l["order"] == cur), None)
        if lesson is None:
            line_client.reply(reply_token, ["這門課已經完成了 🎉"])
            return
        completed = progress.get("completed", []) + [cur]
        repo.set_progress(user_id, course["course_id"], {
            "current_lesson": cur + 1, "completed": completed,
            "active": cur + 1 <= len(lessons),
        })
        streak = _bump_streak(user_id)
        msgs = [formatter.quiz_text(Lesson(**lesson)),
                f"🔥 連續學習 {streak} 天！\n這堂課教得如何？回覆「讚」或「爛」幫我改進選片。"]
        if cur + 1 <= len(lessons):
            nxt = next(l for l in lessons if l["order"] == cur + 1)
            msgs.append(f"下一堂預告：{nxt['title']}\n準備好就點「今日課程」！")
        else:
            msgs.append("🎓 恭喜完成整門課程！\n"
                        "想更深入這個主題？輸入「進階開課」——"
                        "我會避開已上過的影片，編一門更深的課！")
        line_client.reply(reply_token, msgs)
        return

    # --- 課堂回饋（讚/爛 → 策展品質資料，未來回流修提示詞） ---
    if text in ("讚", "爛"):
        ac = _active_course(user_id)
        target = None
        if ac:
            course, progress = ac
            done = progress.get("completed", [])
            if done:
                target = (course["course_id"], done[-1])
        if target is None:
            line_client.reply(reply_token, ["先完成一堂課再評分吧！"])
            return
        repo.add_feedback(user_id, target[0], target[1], "good" if text == "讚" else "bad")
        line_client.reply(reply_token, [
            "收到，謝謝回饋！🙏" + ("" if text == "讚" else "\n我會把這個訊號用來改進之後的選片。")])
        return

    # --- 複習（從已完成堂數隨機抽檢核題 → 間隔重複的第一步） ---
    if text == "複習":
        ac = _active_course(user_id)
        if not ac:
            line_client.reply(reply_token, ["還沒有課程可以複習。輸入「開課」開始！"])
            return
        course, progress = ac
        done = set(progress.get("completed", []))
        pool = [(l["order"], q) for l in repo.get_lessons(course["course_id"])
                if l["order"] in done for q in l.get("quiz", [])]
        if not pool:
            line_client.reply(reply_token, ["先完成至少一堂課，才有題目可以複習喔！"])
            return
        import random
        picked = random.sample(pool, min(3, len(pool)))
        q_lines = [f"{i}. （第 {o} 堂）{q['q']}" for i, (o, q) in enumerate(picked, 1)]
        a_lines = [f"{i}. {q['a']}" for i, (o, q) in enumerate(picked, 1)]
        line_client.reply(reply_token, [
            "🧠 複習時間！先自己想想：\n" + "\n".join(q_lines),
            "⬇️ 想好再看答案：\n" + "\n".join(a_lines)])
        return

    # --- 進階開課（同主題往更深學：排除已上過影片、難度往上） ---
    if text == "進階開課":
        base = None
        user = repo.get_user(user_id) or {}
        for cid in (user.get("active_course_id"), user.get("last_course_id")):
            if cid and (c := repo.get_course(cid)) and c.get("status") == "ready":
                base = c
                break
        if base is None:
            line_client.reply(reply_token, ["要先完成一門課才能進階喔！輸入「開課」開始第一門。"])
            return
        _start_generation(user_id, reply_token, base["topic"], base["lesson_count"],
                          background, advanced_from=base["course_id"])
        return

    # --- 對答案 ---
    if text == "答案":
        ac = _active_course(user_id)
        if ac:
            course, progress = ac
            lessons = repo.get_lessons(course["course_id"])
            last = (progress.get("completed") or [0])[-1]
            lesson = next((l for l in lessons if l["order"] == last), None)
            if lesson:
                line_client.reply(reply_token, [formatter.quiz_answers_text(Lesson(**lesson))])
                return
        line_client.reply(reply_token, ["先完成一堂課再來對答案吧！"])
        return

    # --- 我的點數 ---
    if text == "我的點數":
        line_client.reply(reply_token, [f"目前餘額：{points.balance(user_id)} 點"])
        return

    # --- 重試（上次失敗後一鍵重新生成，不用重填主題） ---
    if text in ("重試", "再試一次"):
        user = repo.get_user(user_id) or {}
        last_id = user.get("last_course_id")
        last = repo.get_course(last_id) if last_id else None
        if last and last.get("status") == "failed":
            _start_generation(user_id, reply_token, last["topic"], last["lesson_count"], background)
        elif last and last.get("status") == "generating":
            line_client.reply(reply_token, [_generating_line(last) + "\n還在跑，不用重試！"])
        else:
            line_client.reply(reply_token, ["沒有失敗的課程需要重試。輸入「開課」開新的一門！"])
        return

    # --- 我的課程（含生成中進度） ---
    if text == "我的課程":
        lines = []
        user = repo.get_user(user_id) or {}
        last_id = user.get("last_course_id")
        if last_id and (last := repo.get_course(last_id)):
            if last.get("status") == "generating":
                lines.append(_generating_line(last))
            elif last.get("status") == "waitlisted":
                lines.append(f"📋 「{last['topic']}」排隊中，明天自動補跑")
            elif last.get("status") == "failed":
                lines.append(f"❌ 上次生成「{last['topic']}」失敗："
                             f"{last.get('fail_reason', '未知原因')}\n輸入「重試」可直接重新生成。")
        if ac := _active_course(user_id):
            course, progress = ac
            done = len(progress.get("completed", []))
            lines.append(f"📚 {course['topic']}（{done}/{course['lesson_count']} 堂完成）"
                         "\n輸入「今日課程」繼續上課！")
        if not lines:
            lines.append("目前沒有課程。輸入「開課」我會一步步帶你開始！")
        line_client.reply(reply_token, ["\n\n".join(lines)])
        return

    # --- 其他文字：LLM 智慧客服（知道使用者狀態；LLM 不可用時退回靜態選單） ---
    line_client.reply(reply_token, [_smart_fallback(user_id, text)])


_STATIC_MENU = ("可以這樣使用我：\n・開課（我會一步步引導你）\n・開課 AI工作流 10（一行式）\n"
                "・今日課程\n・完成\n・我的課程 / 我的點數\n・設定")

_NLU_SYSTEM = """\
你是「YT 課程策展系統」的客服助手，透過 LINE 與使用者對話。這個系統會依主題從 YouTube \
搜集影片並編排成由淺入深的課程。請用繁體中文、150 字內、親切地回覆。

系統指令（使用者必須輸入這些格式才會觸發功能）：
・開課 → 進入逐步引導（AI 訪談收斂「學習契約」：一次一題附建議答案，最推薦新手）
・開課 <主題> <堂數>（例：開課 AI工作流 5）→ 一行式，之後回「確認開課 <主題> <堂數>」
・今日課程 / 完成 / 答案 / 我的課程 / 我的點數
・課程列表 → 全部堂數一覽；「第 3 堂」可跳看任一堂
・複習 → 從已完成的堂數隨機抽檢核題
・讚 / 爛 → 評價剛完成的那堂課（幫助系統改進選片）
・重試 → 上次生成失敗後一鍵重新生成（免重填主題）
・進階開課 → 完成一門課後，同主題往更深學（自動避開上過的影片）
・設定（選單：1 點數制、2 自備 API key、3 YouTube API key、4 清除 key、5 課程偏好）

規則：
1. 使用者想做某件事 → 直接給他可複製的正確指令
2. 使用者問失敗原因或系統狀況 → 根據下方「目前狀態」誠實解釋並給下一步
3. 不要編造不存在的功能；不確定時引導輸入「設定」或列出指令清單"""


def _build_user_state(user_id: str) -> str:
    """組出給 NLU 的使用者狀態摘要（不含任何 key 內容）。"""
    repo = get_repo()
    s = get_settings()
    user = repo.get_user(user_id) or {}
    mode = user.get("billing_mode", "points")
    lines = [
        f"計費模式: {mode}",
        f"點數餘額: {points.balance(user_id)}",
        f"YouTube API key: {'已設定' if (keyvault.get_api_key(user_id, 'youtube') or s.youtube_api_key) else '未設定（開課前必須設定，輸入「設定」→ 3）'}",
    ]
    if mode == "byok_api_key":
        lines.append(f"BYOK key 狀態: {'有效' if keyvault.get_api_key(user_id, 'llm') else '已過期，需重新設定'}")
    ac = _active_course(user_id)
    if ac:
        course, progress = ac
        lines.append(f"進行中課程: {course['topic']}（{len(progress.get('completed', []))}/{course['lesson_count']} 堂完成）")
    else:
        lines.append("進行中課程: 無")
    last_id = user.get("last_course_id")
    if last_id and (last := repo.get_course(last_id)):
        item = f"最近一次生成: 「{last['topic']}」狀態 {last['status']}"
        if last.get("status") == "generating":
            item += (f"（{_generating_line(last)} —— 生成中屬正常，"
                     "請使用者耐心等候或輸入「我的課程」看進度）")
        if last.get("fail_reason"):
            item += f"，失敗原因: {last['fail_reason']}（可輸入「重試」直接重新生成）"
        lines.append(item)
    return "\n".join(lines)


def _smart_fallback(user_id: str, text: str) -> str:
    """LLM 智慧客服。帶短期對話記憶（近幾輪閒聊，記憶體 15 分鐘）——
    只記走到這裡的一般對話；指令與設定流程在上游就被攔截，API key 不會進記憶。"""
    import llm
    import worker as _worker

    chat_key = f"chat:{user_id}"
    history: list[dict] = keyvault.sessions.get(chat_key) or []
    context = ""
    if history:
        context = "\n\n最近的對話（延續語境用）：\n" + "\n".join(
            f"{'使用者' if h['role'] == 'user' else '你'}：{h['text']}" for h in history)
    try:
        user_ctx = _worker.build_user_ctx(user_id)
        answer = llm.complete(
            "intent",
            [{"role": "user", "content": text}],
            user_ctx,
            system=_NLU_SYSTEM + "\n\n使用者目前狀態：\n" + _build_user_state(user_id) + context,
            max_tokens=500,
            job_id=f"nlu-{user_id}",
        )
        answer = answer.strip() or _STATIC_MENU
        keyvault.sessions.set(chat_key, (history + [
            {"role": "user", "text": text[:200]},
            {"role": "assistant", "text": answer[:200]},
        ])[-8:])
        return answer
    except Exception:
        logger.info("NLU 不可用，退回靜態選單（user=%s）", user_id)
        return _STATIC_MENU


def _handle_course_flow(user_id: str, reply_token: str, text: str,
                        session: dict, background: BackgroundTasks) -> None:
    """逐步開課：問主題 → grill 式訪談收斂學習契約 → 問堂數 → 報價 → 確認即開始。

    grill 訪談（intake_flow）需要 LLM；不可用時退回舊版固定三題（level），功能不斷。
    """
    from delivery import intake_flow

    repo = get_repo()
    skey = f"course:{user_id}"

    if text in ("取消", "cancel"):
        keyvault.sessions.delete(skey)
        line_client.reply(reply_token, ["已取消開課，想學的時候再輸入「開課」！"])
        return

    step = session.get("step")

    def _fallback_to_level(topic: str) -> None:
        session.update(step="level", topic=topic)
        keyvault.sessions.set(skey, session)
        line_client.reply(reply_token, [
            f"好的，主題是「{topic}」！\n"
            "你目前對這個主題的程度是？（影響課程難度起點）\n"
            "1. 完全新手\n2. 有一些基礎\n3. 想深入進階"])

    if step == "topic":
        if not text or len(text) > 30:
            line_client.reply(reply_token, ["主題請用 30 字以內描述，例如：AI工作流"])
            return
        try:
            msgs, patch = intake_flow.start(
                text, worker.build_user_ctx(user_id), job_id=f"intake-{user_id}")
            session.update(topic=text, **patch)
            keyvault.sessions.set(skey, session)
            line_client.reply(reply_token, msgs)
        except Exception:
            logger.info("grill 訪談不可用，退回固定流程（user=%s）", user_id)
            _fallback_to_level(text)
        return

    if step == "grill":
        try:
            msgs, patch = intake_flow.answer(
                session, text, worker.build_user_ctx(user_id), job_id=f"intake-{user_id}")
            session.update(**patch)
            keyvault.sessions.set(skey, session)
            line_client.reply(reply_token, msgs)
        except Exception:
            logger.info("grill 訪談中斷，退回固定流程（user=%s）", user_id)
            _fallback_to_level(session["topic"])
        return

    if step == "contract_confirm":
        if text in ("確認", "好", "ok", "OK", "是"):
            session.update(step="count")
            keyvault.sessions.set(skey, session)
            line_client.reply(reply_token, ["想要幾堂課？回覆數字就好（建議 3～10 堂）"])
            return
        try:
            contract = intake_flow.revise(
                session["contract"], text, worker.build_user_ctx(user_id),
                job_id=f"intake-{user_id}")
            session.update(contract=contract.model_dump())
            keyvault.sessions.set(skey, session)
            line_client.reply(reply_token, [intake_flow.render_confirm(contract)])
        except Exception:
            line_client.reply(reply_token, [
                "這個修改我沒改成功 🙏 請再說一次或換個說法\n"
                "（例：發布時間改 3 天內、片長上限改 30 分鐘、加排除某頻道、"
                "難度起點改 2），或回「確認」用目前的契約繼續。"])
        return

    if step == "level":
        mapping = {"1": "beginner", "2": "some", "3": "advanced"}
        if text not in mapping:
            line_client.reply(reply_token, ["請回覆 1-3 選擇程度，或「取消」離開。"])
            return
        session.update(step="count", level=mapping[text])
        keyvault.sessions.set(skey, session)
        line_client.reply(reply_token, ["想要幾堂課？回覆數字就好（建議 3～10 堂）"])
        return

    if step == "count":
        if not text.isdigit() or not 1 <= int(text) <= 20:
            line_client.reply(reply_token, ["請回覆 1～20 的數字，例如：5"])
            return
        n = int(text)
        topic = (session.get("contract") or {}).get("topic") or session["topic"]
        settings = repo.get_llm_settings()
        user = repo.get_user(user_id) or {}
        if user.get("billing_mode", "points") != "points":
            cost_line = "將使用你自己的 API key 生成（不扣點）。"
        else:
            q = quote.estimate_points(0, 25, n, settings)
            cost_str = (f"{q['points']} 點" if q["mode"] == "fixed"
                        else f"約 {q['points_low']}-{q['points_high']} 點")
            cost_line = f"本次生成扣 {cost_str}（目前餘額 {points.balance(user_id)} 點）。"
        session.update(step="confirm", n=n)
        keyvault.sessions.set(skey, session)
        line_client.reply(reply_token, [
            f"最後確認一下 📋\n主題：{topic}\n堂數：{n} 堂\n{cost_line}\n\n"
            "回覆「確認」開始生成，或「取消」放棄。"])
        return

    if step == "confirm":
        if text in ("確認", "好", "ok", "OK", "是"):
            keyvault.sessions.delete(skey)
            contract = session.get("contract")
            topic = (contract or {}).get("topic") or session["topic"]  # 用收斂後的主題
            _start_generation(user_id, reply_token, topic, session["n"], background,
                              level=session.get("level"), contract=contract)
        else:
            line_client.reply(reply_token, ["回覆「確認」開始生成，或「取消」放棄。"])
        return

    keyvault.sessions.delete(skey)  # 未知狀態，重來


def _start_generation(user_id: str, reply_token: str, topic: str, n: int,
                      background: BackgroundTasks, advanced_from: str | None = None,
                      level: str | None = None, contract: dict | None = None) -> None:
    """預檢 → 防重複 → 配額 → 扣點 → 背景生成。一行式與逐步流程共用的唯一入口。"""
    repo = get_repo()
    if problems := _readiness_problems(user_id):
        line_client.reply(reply_token, problems)
        return

    # 防重複：已有課程生成中就別再開一門（卡超過 30 分鐘的交給掃描器，不擋新單）
    user = repo.get_user(user_id) or {}
    last_id = user.get("last_course_id")
    if last_id and (last := repo.get_course(last_id)):
        if last.get("status") == "generating" and _age_seconds(last.get("created_at")) < 30 * 60:
            line_client.reply(reply_token, [
                f"⏳ 你已經有一門「{last['topic']}」正在生成（{last.get('stage', '處理中')}），"
                "完成會通知你。\n輸入「我的課程」可以隨時看進度。"])
            return

    settings = repo.get_llm_settings()
    cost = settings["fixed_points_per_course"]
    user = repo.get_user(user_id) or {}
    mode = user.get("billing_mode", "points")
    charged = 0
    if mode == "points":
        # 免費每日生成配額（止血閥；BYOK/訂閱燒自己的額度，不設限）
        from datetime import date
        quota_key = f"gen_{user_id}_{date.today().isoformat()}"
        daily_limit = settings["free_daily_quota"]
        if repo.get_daily_counter(quota_key) >= daily_limit:
            line_client.reply(reply_token, [
                f"今日生成次數已達上限（{daily_limit} 次），明天再來吧！\n"
                "（自備 API key 的使用者不受此限，可到「設定」切換）"])
            return
        try:
            points.charge(user_id, cost, job_id="pending")
            charged = cost
        except points.InsufficientPoints as e:
            line_client.reply(reply_token, [f"點數不足 😢 餘額 {e.balance} 點，需要 {e.needed} 點。"])
            return
        repo.incr_daily_counter(quota_key, 1)

    # 先建 course 並掛到使用者身上 → 「我的課程」與智慧客服立刻看得到生成中狀態
    course_id = repo.create_course(owner=user_id, topic=topic, lesson_count=n)
    repo.update_course(course_id, {"charged_points": charged, "stage": "排隊中",
                                   "advanced_from": advanced_from, "level": level,
                                   "contract": contract})
    repo.upsert_user(user_id, {"last_course_id": course_id})

    kind = "進階課程" if advanced_from else "課程"
    line_client.reply(reply_token, [
        f"收到！開始為你策展「{topic}」{kind}。\n"
        "AI 會逐支深度分析候選影片，約需 30～60 分鐘（測試階段速度）。\n"
        "完成會通知你；等待時輸入「我的課程」可以看進度 🚀"
    ])
    background.add_task(_run_generation, user_id, topic, n, charged, course_id, advanced_from)


def _run_generation(user_id: str, topic: str, n: int, charged: int,
                    course_id: str, advanced_from: str | None = None) -> None:
    worker.generate_course(user_id, topic, n, charged_points=charged,
                           course_id=course_id, advanced_from=advanced_from)
    course = get_repo().get_course(course_id)
    if course and course.get("status") == "ready":
        get_repo().upsert_user(user_id, {"active_course_id": course_id})
