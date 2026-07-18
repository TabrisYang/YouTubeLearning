"""本機驗收 CLI（不經 LINE）。

D1 驗收：python cli.py candidates "AI工作流"        → 印出 25 支候選清單
D3 驗收：python cli.py course "AI工作流" 10          → 完整課綱 Markdown（含逐筆成本）
其他：  python cli.py settings                       → 目前 llm_settings
"""
from __future__ import annotations

import sys

from billing import meter
from delivery import formatter
from pipeline import curator, filters, searcher, transcript
from pipeline.models import UserContext
from storage.firestore_repo import get_repo

DEV_USER = UserContext(user_id="cli-dev", billing_mode="points")


def cmd_candidates(topic: str) -> None:
    keywords = searcher.expand_topic(topic, DEV_USER, job_id="cli-candidates")
    print(f"🔑 關鍵字：{keywords}\n")
    candidates, quota = searcher.search_videos(keywords)
    shortlist = filters.apply(candidates)
    print(f"搜到 {len(candidates)} 支 → 粗篩後 {len(shortlist)} 支（quota 用量 {quota}）\n")
    for i, c in enumerate(shortlist, 1):
        zh = "中" if filters.is_chinese(c) else "英"
        print(f"{i:2d}. [{zh}] {c.duration_sec // 60:3d}分 | 觀看 {c.view_count:>9,} | "
              f"讚 {c.like_count:>7,} | {c.title[:45]}")
        print(f"     {c.url}")
    _print_cost("cli-candidates")


def cmd_course(topic: str, n: int) -> None:
    job_id = "cli-course"
    keywords = searcher.expand_topic(topic, DEV_USER, job_id=job_id)
    print(f"🔑 關鍵字：{keywords}", file=sys.stderr)
    candidates, _ = searcher.search_videos(keywords)
    shortlist = filters.apply(candidates)
    print(f"候選 {len(candidates)} → 粗篩 {len(shortlist)}，開始評估…", file=sys.stderr)

    evaluations = []
    for c in shortlist:
        tr = transcript.fetch(c)
        ev = curator.evaluate(c, tr, DEV_USER, job_id=job_id)
        if ev and not ev.is_outdated:
            evaluations.append(ev)
            print(f"  ✓ 難度{ev.difficulty} 品質{ev.quality_score:.1f} "
                  f"[{ev.analysis_basis}] {c.title[:40]}", file=sys.stderr)
    print(f"評估存活 {len(evaluations)} 支，開始編排…", file=sys.stderr)

    plan = curator.curate(topic, n, shortlist, evaluations, DEV_USER, job_id=job_id)
    print(formatter.course_markdown(plan))
    _print_cost(job_id)


def _print_cost(job_id: str) -> None:
    log = get_repo().get_usage_log(job_id)
    if not log:
        return
    settings = get_repo().get_llm_settings()
    total = log["total_cost_usd"]
    pts = meter.usd_to_points(total, settings)
    print(f"\n💰 本次成本：${total:.4f} USD ≈ {pts} 點（markup {settings['markup']}×）"
          f"｜LLM 呼叫 {len(log['calls'])} 次", file=sys.stderr)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "candidates" and len(args) >= 2:
        cmd_candidates(args[1])
    elif cmd == "course" and len(args) >= 3:
        cmd_course(args[1], int(args[2]))
    elif cmd == "settings":
        print(get_repo().get_llm_settings())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
