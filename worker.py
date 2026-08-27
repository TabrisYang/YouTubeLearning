"""course_worker：背景課程生成流程（架構文件第四節管線的總指揮）。

搜尋 → 粗篩 → 字幕 → 評估（eval）→ 編排（curate）→ 存課程 → Push 完成通知。
points 模式：開跑前先扣點，任何失敗全額退點 + 道歉訊息。
"""
from __future__ import annotations

import logging

from billing import meter, points
from delivery import formatter, line_client
from pipeline import curator, filters, searcher, transcript
from pipeline.models import LearningContract, UserContext, VideoEvaluation
from storage.firestore_repo import get_repo

logger = logging.getLogger(__name__)


class ByokKeyMissing(Exception):
    """BYOK/OAuth 的 key 不在記憶體（過期或重啟）→ 引導使用者重新設定。"""


def build_user_ctx(user_id: str) -> UserContext:
    """從 users/{id}（非敏感偏好）+ keyvault（記憶體中的 key）組出 llm 路由脈絡。

    v2.1：key 一律不儲存 —— Firestore 只有 provider/selected_model，
    key 只在 keyvault 記憶體，過期或容器重啟即消失。
    """
    import keyvault

    user = get_repo().get_user(user_id) or {}
    ctx = UserContext(user_id=user_id, billing_mode=user.get("billing_mode", "points"))
    if ctx.billing_mode == "byok_api_key":
        byok = user.get("byok", {})
        ctx.byok_provider = byok.get("provider")
        ctx.byok_model = byok.get("selected_model")
        ctx.byok_api_key = keyvault.get_api_key(user_id, "llm")
        if not ctx.byok_api_key:
            raise ByokKeyMissing("你的 API key 已過期（安全起見系統不儲存），請到「設定」重新輸入")
    elif ctx.billing_mode == "oauth":
        ctx.oauth_source = user.get("oauth_source", "token")
        ctx.oauth_model = user.get("oauth_model")
        if ctx.oauth_source != "cli":   # CLI 模式免憑證（由本機 CLI 自管）
            ctx.oauth_token = keyvault.get_api_key(user_id, "oauth")
            if not ctx.oauth_token:
                raise ByokKeyMissing("訂閱制 token 已過期，請到「設定」重新輸入")
    ctx.youtube_api_key = keyvault.get_api_key(user_id, "youtube")
    return ctx


# 單次課程生成的 YouTube quota 預估（5 組關鍵字 × search 100 units + metadata 補查）
_QUOTA_PER_COURSE = 600

# 契約吻合度低於此值 → 剔除（評估時有給 contract_fit 才適用）
_MIN_CONTRACT_FIT = 4.0


