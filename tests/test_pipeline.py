"""單元測試：filters 規則、meter 計費、router 認證解析、points 帳本。全部離線可跑。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billing import meter, points
from billing.price_table import PRICE_TABLE, get_price
from llm import router
from pipeline import filters
from pipeline.models import LLMUsage, UserContext, VideoCandidate
from storage import firestore_repo
from storage.firestore_repo import InMemoryRepo

NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_repo(monkeypatch):
    repo = InMemoryRepo()
    monkeypatch.setattr(firestore_repo, "_repo", repo)
    yield repo


def make_candidate(**kw) -> VideoCandidate:
    base = dict(
        video_id="v1", title="Python 入門教學", description="測試",
        channel_title="ch", channel_id="c1", duration_sec=600,
        view_count=10_000, like_count=400, subscriber_count=20_000,
        published_at="2026-01-01T00:00:00Z",
    )
    base.update(kw)
    return VideoCandidate(**base)


# ---------- filters ----------

class TestFilters:
    def test_duration_hard_limits(self):
        too_short = make_candidate(video_id="a", duration_sec=3 * 60)
        too_long = make_candidate(video_id="b", duration_sec=46 * 60)
        ok = make_candidate(video_id="c", duration_sec=10 * 60)
        result = filters.apply([too_short, too_long, ok], now=NOW)
        assert [c.video_id for c in result] == ["c"]

    def test_low_views_excluded(self):
        cold = make_candidate(video_id="a", view_count=100)
        assert filters.apply([cold], now=NOW) == []

    def test_chinese_priority(self):
        en = make_candidate(video_id="en", title="Python tutorial for beginners",
                            description="english only", like_count=2000)  # 分數較高
        zh = make_candidate(video_id="zh", like_count=100)
        result = filters.apply([en, zh], target=1, now=NOW)
        assert result[0].video_id == "zh"  # 中文優先於分數

    def test_english_fills_when_insufficient(self):
        zh = make_candidate(video_id="zh")
        en = make_candidate(video_id="en", title="Python tutorial", description="english")
        result = filters.apply([zh, en], target=5, now=NOW)
        assert {c.video_id for c in result} == {"zh", "en"}

    def test_recency_decay(self):
        assert filters._recency_score("2026-01-01T00:00:00Z", NOW) == 1.0
        assert filters._recency_score("2020-01-01T00:00:00Z", NOW) == 0.0

    def test_takes_top_25(self):
        cands = [make_candidate(video_id=f"v{i}") for i in range(40)]
        assert len(filters.apply(cands, now=NOW)) == 25

    def test_max_per_channel_pref(self):
        """偏好：同頻道上限 → 強制來源多樣性。"""
        same = [make_candidate(video_id=f"a{i}", channel_id="chA", like_count=5000)
                for i in range(10)]
        other = [make_candidate(video_id=f"b{i}", channel_id=f"ch{i}") for i in range(5)]
        result = filters.apply(same + other, target=10, now=NOW,
                               prefs={"max_per_channel": 2})
        assert sum(1 for c in result if c.channel_id == "chA") == 2
        # 不設上限 → chA 可以洗版
        result = filters.apply(same + other, target=10, now=NOW)
        assert sum(1 for c in result if c.channel_id == "chA") == 10

    def test_zh_only_pref(self):
        zh = make_candidate(video_id="zh")
        en = make_candidate(video_id="en", title="Python tutorial", description="english")
        result = filters.apply([zh, en], target=5, now=NOW, prefs={"language": "zh_only"})
        assert [c.video_id for c in result] == ["zh"]

    def test_sample_for_eval_covers_head_mid_tail(self):
        """評估取樣：長字幕改為頭/中/尾三段，不再只看開頭。"""
        from pipeline.transcript import sample_for_eval

        text = "頭" * 3000 + "中" * 3000 + "尾" * 3000
        sampled = sample_for_eval(text, budget=3000)
        assert "頭" in sampled and "中" in sampled and "尾" in sampled
        assert "中段節錄" in sampled and "結尾節錄" in sampled
        assert len(sampled) < 3300          # 預算沒有爆
        short = "短字幕內容"
        assert sample_for_eval(short, budget=3000) == short   # 短的不動

    def test_exclude_ids(self):
        """進階開課：排除基礎課程已上過的影片。"""
        a, b = make_candidate(video_id="a"), make_candidate(video_id="b")
        result = filters.apply([a, b], now=NOW, exclude_ids={"a"})
        assert [c.video_id for c in result] == ["b"]


# ---------- billing.meter ----------

class TestMeter:
    def test_cost_usd_known_model(self):
        usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        p = PRICE_TABLE["claude-haiku-4-5"]
        assert meter.cost_usd("claude-haiku-4-5", usage) == p["input"] + p["output"]

    def test_cost_usd_unknown_model_is_zero(self):
        assert meter.cost_usd("mystery-model-9000", LLMUsage(input_tokens=1000)) == 0.0

    def test_price_lookup_with_date_suffix(self):
        assert get_price("claude-sonnet-4-6-20250101") == PRICE_TABLE["claude-sonnet-4-6"]

    def test_usd_to_points_ceil(self):
        settings = {"usd_to_twd": 32.5, "markup": 3.0}
        # 0.2 USD * 32.5 * 3 = 19.5 → 進位 20
        assert meter.usd_to_points(0.2, settings) == 20

    def test_job_total_accumulates(self, fresh_repo):
        from pipeline.models import LLMCallRecord

        for cost in (0.01, 0.02):
            meter.record_call("u1", "points", "job1", LLMCallRecord(
                purpose="eval", provider="anthropic", model="claude-haiku-4-5",
                input_tokens=10, output_tokens=10, cost_usd=cost))
        assert meter.job_total_cost_usd("job1") == pytest.approx(0.03)


# ---------- billing.points ----------

class TestPoints:
    def test_topup_charge_refund_flow(self):
        points.topup("u1", 100)
        assert points.balance("u1") == 100
        points.charge("u1", 20, job_id="j1")
        assert points.balance("u1") == 80
        points.refund("u1", 20, job_id="j1")
        assert points.balance("u1") == 100

    def test_insufficient_points(self):
        points.topup("u2", 5)
        with pytest.raises(points.InsufficientPoints):
            points.charge("u2", 20, job_id="j1")
        assert points.balance("u2") == 5  # 失敗不留紀錄


# ---------- llm.router ----------

class TestRouter:
    SETTINGS = {"eval_model": "claude-haiku-4-5", "curate_model": "claude-sonnet-4-6",
                "intent_model": "claude-haiku-4-5"}

    def test_provider_of_model(self):
        assert router.provider_of_model("claude-haiku-4-5") == "anthropic"
        assert router.provider_of_model("gpt-4o") == "openai"
        assert router.provider_of_model("gemini-2.0-flash") == "google"

    def test_points_mode_uses_platform_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        from config import get_settings
        get_settings.cache_clear()
        ctx = UserContext(user_id="u", billing_mode="points")
        provider, model, key = router.resolve("eval", ctx, self.SETTINGS)
        assert (provider, model, key) == ("anthropic", "claude-haiku-4-5", "sk-platform")
        get_settings.cache_clear()

    def test_byok_uses_user_key_and_model(self):
        ctx = UserContext(user_id="u", billing_mode="byok_api_key",
                          byok_provider="openai", byok_api_key="sk-user", byok_model="gpt-4o")
        assert router.resolve("curate", ctx, self.SETTINGS) == ("openai", "gpt-4o", "sk-user")

    def test_byok_missing_config_raises(self):
        ctx = UserContext(user_id="u", billing_mode="byok_api_key")
        with pytest.raises(ValueError):
            router.resolve("eval", ctx, self.SETTINGS)

    def test_oauth_blocked_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_OAUTH", raising=False)
        from config import get_settings
        get_settings.cache_clear()
        ctx = UserContext(user_id="u", billing_mode="oauth", oauth_token="tok")
        with pytest.raises(PermissionError):
            router.resolve("eval", ctx, self.SETTINGS)
        get_settings.cache_clear()

    def test_unknown_purpose_raises(self):
        with pytest.raises(ValueError):
            router.resolve("nonsense", UserContext(user_id="u"), self.SETTINGS)

    def test_oauth_cli_routes_to_claude_cli_provider(self, monkeypatch):
        """訂閱制（本機 CLI 模式）：不需憑證；eval 自動走 haiku 提速，curate 用選定模型。"""
        monkeypatch.setenv("ENABLE_OAUTH", "true")
        from config import get_settings
        get_settings.cache_clear()
        ctx = UserContext(user_id="u", billing_mode="oauth",
                          oauth_source="cli", oauth_model="opus")
        assert router.resolve("eval", ctx, self.SETTINGS) == ("claude_cli", "haiku", "local")
        assert router.resolve("curate", ctx, self.SETTINGS) == ("claude_cli", "opus", "local")
        get_settings.cache_clear()

    def test_cli_alias_price_lookup(self):
        assert get_price("sonnet") == PRICE_TABLE["claude-sonnet-4-6"]
        assert get_price("haiku") == PRICE_TABLE["claude-haiku-4-5"]

    def test_oauth_uses_bearer_headers(self):
        """訂閱制 token 必須走 Bearer 標頭（x-api-key 會 401）。"""
        from llm.providers.anthropic_p import AnthropicProvider

        p_key = AnthropicProvider("sk-normal")
        assert "x-api-key" in p_key._headers()
        p_oauth = AnthropicProvider("oauth-token", auth_scheme="oauth")
        h = p_oauth._headers()
        assert h["Authorization"] == "Bearer oauth-token"
        assert "x-api-key" not in h
        assert "anthropic-beta" in h


# ---------- keyvault（v2.1：key 不儲存，只放記憶體 + TTL） ----------

class TestKeyVault:
    def test_set_get_clear(self):
        import keyvault

        keyvault.set_api_key("u1", "llm", "sk-test")
        assert keyvault.get_api_key("u1", "llm") == "sk-test"
        keyvault.clear_api_key("u1", "llm")
        assert keyvault.get_api_key("u1", "llm") is None

    def test_ttl_expiry(self, monkeypatch):
        from keyvault import TTLStore

        store = TTLStore(ttl_seconds=100)
        store.set("k", "v")
        assert store.get("k") == "v"
        # 快轉時間讓它過期
        import time
        real = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: real + 101)
        assert store.get("k") is None


# ---------- worker.build_user_ctx（key 只來自記憶體） ----------

class TestBuildUserCtx:
    def test_byok_key_from_vault(self, fresh_repo):
        import keyvault
        import worker

        fresh_repo.upsert_user("u1", {
            "billing_mode": "byok_api_key",
            "byok": {"provider": "anthropic", "selected_model": "claude-haiku-4-5"},
        })
        keyvault.set_api_key("u1", "llm", "sk-in-memory")
        ctx = worker.build_user_ctx("u1")
        assert ctx.byok_api_key == "sk-in-memory"
        assert ctx.byok_provider == "anthropic"
        keyvault.clear_api_key("u1", "llm")

    def test_byok_key_missing_raises(self, fresh_repo):
        import keyvault
        import worker

        fresh_repo.upsert_user("u2", {
            "billing_mode": "byok_api_key",
            "byok": {"provider": "openai", "selected_model": "gpt-4o"},
        })
        keyvault.clear_api_key("u2", "llm")
        with pytest.raises(worker.ByokKeyMissing):
            worker.build_user_ctx("u2")

    def test_firestore_never_holds_key(self, fresh_repo):
        """資安驗證：users 文件內不得出現任何 key 欄位。"""
        fresh_repo.upsert_user("u3", {
            "billing_mode": "byok_api_key",
            "byok": {"provider": "anthropic", "selected_model": "claude-haiku-4-5"},
        })
        user = fresh_repo.get_user("u3")
        assert set(user["byok"]) == {"provider", "selected_model"}  # 只有非敏感欄位
        assert "sk-" not in str(user)                               # 不含任何 key 明文


# ---------- 每日配額 / waitlist / 卡住任務掃描 ----------

class TestQuotaAndScanner:
    def test_daily_counter(self, fresh_repo):
        assert fresh_repo.get_daily_counter("k") == 0
        assert fresh_repo.incr_daily_counter("k", 3) == 3
        assert fresh_repo.incr_daily_counter("k", 2) == 5

    def test_free_daily_quota_blocks_second_generation(self, fresh_repo, monkeypatch):
        """points 模式：當日達 free_daily_quota（預設 1）後，確認開課被擋下且不扣點。"""
        from fastapi import BackgroundTasks

        import main
        from config import get_settings
        from delivery import line_client

        # 預檢需要平台憑證齊備
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()

        points.topup("uq", 100)
        main._handle_text("uq", "tok", "確認開課 Python 3", BackgroundTasks())
        msgs = line_client.drain_dev_outbox()
        assert any("開始為你策展" in m["text"] for m in msgs)
        assert points.balance("uq") == 80          # 已扣 20

        # 讓第一門課完成（否則會先被防重複機制擋下）
        first_id = (fresh_repo.get_user("uq") or {})["last_course_id"]
        fresh_repo.update_course(first_id, {"status": "ready"})

        main._handle_text("uq", "tok", "確認開課 Docker 3", BackgroundTasks())
        msgs = line_client.drain_dev_outbox()
        assert any("已達上限" in m["text"] for m in msgs)
        assert points.balance("uq") == 80          # 沒有再扣
        get_settings.cache_clear()


class TestGuidedCourseFlow:
    """「開課」逐步引導：問主題 → 問堂數 → 報價 → 確認即生成。"""

    def _msg(self, user_id, text):
        from fastapi import BackgroundTasks

        import main
        from delivery import line_client

        main._handle_text(user_id, "tok", text, BackgroundTasks())
        return [m["text"] for m in line_client.drain_dev_outbox()]

    def test_full_guided_flow(self, fresh_repo, monkeypatch):
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        points.topup("ugc", 100)

        assert any("想學什麼主題" in m for m in self._msg("ugc", "開課"))
        assert any("程度" in m for m in self._msg("ugc", "AI工作流"))   # 程度診斷
        assert any("幾堂課" in m for m in self._msg("ugc", "1"))        # 完全新手
        msgs = self._msg("ugc", "5")
        assert any("主題：AI工作流" in m and "5 堂" in m for m in msgs)
        assert any("開始為你策展" in m for m in self._msg("ugc", "確認"))
        assert points.balance("ugc") == 80

        # 生成中：我的課程看得到進度（含百分比）、再開課會被防重複擋下
        course_id = (fresh_repo.get_user("ugc") or {})["last_course_id"]
        fresh_repo.update_course(course_id, {"stage": "評估影片 12/25", "progress_pct": 46})
        msgs = self._msg("ugc", "我的課程")
        assert any("進度 46%" in m for m in msgs)
        assert any("已經有一門" in m for m in self._msg("ugc", "確認開課 Docker 3"))
        get_settings.cache_clear()

    def test_retry_after_failure(self, fresh_repo, monkeypatch):
        """「重試」：上次失敗直接重新生成同主題同堂數，免重填。"""
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        points.topup("urt", 100)

        course_id = fresh_repo.create_course("urt", "技術分析", 5)
        fresh_repo.update_course(course_id, {"status": "failed", "fail_reason": "模型回應逾時"})
        fresh_repo.upsert_user("urt", {"last_course_id": course_id})

        msgs = self._msg("urt", "重試")
        assert any("開始為你策展「技術分析」" in m for m in msgs)
        assert points.balance("urt") == 80         # points 模式重試照常扣點（上次已退）

        msgs = self._msg("urt", "重試")             # 生成中再按重試 → 顯示進度不重開
        assert any("還在跑" in m for m in msgs)
        get_settings.cache_clear()

    def test_retry_with_nothing_to_retry(self, fresh_repo):
        msgs = self._msg("urt2", "重試")
        assert any("沒有失敗的課程" in m for m in msgs)

    def test_lesson_list_and_jump(self, fresh_repo):
        """課程列表：依序標註第幾堂與進度；「第 2 堂」可跳看。"""
        cid = fresh_repo.create_course("ul", "Python", 3)
        fresh_repo.update_course(cid, {"status": "ready"})
        fresh_repo.save_lessons(cid, [
            {"order": i, "video_id": f"v{i}", "video_url": f"https://youtu.be/v{i}",
             "title": f"第{i}課主題", "channel": "ch", "duration_sec": 600,
             "difficulty": 2, "summary": "s", "learning_goals": ["g"], "quiz": [],
             "bridge_note": ""} for i in (1, 2, 3)])
        fresh_repo.upsert_user("ul", {"active_course_id": cid})
        fresh_repo.set_progress("ul", cid, {"current_lesson": 2, "completed": [1], "active": True})

        msgs = self._msg("ul", "課程列表")
        assert any("✅ 第 1 堂" in m and "▶️ 第 2 堂" in m and "▫️ 第 3 堂" in m for m in msgs)

        from delivery import line_client
        from fastapi import BackgroundTasks
        import main
        main._handle_text("ul", "tok", "第 3 堂", BackgroundTasks())
        out = line_client.drain_dev_outbox()
        assert any(o.get("type") == "flex" and "第 3 堂" in o.get("altText", "") for o in out)

    def test_advanced_course_command(self, fresh_repo, monkeypatch):
        """進階開課：以完成的課為基底、記 advanced_from。"""
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        points.topup("ua", 100)

        base = fresh_repo.create_course("ua", "技術分析", 5)
        fresh_repo.update_course(base, {"status": "ready"})
        fresh_repo.upsert_user("ua", {"active_course_id": base, "last_course_id": base})

        msgs = self._msg("ua", "進階開課")
        assert any("進階課程" in m for m in msgs)
        new_id = (fresh_repo.get_user("ua") or {})["last_course_id"]
        assert new_id != base
        assert fresh_repo.get_course(new_id)["advanced_from"] == base
        get_settings.cache_clear()

    def test_advanced_requires_completed_course(self, fresh_repo):
        msgs = self._msg("ua2", "進階開課")
        assert any("要先完成一門課" in m for m in msgs)

    def test_invalid_count_reprompts(self, fresh_repo, monkeypatch):
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()

        self._msg("ugc2", "開課")
        self._msg("ugc2", "Python")
        self._msg("ugc2", "2")     # 程度：有一些基礎
        assert any("1～20 的數字" in m for m in self._msg("ugc2", "很多堂"))
        get_settings.cache_clear()

    def test_level_recorded_on_course(self, fresh_repo, monkeypatch):
        """程度診斷會存進 course.level → worker 轉成編排的難度指示。"""
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        points.topup("ugl", 100)
        self._msg("ugl", "開課")
        self._msg("ugl", "Python")
        self._msg("ugl", "3")      # 想深入進階
        self._msg("ugl", "5")
        self._msg("ugl", "確認")
        cid = (fresh_repo.get_user("ugl") or {})["last_course_id"]
        assert fresh_repo.get_course(cid)["level"] == "advanced"
        get_settings.cache_clear()

    def test_cancel_exits_flow(self, fresh_repo, monkeypatch):
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()

        self._msg("ugc3", "開課")
        assert any("已取消" in m for m in self._msg("ugc3", "取消"))
        import keyvault
        assert keyvault.sessions.get("course:ugc3") is None
        get_settings.cache_clear()


class TestRetention:
    """留存與品質資料：streak、讚/爛 回饋、複習、死鏈巡檢。"""

    def _msg(self, user_id, text):
        from fastapi import BackgroundTasks

        import main
        from delivery import line_client

        main._handle_text(user_id, "tok", text, BackgroundTasks())
        return [m["text"] if m.get("type") == "text" else m for m in line_client.drain_dev_outbox()]

    def _make_course(self, repo, user_id, completed=(1,), current=2):
        cid = repo.create_course(user_id, "Python", 3)
        repo.update_course(cid, {"status": "ready"})
        repo.save_lessons(cid, [
            {"order": i, "video_id": f"v{i}", "video_url": f"https://youtu.be/v{i}",
             "title": f"第{i}課", "channel": "ch", "duration_sec": 600, "difficulty": 2,
             "summary": "s", "learning_goals": ["g"],
             "quiz": [{"q": f"Q{i}", "a": f"A{i}"}], "bridge_note": ""} for i in (1, 2, 3)])
        repo.upsert_user(user_id, {"active_course_id": cid})
        repo.set_progress(user_id, cid, {"current_lesson": current,
                                         "completed": list(completed), "active": True})
        return cid

    def test_streak_logic(self, fresh_repo):
        import main
        from datetime import date, timedelta

        assert main._bump_streak("us") == 1
        assert main._bump_streak("us") == 1          # 同日不重複累計
        fresh_repo.upsert_user("us", {
            "last_learn_date": (date.today() - timedelta(days=1)).isoformat(), "streak": 3})
        assert main._bump_streak("us") == 4          # 昨天有學 → +1
        fresh_repo.upsert_user("us", {
            "last_learn_date": (date.today() - timedelta(days=5)).isoformat(), "streak": 9})
        assert main._bump_streak("us") == 1          # 斷了 → 歸 1

    def test_complete_shows_streak_and_asks_feedback(self, fresh_repo):
        self._make_course(fresh_repo, "uf1", completed=(), current=1)
        msgs = self._msg("uf1", "完成")
        assert any("連續學習 1 天" in str(m) for m in msgs)
        assert any("讚」或「爛" in str(m) for m in msgs)

    def test_feedback_recorded(self, fresh_repo):
        cid = self._make_course(fresh_repo, "uf2")
        msgs = self._msg("uf2", "讚")
        assert any("謝謝回饋" in str(m) for m in msgs)
        assert fresh_repo._feedback[-1] == {
            **fresh_repo._feedback[-1],
            "user_id": "uf2", "course_id": cid, "lesson_order": 1, "rating": "good"}

    def test_review_command(self, fresh_repo):
        self._make_course(fresh_repo, "uf3", completed=(1, 2), current=3)
        msgs = self._msg("uf3", "複習")
        assert any("複習時間" in str(m) for m in msgs)
        assert any("想好再看答案" in str(m) for m in msgs)

    def test_review_needs_completed_lessons(self, fresh_repo):
        self._make_course(fresh_repo, "uf4", completed=(), current=1)
        msgs = self._msg("uf4", "複習")
        assert any("先完成至少一堂課" in str(m) for m in msgs)

    def test_dead_link_check_marks_lessons(self, fresh_repo, monkeypatch):
        import httpx

        import worker
        from config import get_settings

        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        cid = self._make_course(fresh_repo, "uf5")

        class FakeResp:
            def raise_for_status(self): ...
            def json(self):
                # v2 消失（已刪除）、v3 轉私人 → 只有 v1 存活
                return {"items": [
                    {"id": "v1", "status": {"privacyStatus": "public"}},
                    {"id": "v3", "status": {"privacyStatus": "private"}},
                ]}

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *a): ...
            def get(self, *a, **kw): return FakeResp()

        monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
        result = worker.check_dead_links()
        assert result == {"checked": 3, "dead": 2}
        lessons = {l["order"]: l for l in fresh_repo.get_lessons(cid)}
        assert not lessons[1].get("dead")
        assert lessons[2].get("dead") and lessons[3].get("dead")

        # 課程列表顯示 ⚠️
        msgs = self._msg("uf5", "課程列表")
        assert any("⚠️影片已失效" in str(m) for m in msgs)
        get_settings.cache_clear()


class TestPrefsFlow:
    def test_prefs_setting_flow(self, fresh_repo):
        from delivery import settings_flow

        settings_flow.start("up")
        msgs = settings_flow.handle("up", "5")
        assert "同一個頻道最多選幾支" in msgs[0]
        msgs = settings_flow.handle("up", "1")       # 最多 2 支
        assert "語言偏好" in msgs[0]
        msgs = settings_flow.handle("up", "2")       # 只要中文
        assert "簡體中文" in msgs[0]                  # 第 3 題：繁簡
        msgs = settings_flow.handle("up", "1")       # 排除簡中
        assert "偏好已更新" in msgs[0]
        assert "按讚率 35%" in msgs[0]               # 選片依據透明化
        prefs = fresh_repo.get_user("up")["prefs"]
        assert prefs == {"max_per_channel": 2, "language": "zh_only",
                         "chinese_script": "no_simplified"}

    def test_gemini_key_flow(self, fresh_repo, monkeypatch):
        """BYOK 使用者引導自備 Gemini key：貼上 → 驗證 → 進 keyvault。"""
        import keyvault
        from delivery import settings_flow
        from llm.providers.base import LLMResponse
        from llm.providers.google_p import GoogleProvider
        from pipeline.models import LLMUsage

        monkeypatch.setattr(GoogleProvider, "complete",
                            lambda self, msgs, model, **kw:
                            LLMResponse("OK", LLMUsage(input_tokens=3, output_tokens=1)))
        fresh_repo.upsert_user("ug6", {"billing_mode": "byok_api_key"})
        settings_flow.start("ug6")
        msgs = settings_flow.handle("ug6", "6")
        assert "aistudio.google.com" in msgs[0]       # 引導連結
        msgs = settings_flow.handle("ug6", "AIzaSy-test-key")
        assert "驗證成功" in msgs[0]
        assert keyvault.get_api_key("ug6", "gemini") == "AIzaSy-test-key"
        keyvault.clear_api_key("ug6", "gemini")

    def test_gemini_key_not_needed_for_points_mode(self, fresh_repo):
        from delivery import settings_flow

        settings_flow.start("ug7")
        msgs = settings_flow.handle("ug7", "6")
        assert "不需要" in msgs[0]                    # 點數制：平台已含
        settings_flow.handle("ug7", "取消")


class TestKeyAdaptation:
    """開箱即用：填哪家的 key 就自動用哪家的模型。"""

    def test_google_only_switches_models_to_gemini(self, fresh_repo, monkeypatch):
        from config import get_settings

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "gemini-key")
        get_settings.cache_clear()
        settings = fresh_repo.get_llm_settings()
        assert settings["eval_model"].startswith("gemini")
        assert settings["curate_model"].startswith("gemini")
        get_settings.cache_clear()

    def test_anthropic_key_keeps_claude_defaults(self, fresh_repo, monkeypatch):
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        get_settings.cache_clear()
        settings = fresh_repo.get_llm_settings()
        assert settings["eval_model"].startswith("claude")
        get_settings.cache_clear()

    def test_no_keys_leaves_settings_untouched(self, fresh_repo, monkeypatch):
        from config import DEFAULT_LLM_SETTINGS, get_settings

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        get_settings.cache_clear()
        assert (fresh_repo.get_llm_settings()["eval_model"]
                == DEFAULT_LLM_SETTINGS["eval_model"])
        get_settings.cache_clear()


class TestSimplifiedChinese:
    def test_is_simplified_detection(self):
        s = make_candidate(video_id="s", title="一口气讲透K线技术分析，让你少走弯路", description="")
        t = make_candidate(video_id="t", title="技術分析教學：讓你看懂K線圖", description="")
        assert filters.is_simplified(s)
        assert not filters.is_simplified(t)

    def test_no_simplified_pref_excludes(self):
        s = make_candidate(video_id="s", title="一口气讲透K线技术分析，让你少走弯路")
        t = make_candidate(video_id="t", title="技術分析教學：讓你看懂K線圖")
        result = filters.apply([s, t], now=NOW, prefs={"chinese_script": "no_simplified"})
        assert [c.video_id for c in result] == ["t"]

    def test_simplified_ranked_below_traditional(self):
        # 數據相同時，繁中分數應高於簡中
        s = make_candidate(video_id="s", title="一口气讲透K线技术分析，让你少走弯路")
        t = make_candidate(video_id="t", title="技術分析教學：讓你看懂K線圖")
        assert filters.score(t, NOW) > filters.score(s, NOW)


class TestVideoEvalCredentials:
    def test_byok_without_gemini_key_raises(self, fresh_repo, monkeypatch):
        """BYOK（非 Google）未自備 Gemini key → 不得燒平台額度。"""
        import llm
        from pipeline.models import UserContext

        monkeypatch.setenv("GOOGLE_API_KEY", "platform-gemini")
        from config import get_settings
        get_settings.cache_clear()
        ctx = UserContext(user_id="uv", billing_mode="byok_api_key",
                          byok_provider="anthropic", byok_api_key="sk-x", byok_model="claude-haiku-4-5")
        with pytest.raises(ValueError, match="自備 Gemini key"):
            llm.analyze_video("https://youtu.be/x", "test", ctx)
        get_settings.cache_clear()

    def test_user_gemini_key_takes_priority(self, fresh_repo, monkeypatch):
        import keyvault
        import llm
        from llm.providers.base import LLMResponse
        from llm.providers.google_p import GoogleProvider
        from pipeline.models import LLMUsage, UserContext

        captured = {}

        def fake_init(self, api_key, auth_scheme="api_key"):
            captured["key"] = api_key
            self._api_key, self._auth_scheme = api_key, auth_scheme

        monkeypatch.setattr(GoogleProvider, "__init__", fake_init)
        monkeypatch.setattr(GoogleProvider, "complete_with_video",
                            lambda self, url, prompt, model, **kw:
                            LLMResponse("{}", LLMUsage(input_tokens=100, output_tokens=10)))
        keyvault.set_api_key("uv2", "gemini", "user-own-gemini")
        ctx = UserContext(user_id="uv2", billing_mode="byok_api_key",
                          byok_provider="anthropic", byok_api_key="sk-x", byok_model="m")
        llm.analyze_video("https://youtu.be/x", "test", ctx)
        assert captured["key"] == "user-own-gemini"
        keyvault.clear_api_key("uv2", "gemini")


class TestGuidance:
    """開課前預檢引導 + LLM 智慧客服 fallback。"""

    def test_missing_youtube_key_guides_setup(self, fresh_repo, monkeypatch):
        from fastapi import BackgroundTasks

        import main
        from config import get_settings
        from delivery import line_client

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        get_settings.cache_clear()

        main._handle_text("ug", "tok", "開課 AI工作流 5", BackgroundTasks())
        msgs = line_client.drain_dev_outbox()
        # 引導含一鍵啟用連結與回貼步驟
        assert any("console.cloud.google.com/apis/library/youtube.googleapis.com" in m["text"]
                   for m in msgs)
        assert any("設定」→ 選 3" in m["text"] for m in msgs)
        get_settings.cache_clear()

    def test_missing_platform_llm_key_guides_byok(self, fresh_repo, monkeypatch):
        from fastapi import BackgroundTasks

        import main
        from config import get_settings
        from delivery import line_client

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()

        main._handle_text("ug2", "tok", "開課 AI工作流 5", BackgroundTasks())
        msgs = line_client.drain_dev_outbox()
        assert any("自備 API key" in m["text"] for m in msgs)
        get_settings.cache_clear()

    def test_smart_fallback_uses_llm_with_state(self, fresh_repo, monkeypatch):
        """未知文字 → LLM 客服回覆（帶使用者狀態）；LLM 掛掉 → 靜態選單。"""
        import llm
        import main

        captured = {}

        def fake_complete(purpose, messages, user_ctx, **kw):
            captured["purpose"] = purpose
            captured["system"] = kw.get("system", "")
            return "要開課的話，輸入：開課 AI工作流 5"

        monkeypatch.setattr(llm, "complete", fake_complete)
        answer = main._smart_fallback("uf", "為什麼失敗")
        assert "開課" in answer
        assert captured["purpose"] == "intent"
        assert "YouTube API key" in captured["system"]     # 狀態摘要有進 system prompt

        def broken(*a, **kw):
            raise RuntimeError("no llm")
        monkeypatch.setattr(llm, "complete", broken)
        assert main._smart_fallback("uf", "隨便") == main._STATIC_MENU

    def test_fail_reason_recorded_and_pushed(self, fresh_repo):
        """生成失敗：course 記 fail_reason、通知帶原因（供客服引用）。"""
        import worker
        from delivery import line_client

        # 平台無 YouTube key 且使用者未自備 → search 前就會炸
        course_id = worker.generate_course("ur", "AI", 3, charged_points=0)
        course = fresh_repo.get_course(course_id)
        assert course["status"] == "failed"
        assert course["fail_reason"]
        msgs = line_client.drain_dev_outbox()
        assert any("原因：" in m["text"] for m in msgs)

    def test_model_step_rejects_non_model_text(self, fresh_repo, monkeypatch):
        """「訂閱」等中文輸入不再被誤當模型名稱送測。"""
        from config import get_settings
        from delivery import settings_flow
        from llm.providers import claude_cli
        from llm.providers.claude_cli import ClaudeCLIProvider

        monkeypatch.setenv("ENABLE_OAUTH", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(ClaudeCLIProvider, "is_available", staticmethod(lambda: True))
        monkeypatch.setattr(claude_cli, "discover_models",
                            lambda force=False: {"sonnet": "claude-sonnet-4-6"})

        settings_flow.start("um")
        settings_flow.handle("um", "2")
        settings_flow.handle("um", "1")
        settings_flow.handle("um", "訂閱")            # 進入選模型步驟
        msgs = settings_flow.handle("um", "訂閱")     # 再打一次「訂閱」
        assert "看起來不是模型名稱" in msgs[0]         # 不再噴「測試失敗」
        settings_flow.handle("um", "取消")
        get_settings.cache_clear()

    def test_waitlist_when_youtube_quota_exhausted(self, fresh_repo):
        """平台 YouTube quota 用盡：進 waitlist、不退點、推送等候通知。"""
        import worker
        from delivery import line_client

        fresh_repo.incr_daily_counter(f"yt_quota_{worker._today()}", 10_000)
        points.topup("uw", 100)
        points.charge("uw", 20, job_id="j")
        course_id = worker.generate_course("uw", "AI", 3, charged_points=20)
        assert fresh_repo.get_course(course_id)["status"] == "waitlisted"
        assert points.balance("uw") == 80          # 不退點（隔日補跑）
        msgs = line_client.drain_dev_outbox()
        assert any("排入等候" in m["text"] for m in msgs)

    def test_scan_jobs_retries_stuck_and_waitlisted(self, fresh_repo):
        """掃描器：generating >10 分（卡住）與 waitlisted 都會被重排。"""
        import time

        from fastapi import BackgroundTasks

        import main

        stuck = fresh_repo.create_course("u1", "卡住的課", 3)
        fresh_repo.update_course(stuck, {"created_at": time.time() - 3600, "charged_points": 20})
        fresh_wait = fresh_repo.create_course("u2", "等候的課", 3)
        fresh_repo.update_course(fresh_wait, {"status": "waitlisted"})
        recent = fresh_repo.create_course("u3", "剛開始的課", 3)  # 不該被動到

        result = main.scan_jobs(BackgroundTasks())
        assert set(result["retried"]) == {stuck, fresh_wait}
        assert recent not in result["retried"]


# ---------- settings_flow（設定狀態機） ----------

class TestSettingsFlow:
    def test_full_byok_flow(self, fresh_repo, monkeypatch):
        import keyvault
        from delivery import settings_flow

        monkeypatch.setattr(settings_flow.model_catalog, "list_models",
                            lambda p, k, limit=None: ["claude-sonnet-4-6", "claude-haiku-4-5"])
        monkeypatch.setattr(settings_flow.model_catalog, "test_connection",
                            lambda p, k, m: {"ok": True, "input_tokens": 5,
                                             "output_tokens": 2, "cost_usd": 0.00002})

        settings_flow.start("u1")
        settings_flow.handle("u1", "2")                    # 自備 API key
        settings_flow.handle("u1", "1")                    # Anthropic
        msgs = settings_flow.handle("u1", "sk-ant-secret-key-12345")
        assert "claude-sonnet-4-6" in msgs[0]
        assert "sk-ant-secret-key-12345" not in msgs[0]    # 回覆訊息不得含明文 key
        msgs = settings_flow.handle("u1", "1")             # 選第一個模型
        assert "測試連線成功" in msgs[0]

        user = fresh_repo.get_user("u1")
        assert user["billing_mode"] == "byok_api_key"
        assert user["byok"] == {"provider": "anthropic", "selected_model": "claude-sonnet-4-6"}
        assert keyvault.get_api_key("u1", "llm") == "sk-ant-secret-key-12345"  # 只在記憶體
        assert not settings_flow.in_session("u1")
        keyvault.clear_api_key("u1", "llm")

    def test_byok_family_then_version_selection(self, fresh_repo, monkeypatch):
        """多版本系列：選系列後出現版本清單，再選版本。"""
        from delivery import settings_flow

        monkeypatch.setattr(settings_flow.model_catalog, "list_models",
                            lambda p, k, limit=None: [
                                "claude-sonnet-4-6-20260101",
                                "claude-sonnet-4-6-20250901",
                                "claude-haiku-4-5-20251001",
                            ])
        monkeypatch.setattr(settings_flow.model_catalog, "test_connection",
                            lambda p, k, m: {"ok": True, "input_tokens": 5,
                                             "output_tokens": 2, "cost_usd": 0.00002})

        settings_flow.start("u8")
        settings_flow.handle("u8", "2")
        settings_flow.handle("u8", "1")                      # Anthropic
        msgs = settings_flow.handle("u8", "sk-test-key-888")
        assert "claude-sonnet-4-6（2 個版本）" in msgs[0]      # 系列層
        msgs = settings_flow.handle("u8", "1")               # 選 sonnet 系列
        assert "claude-sonnet-4-6-20260101（最新）" in msgs[0]  # 版本層
        msgs = settings_flow.handle("u8", "2")               # 選舊版本
        assert "測試連線成功" in msgs[0]
        assert (fresh_repo.get_user("u8")["byok"]["selected_model"]
                == "claude-sonnet-4-6-20250901")

    def test_group_by_family(self):
        from llm.model_catalog import group_by_family

        groups = group_by_family([
            "claude-sonnet-4-6-20260101", "claude-sonnet-4-6-20250901",
            "gpt-4o-2024-08-06", "gpt-4o", "gemini-2.0-flash-001",
        ])
        assert groups["claude-sonnet-4-6"] == [
            "claude-sonnet-4-6-20260101", "claude-sonnet-4-6-20250901"]
        assert groups["gpt-4o"] == ["gpt-4o-2024-08-06", "gpt-4o"]
        assert groups["gemini-2.0-flash"] == ["gemini-2.0-flash-001"]

    def test_switch_to_points(self, fresh_repo):
        from delivery import settings_flow

        settings_flow.start("u2")
        msgs = settings_flow.handle("u2", "1")
        assert "點數制" in msgs[0]
        assert fresh_repo.get_user("u2")["billing_mode"] == "points"

    def test_cancel(self):
        from delivery import settings_flow

        settings_flow.start("u3")
        settings_flow.handle("u3", "取消")
        assert not settings_flow.in_session("u3")

    def test_clear_keys(self, fresh_repo):
        """「清除我的 key」：三種 key 全清、切回點數制。"""
        import keyvault
        from delivery import settings_flow

        for kind in ("llm", "youtube", "oauth"):
            keyvault.set_api_key("u9", kind, f"secret-{kind}")
        fresh_repo.upsert_user("u9", {"billing_mode": "byok_api_key"})

        settings_flow.start("u9")
        msgs = settings_flow.handle("u9", "4")
        assert "已清除" in msgs[0]
        for kind in ("llm", "youtube", "oauth"):
            assert keyvault.get_api_key("u9", kind) is None
        assert fresh_repo.get_user("u9")["billing_mode"] == "points"

    def test_oauth_hint_hidden_when_flag_off(self, monkeypatch):
        """flag 關閉時，貼 key 步驟不得出現訂閱制入口。"""
        from config import get_settings
        from delivery import settings_flow

        monkeypatch.delenv("ENABLE_OAUTH", raising=False)
        get_settings.cache_clear()
        settings_flow.start("u4")
        settings_flow.handle("u4", "2")
        msgs = settings_flow.handle("u4", "1")     # 選品牌 → 進貼 key 步驟
        assert "訂閱" not in msgs[0]
        settings_flow.handle("u4", "取消")
        get_settings.cache_clear()

    def test_oauth_hint_shown_in_dev_mode(self, monkeypatch):
        """開發階段（flag 開 + dev 模式）：貼 key 步驟出現訂閱制入口；
        CLI 不在本機時退回貼 token 路徑。"""
        import keyvault
        from config import get_settings
        from delivery import settings_flow
        from llm.providers.claude_cli import ClaudeCLIProvider

        monkeypatch.setenv("ENABLE_OAUTH", "true")
        monkeypatch.setattr(ClaudeCLIProvider, "is_available", staticmethod(lambda: False))
        get_settings.cache_clear()
        settings_flow.start("u5")
        settings_flow.handle("u5", "2")
        msgs = settings_flow.handle("u5", "1")     # Anthropic → 貼 key 步驟
        assert "訂閱" in msgs[0]
        msgs = settings_flow.handle("u5", "訂閱")   # 改走訂閱制
        assert "token" in msgs[0]
        msgs = settings_flow.handle("u5", "fake-oauth-token")
        assert "訂閱制 token 已載入" in msgs[0]
        assert keyvault.get_api_key("u5", "oauth") == "fake-oauth-token"
        keyvault.clear_api_key("u5", "oauth")
        get_settings.cache_clear()

    def test_oauth_cli_auto_detect_flow(self, fresh_repo, monkeypatch):
        """訂閱制自動偵測本機 CLI：不用貼任何憑證，選模型 → 測試 → 啟用。"""
        from config import get_settings
        from delivery import settings_flow
        from llm.providers.claude_cli import ClaudeCLIProvider
        from llm.providers.base import LLMResponse
        from pipeline.models import LLMUsage

        monkeypatch.setenv("ENABLE_OAUTH", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(ClaudeCLIProvider, "is_available", staticmethod(lambda: True))
        # 別名探測 mock：不真的開子進程
        from llm.providers import claude_cli
        monkeypatch.setattr(claude_cli, "discover_models",
                            lambda force=False: {"sonnet": "claude-sonnet-4-6-20260101"})
        monkeypatch.setattr(ClaudeCLIProvider, "complete",
                            lambda self, msgs, model, **kw:
                            LLMResponse("OK", LLMUsage(input_tokens=5, output_tokens=2)))

        settings_flow.start("u6")
        settings_flow.handle("u6", "2")            # 自備 API key
        settings_flow.handle("u6", "1")            # Anthropic
        msgs = settings_flow.handle("u6", "訂閱")
        assert "偵測到本機的 Claude Code CLI" in msgs[0]
        assert "sonnet → claude-sonnet-4-6-20260101（實測可用）" in msgs[0]
        msgs = settings_flow.handle("u6", "1")     # 選 sonnet（探測過 → 免重測）
        assert "訂閱制連線成功" in msgs[0]
        assert "探測時已實測" in msgs[0]

        user = fresh_repo.get_user("u6")
        assert user["billing_mode"] == "oauth"
        assert user["oauth_source"] == "cli"
        assert user["oauth_model"] == "sonnet"
        import keyvault
        assert keyvault.get_api_key("u6", "oauth") is None   # 全程沒有存任何憑證
        get_settings.cache_clear()

    def test_oauth_cli_free_typed_model(self, fresh_repo, monkeypatch):
        """訂閱制不限別名清單：可直接輸入任何完整模型名稱。"""
        from config import get_settings
        from delivery import settings_flow
        from llm.providers.claude_cli import ClaudeCLIProvider
        from llm.providers.base import LLMResponse
        from pipeline.models import LLMUsage

        monkeypatch.setenv("ENABLE_OAUTH", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(ClaudeCLIProvider, "is_available", staticmethod(lambda: True))
        from llm.providers import claude_cli
        monkeypatch.setattr(claude_cli, "discover_models",
                            lambda force=False: {"sonnet": "claude-sonnet-4-6-20260101"})
        monkeypatch.setattr(ClaudeCLIProvider, "complete",
                            lambda self, msgs, model, **kw:
                            LLMResponse("OK", LLMUsage(input_tokens=5, output_tokens=2)))

        settings_flow.start("u7")
        settings_flow.handle("u7", "2")
        settings_flow.handle("u7", "1")
        settings_flow.handle("u7", "訂閱")
        msgs = settings_flow.handle("u7", "claude-opus-4-8")   # 清單外，直接打名稱 → 需實測
        assert "訂閱制連線成功" in msgs[0]
        assert fresh_repo.get_user("u7")["oauth_model"] == "claude-opus-4-8"
        get_settings.cache_clear()


# ---------- 學習契約與 grill 式開課訪談 ----------

class TestLearningContract:
    def test_defaults_and_summary(self):
        from pipeline.models import LearningContract

        c = LearningContract(topic="技術分析基礎")
        assert c.start_difficulty == 1 and c.language == "zh_first"
        text = "\n".join(c.summary_lines())
        assert "技術分析基礎" in text and "4–45 分鐘" in text

    def test_low_trust_note_in_summary(self):
        from pipeline.models import LearningContract

        c = LearningContract(topic="技術分析", low_trust_popularity=True,
                             channel_blocklist=["帶單老師"])
        text = "\n".join(c.summary_lines())
        assert "流量≠品質" in text and "帶單老師" in text

    def test_default_contract_maps_level(self):
        from delivery.intake_flow import default_contract

        assert default_contract("Python", "beginner").start_difficulty == 1
        assert default_contract("Python", "some").start_difficulty == 2
        assert default_contract("Python", "advanced").start_difficulty == 3
        assert default_contract("Python").start_difficulty == 1


class TestGrillIntake:
    """grill 式開課：一次一題附建議 → 學習契約 → 確認 → 堂數 → 生成（契約入庫）。"""

    def _msg(self, user_id, text):
        from fastapi import BackgroundTasks

        import main
        from delivery import line_client

        main._handle_text(user_id, "tok", text, BackgroundTasks())
        return [m["text"] for m in line_client.drain_dev_outbox()]

    def _script_llm(self, monkeypatch, responses: list[str]):
        """把 llm.complete 換成照劇本輪流回覆。"""
        import llm

        seq = list(responses)
        monkeypatch.setattr(llm, "complete",
                            lambda *a, **k: seq.pop(0) if seq else "{}")

    _CONTRACT_JSON = (
        '{"type": "contract", "contract": {"topic": "K線與量價結構基礎", '
        '"include": ["K線", "量價"], "exclude": ["選擇權"], "language": "zh_first", '
        '"chinese_script": "no_simplified", "start_difficulty": 2, '
        '"min_duration_min": 8, "max_duration_min": 30, "recency_months": 24, '
        '"channel_blocklist": [], "channel_prioritize": [], '
        '"teaching_style_pref": "實作看盤示範優先", "low_trust_popularity": true}}')

    def test_grill_to_contract_to_generation(self, fresh_repo, monkeypatch):
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        points.topup("ug", 100)
        self._script_llm(monkeypatch, [
            '{"type": "ask", "question": "你想學的是哪一塊？", '
            '"recommendation": "建議從 K 線與量價開始"}',
            self._CONTRACT_JSON,
        ])

        self._msg("ug", "開課")
        msgs = self._msg("ug", "技術分析")            # 第一輪 → 追問
        assert any("❓" in m and "💡 建議" in m for m in msgs)
        msgs = self._msg("ug", "K線和量價")            # 第二輪 → 收斂成契約
        assert any("學習契約確認" in m and "K線與量價結構基礎" in m for m in msgs)
        assert any("流量≠品質" in m for m in msgs)     # 財經領域降權有進契約

        msgs = self._msg("ug", "確認")                # 契約 → 堂數
        assert any("幾堂課" in m for m in msgs)
        msgs = self._msg("ug", "5")
        assert any("5 堂" in m for m in msgs)
        msgs = self._msg("ug", "確認")                # 報價 → 生成
        assert any("開始為你策展" in m for m in msgs)

        cid = (fresh_repo.get_user("ug") or {})["last_course_id"]
        course = fresh_repo.get_course(cid)
        assert course["topic"] == "K線與量價結構基礎"   # 用收斂後的主題
        assert course["contract"]["low_trust_popularity"] is True
        assert course["contract"]["exclude"] == ["選擇權"]
        get_settings.cache_clear()

    def test_contract_revision(self, fresh_repo, monkeypatch):
        """契約確認時用自然語言修改 → 重新呈現修訂版。"""
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()
        revised = self._CONTRACT_JSON.replace('"max_duration_min": 30',
                                              '"max_duration_min": 20')
        # revise 只輸出契約本體（不含 type 包裝）
        import json as _json
        revised_body = _json.dumps(_json.loads(revised)["contract"], ensure_ascii=False)
        self._script_llm(monkeypatch, [self._CONTRACT_JSON, revised_body])

        self._msg("ur", "開課")
        msgs = self._msg("ur", "技術分析")            # 主題夠明確 → 直接出契約
        assert any("學習契約確認" in m for m in msgs)
        msgs = self._msg("ur", "片長上限改 20 分鐘")
        assert any("8–20 分鐘" in m for m in msgs)
        get_settings.cache_clear()

    def test_llm_down_falls_back_to_fixed_flow(self, fresh_repo, monkeypatch):
        """LLM 不可用（conftest 預設擋掉）→ 退回固定三題，功能不斷。"""
        from config import get_settings

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform")
        monkeypatch.setenv("YOUTUBE_API_KEY", "yt-platform")
        get_settings.cache_clear()

        self._msg("uf", "開課")
        msgs = self._msg("uf", "技術分析")
        assert any("程度" in m for m in msgs)          # 退回舊版程度診斷
        get_settings.cache_clear()


class TestContractFilters:
    """粗篩服從契約：片長/時效/頻道黑白名單/污染領域流量降權。"""

    def _contract(self, **kw):
        from pipeline.models import LearningContract

        base = dict(topic="技術分析")
        base.update(kw)
        return LearningContract(**base)

    def test_duration_from_contract(self):
        c8 = make_candidate(video_id="a", duration_sec=8 * 60)
        c40 = make_candidate(video_id="b", duration_sec=40 * 60)
        result = filters.apply([c8, c40], now=NOW,
                               contract=self._contract(max_duration_min=30))
        assert [c.video_id for c in result] == ["a"]

    def test_channel_blocklist(self):
        bad = make_candidate(video_id="a", channel_title="帶單老師王")
        ok = make_candidate(video_id="b", channel_title="正經教學台")
        result = filters.apply([bad, ok], now=NOW,
                               contract=self._contract(channel_blocklist=["帶單"]))
        assert [c.video_id for c in result] == ["b"]

    def test_recency_hard_filter_with_relax(self):
        old = make_candidate(video_id="old", published_at="2023-01-01T00:00:00Z")
        new = make_candidate(video_id="new", published_at="2026-06-01T00:00:00Z")
        # 新片不足 MIN_SHORTLIST_FOR_RECENCY → 放寬，舊片仍保留
        result = filters.apply([old, new], now=NOW,
                               contract=self._contract(recency_months=12))
        assert {c.video_id for c in result} == {"old", "new"}
        # 新片夠多 → 舊片被時效硬篩掉
        many_new = [make_candidate(video_id=f"n{i}",
                                   published_at="2026-06-01T00:00:00Z")
                    for i in range(8)]
        result = filters.apply([old] + many_new, now=NOW,
                               contract=self._contract(recency_months=12))
        assert "old" not in {c.video_id for c in result}

    def test_low_trust_popularity_downweights_views(self):
        """污染領域：高流量老片輸給流量普通的新片（正常權重下相反）。"""
        viral_old = make_candidate(video_id="viral", like_count=2000, view_count=50_000,
                                   subscriber_count=10_000,
                                   published_at="2023-06-01T00:00:00Z")
        modest_new = make_candidate(video_id="new", like_count=60, view_count=3_000,
                                    subscriber_count=50_000,
                                    published_at="2026-06-01T00:00:00Z")
        assert filters.score(viral_old, NOW) > filters.score(modest_new, NOW)
        assert (filters.score(modest_new, NOW, low_trust_popularity=True)
                > filters.score(viral_old, NOW, low_trust_popularity=True))

    def test_channel_prioritize_boost(self):
        pocket = make_candidate(video_id="p", channel_title="口袋名單頻道", like_count=200)
        other = make_candidate(video_id="o", channel_title="其他頻道", like_count=400)
        result = filters.apply([other, pocket], target=2, now=NOW,
                               contract=self._contract(channel_prioritize=["口袋名單"]))
        assert result[0].video_id == "p"

    def test_contract_language_overrides_prefs(self):
        zh = make_candidate(video_id="zh")
        en = make_candidate(video_id="en", title="Python tutorial", description="english")
        result = filters.apply([zh, en], target=5, now=NOW,
                               prefs={"language": "any"},
                               contract=self._contract(language="zh_only"))
        assert [c.video_id for c in result] == ["zh"]


class TestContractGate:
    """評估後守門：導流片、低吻合度、過時影片一律剔除。"""

    def test_passes_contract(self):
        import worker
        from pipeline.models import VideoEvaluation

        ok = VideoEvaluation(video_id="a", difficulty=2, quality_score=7,
                             contract_fit=8.0)
        promo = VideoEvaluation(video_id="b", difficulty=2, quality_score=7,
                                contract_fit=9.0, is_promotional=True)
        low_fit = VideoEvaluation(video_id="c", difficulty=2, quality_score=7,
                                  contract_fit=2.0)
        outdated = VideoEvaluation(video_id="d", difficulty=2, quality_score=7,
                                   is_outdated=True)
        legacy = VideoEvaluation(video_id="e", difficulty=2, quality_score=7)  # 無契約評估
        assert worker._passes_contract(ok)
        assert not worker._passes_contract(promo)
        assert not worker._passes_contract(low_fit)
        assert not worker._passes_contract(outdated)
        assert worker._passes_contract(legacy)


class TestDevExport:
    """/dev/export：從資料庫組完整課綱 Markdown 下載，不動學習進度。"""

    def test_export_ready_course(self, fresh_repo):
        from fastapi.testclient import TestClient

        import main

        cid = fresh_repo.create_course(main.DEV_USER, "技術分析基礎", 2)
        fresh_repo.update_course(cid, {"status": "ready", "honest_note": ""})
        fresh_repo.save_lessons(cid, [
            {"order": i, "video_id": f"v{i}", "video_url": f"https://youtu.be/v{i}",
             "title": f"第{i}課", "channel": "ch", "duration_sec": 600,
             "difficulty": 2, "summary": f"摘要{i}", "learning_goals": [f"目標{i}"],
             "quiz": [{"q": f"問題{i}", "a": f"答案{i}"}], "bridge_note": ""}
            for i in (1, 2)])
        fresh_repo.upsert_user(main.DEV_USER, {"active_course_id": cid})
        fresh_repo.set_progress(main.DEV_USER, cid,
                                {"current_lesson": 1, "completed": [], "active": True})

        r = TestClient(main.app).get("/dev/export")
        assert r.status_code == 200
        body = r.text
        assert "技術分析基礎" in body and "https://youtu.be/v2" in body
        assert "摘要1" in body and "目標2" in body
        assert "問題1" in body and "答案2" in body        # 檢核題含答案
        # 匯出不影響進度
        progress = fresh_repo.get_progress(main.DEV_USER, cid)
        assert progress["completed"] == []

    def test_export_without_course_404(self, fresh_repo):
        from fastapi.testclient import TestClient

        import main

        r = TestClient(main.app).get("/dev/export")
        assert r.status_code == 404