def _passes_contract(ev: VideoEvaluation) -> bool:
    """評估後的守門：過時、導流/賣課、與學習契約吻合度過低的影片一律剔除。"""
    if ev.is_outdated or ev.is_promotional:
        return False
    if ev.contract_fit is not None and ev.contract_fit < _MIN_CONTRACT_FIT:
        return False
    return True


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def generate_course(user_id: str, topic: str, lesson_count: int,
                    charged_points: int = 0, notify: bool = True,
                    course_id: str | None = None,
                    advanced_from: str | None = None) -> str:
    """完整生成一門課。回傳 course_id。charged_points 是 webhook 已扣的點數（失敗退還）。

    course_id 有值 = 重跑既有課程（卡住任務掃描 / waitlist 補跑），不重複建 course。
    advanced_from = 基礎課程 course_id：排除已上過的影片、往更深難度編排（進階開課）。
    """
    repo = get_repo()
    if course_id is None:
        course_id = repo.create_course(owner=user_id, topic=topic, lesson_count=lesson_count)
        repo.update_course(course_id, {"charged_points": charged_points})
    else:
        repo.update_course(course_id, {"status": "generating"})
        advanced_from = advanced_from or (repo.get_course(course_id) or {}).get("advanced_from")
    if advanced_from:
        repo.update_course(course_id, {"advanced_from": advanced_from})
    job_id = course_id  # usage_logs 以 course_id 為 job 單位

    # 學習契約（grill 式開課對話的產出）：搜尋/粗篩/評估/編排共同服從的規格。
    # 無契約（一行式開課、LLM 退路流程）時依 level 建預設契約。
    course_doc = repo.get_course(course_id) or {}
    contract: LearningContract | None = None
    if course_doc.get("contract"):
        try:
            contract = LearningContract(**course_doc["contract"])
        except Exception:
            logger.warning("[%s] 課程契約資料非法，改用預設", course_id)
    if contract is None:
        from delivery.intake_flow import default_contract

        contract = default_contract(topic, course_doc.get("level"))
        repo.update_course(course_id, {"contract": contract.model_dump()})
    # 診斷：實際生效的契約進 log（D4 追查「契約有沒有傳到管線」用）
    logger.info("[%s] 學習契約：%s", course_id, "｜".join(contract.summary_lines()))

    # 程度診斷（逐步開課時使用者自報）→ 編排的難度起點
    level_notes = {
        "beginner": "使用者是零基礎新手：從難度 1-2 起步，第一堂必須完全不需先備知識",
        "some": "使用者有一些基礎：跳過純入門內容，從難度 2-3 起步",
        "advanced": "使用者已有相當基礎：以難度 3 以上為主，聚焦深入與實戰",
    }
    extra_note = level_notes.get(course_doc.get("level", ""), "")

    # 進階模式：載入基礎課程的影片（排除）與涵蓋內容（給編排參考）
    exclude_ids: set[str] = set()
    if advanced_from:
        prev_lessons = repo.get_lessons(advanced_from)
        exclude_ids = {l["video_id"] for l in prev_lessons}
        covered = "、".join(l["title"] for l in prev_lessons[:10])
        advanced_note = (f"這是進階課程。使用者已完成同主題的基礎課程（內容：{covered}），"
                         "請以難度 3 以上為主、聚焦深入與實戰，禁止安排重複或過於入門的內容")
        extra_note = "；".join(filter(None, [extra_note, advanced_note]))

    try:
        user_ctx = build_user_ctx(user_id)

        # 0. YouTube quota 檢查（在燒任何 LLM 錢之前；使用者自備 key 免檢）
        if not user_ctx.youtube_api_key:
            quota_key = f"yt_quota_{_today()}"
            from config import get_settings

            if repo.get_daily_counter(quota_key) + _QUOTA_PER_COURSE > get_settings().youtube_daily_quota:
                repo.update_course(course_id, {"status": "waitlisted"})
                if notify:
                    line_client.push(user_id, [
                        f"📋 今日搜尋名額已滿，課程「{topic}」已排入等候，明天會自動補跑（點數不退、不重扣）。"
                    ], reason="course_ready")
                return course_id

        # 1. 主題展開 + 搜尋（使用者有自備 YouTube key 就用他的額度）
        repo.update_course(course_id, {"stage": "搜尋影片中", "progress_pct": 5})
        keywords = searcher.expand_topic(topic, user_ctx, job_id=job_id,
                                         advanced=bool(advanced_from),
                                         contract=contract)
        candidates, quota_used = searcher.search_videos(keywords, api_key=user_ctx.youtube_api_key)
        if not user_ctx.youtube_api_key:
            repo.incr_daily_counter(f"yt_quota_{_today()}", quota_used)
        logger.info("[%s] 搜尋 %d 支（quota %d）", course_id, len(candidates), quota_used)

        # 2. 規則粗篩（套用使用者偏好：同頻道上限、語言）
        user_prefs = (repo.get_user(user_id) or {}).get("prefs", {})
        shortlist = filters.apply(candidates, exclude_ids=exclude_ids, prefs=user_prefs,
                                  contract=contract)
        logger.info("[%s] 粗篩後 %d 支", course_id, len(shortlist))
        if not shortlist:
            raise RuntimeError("粗篩後沒有任何合格影片")

        # 3+4. 評估（逐支；失敗剔除）— 三層策略：
        #   字幕可用 → 文字評估（已被 YouTube 大面積封鎖，可遇不可求）
        #   前 video_eval_limit 支 → Gemini 直接看影片（主力路線）
        #   其餘/失敗 → metadata + 章節 + 熱門留言 降級評估
        llm_settings = repo.get_llm_settings()
        video_limit = int(llm_settings.get("video_eval_limit", 15))
        evaluations = []
        basis_stats = {"transcript": 0, "video": 0, "metadata": 0}
        for i, c in enumerate(shortlist, 1):
            repo.update_course(course_id, {
                "stage": f"評估影片 {i}/{len(shortlist)}",
                "progress_pct": 10 + int(75 * i / len(shortlist)),
            })
            ev = None
            tr = transcript.fetch(c)
            if tr.analysis_basis == "transcript":
                ev = curator.evaluate(c, tr, user_ctx, job_id=job_id, contract=contract)
            elif i <= video_limit:
                ev = curator.evaluate_by_video(c, user_ctx, job_id=job_id,
                                               contract=contract)
            if ev is None:
                comments = searcher.fetch_top_comments(
                    c.video_id, api_key=user_ctx.youtube_api_key)
                if comments:
                    tr.text += "\n熱門留言:\n" + "\n".join(f"- {cm}" for cm in comments)
                ev = curator.evaluate(c, tr, user_ctx, job_id=job_id, contract=contract)
            if ev:
                basis_stats[ev.analysis_basis] += 1
                if _passes_contract(ev):
                    evaluations.append(ev)
        repo.update_course(course_id, {
            "basis_stats": basis_stats,
            "transcript_rate": round(basis_stats["transcript"] / len(shortlist), 2),
            "video_rate": round(basis_stats["video"] / len(shortlist), 2),
        })
        logger.info("[%s] 評估存活 %d 支（字幕 %d / 影片理解 %d / metadata %d）",
                    course_id, len(evaluations), basis_stats["transcript"],
                    basis_stats["video"], basis_stats["metadata"])
        if len(evaluations) < 3:
            raise RuntimeError(f"評估後僅剩 {len(evaluations)} 支影片，不足以成課")

        # 5. 編排
        repo.update_course(course_id, {"stage": "編排課綱中", "progress_pct": 88})
        plan = curator.curate(topic, lesson_count, shortlist, evaluations,
                              user_ctx, job_id=job_id, extra_note=extra_note,
                              contract=contract)
        if not plan.lessons:
            raise RuntimeError("編排結果沒有任何課程")

        repo.save_lessons(course_id, [l.model_dump() for l in plan.lessons])
        repo.update_course(course_id, {
            "status": "ready",
            "stage": "完成",
            "progress_pct": 100,
            "lesson_count": len(plan.lessons),
            "honest_note": plan.honest_note,
            "total_cost_usd": meter.job_total_cost_usd(job_id),
            "charged_points": charged_points,
        })
        repo.set_progress(user_id, course_id, {"current_lesson": 1, "completed": [], "active": True})

        if notify:
            note = f"\n⚠️ {plan.honest_note}" if plan.honest_note else ""
            deep = basis_stats["video"] + basis_stats["transcript"]
            line_client.push(user_id, [
                f"🎓 你的課程「{topic}」完成了！共 {len(plan.lessons)} 堂。{note}\n"
                f"📊 品質說明：{deep}/{len(shortlist)} 支候選影片經 AI 深度分析\n"
                f"點選單「今日課程」開始第一堂。"
            ], reason="course_ready")
        return course_id

    except searcher.QuotaExceeded:
        # 搜尋到一半 quota 見底 → 進 waitlist，隔日掃描補跑（不退點、不算失敗）
        logger.warning("[%s] YouTube quota 用盡，進 waitlist", course_id)
        repo.update_course(course_id, {"status": "waitlisted"})
        if notify:
            line_client.push(user_id, [
                f"📋 今日搜尋名額已滿，課程「{topic}」已排入等候，明天會自動補跑（點數不退、不重扣）。"
            ], reason="course_ready")
        return course_id

    except Exception as e:
        logger.exception("[%s] 生成失敗", course_id)
        reason = _friendly_reason(e)
        repo.update_course(course_id, {"status": "failed", "fail_reason": reason})
        if charged_points > 0:
            points.refund(user_id, charged_points, job_id)  # 沉沒 token 平台吸收
        if notify:
            line_client.push(user_id, [
                f"😢 課程「{topic}」生成失敗，"
                + (f"已退回 {charged_points} 點。" if charged_points else "")
                + f"\n原因：{reason}\n\n輸入「重試」可直接重新生成，不用重填主題。"
            ], reason="course_ready")
        return course_id


def check_dead_links() -> dict:
    """死鏈巡檢：查所有 ready 課程的影片是否還存在/公開，標記 lesson.dead。

    用 videos.list（1 unit/50 支）超省 quota；Cloud Scheduler 每日打 /admin/check-links。
    """
    import httpx

    from config import get_settings
    from pipeline.searcher import YT_API

    api_key = get_settings().youtube_api_key
    if not api_key:
        return {"error": "未設定平台 YOUTUBE_API_KEY，無法巡檢"}

    repo = get_repo()
    # 收集所有 ready 課程的影片 → 批次查詢
    lesson_index: dict[str, list[tuple[str, int]]] = {}  # video_id → [(course_id, order)]
    for course in repo.list_courses_by_status("ready"):
        for lesson in repo.get_lessons(course["course_id"]):
            lesson_index.setdefault(lesson["video_id"], []).append(
                (course["course_id"], lesson["order"]))

    ids = list(lesson_index)
    alive: set[str] = set()
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(ids), 50):
            r = client.get(f"{YT_API}/videos", params={
                "key": api_key, "part": "status", "id": ",".join(ids[i:i + 50])})
            r.raise_for_status()
            for item in r.json().get("items", []):
                if item.get("status", {}).get("privacyStatus") == "public":
                    alive.add(item["id"])

    dead = [vid for vid in ids if vid not in alive]
    for vid in dead:
        for course_id, order in lesson_index[vid]:
            repo.update_lesson(course_id, order, {"dead": True})
    logger.info("死鏈巡檢：%d 支影片，%d 支失效", len(ids), len(dead))
    return {"checked": len(ids), "dead": len(dead)}


def _friendly_reason(e: Exception) -> str:
    """把技術錯誤翻成使用者看得懂的原因 + 下一步（存進 course.fail_reason 供客服引用）。"""
    msg = str(e)
    if isinstance(e, ByokKeyMissing):
        return msg
    if "timed out" in msg or "TimeoutExpired" in type(e).__name__:
        return "模型回應逾時（編排大量課綱較耗時）。輸入「重試」重新生成，或到「設定」換較快的模型"
    if "YouTube API key" in msg:
        return "尚未設定 YouTube API key。請輸入「設定」→ 選 3 貼上 key（免費申請）"
    if "平台未設定" in msg:
        return "點數制生成暫時未開放，請輸入「設定」→ 選 2 改用自備 API key"
    if "不足以成課" in msg or "沒有任何合格影片" in msg or "沒有任何課程" in msg:
        return "這個主題找到的合格影片太少，換個更常見的主題或減少堂數試試"
    return "系統處理時發生錯誤，請稍後再試一次"
