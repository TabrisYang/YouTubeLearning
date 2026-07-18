"""課綱輸出版型：Markdown（D3 驗收 / 後台檢視）與 LINE Flex Message。"""
from __future__ import annotations

from pipeline.models import CoursePlan, Lesson


def _fmt_duration(sec: int) -> str:
    return f"{sec // 60} 分鐘"


def course_markdown(plan: CoursePlan) -> str:
    """D3 驗收用：完整課綱 Markdown，自己照著上第一堂。"""
    lines = [f"# 課程：{plan.topic}", ""]
    if plan.honest_note:
        lines += [f"> ⚠️ {plan.honest_note}", ""]
    lines.append(f"共 {len(plan.lessons)} 堂（要求 {plan.requested_lessons} 堂）")
    for lesson in plan.lessons:
        lines += [
            "",
            f"## 第 {lesson.order} 堂：{lesson.title}",
            f"- 📺 {lesson.video_url}",
            f"- 頻道：{lesson.channel}｜時長：{_fmt_duration(lesson.duration_sec)}｜難度：{'★' * lesson.difficulty}",
        ]
        if lesson.bridge_note:
            lines.append(f"- 🔗 銜接：{lesson.bridge_note}")
        lines += ["", f"**摘要**：{lesson.summary}", "", "**學習目標**"]
        lines += [f"- {g}" for g in lesson.learning_goals]
        lines += ["", "**檢核題**"]
        lines += [f"{i}. {q.q}\n   > 答：{q.a}" for i, q in enumerate(lesson.quiz, 1)]
    return "\n".join(lines)


def lesson_flex(lesson: Lesson, total: int) -> dict:
    """「今日課程」單堂 Flex Message。"""
    return {
        "type": "flex",
        "altText": f"第 {lesson.order} 堂：{lesson.title}",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": f"第 {lesson.order}/{total} 堂", "size": "sm", "color": "#888888"},
                    {"type": "text", "text": lesson.title, "weight": "bold", "size": "lg", "wrap": True},
                    {"type": "text", "text": f"{lesson.channel}｜{_fmt_duration(lesson.duration_sec)}｜難度 {'★' * lesson.difficulty}",
                     "size": "xs", "color": "#888888", "wrap": True},
                    {"type": "separator"},
                    {"type": "text", "text": lesson.summary, "size": "sm", "wrap": True},
                    {"type": "text", "text": "🎯 " + "／".join(lesson.learning_goals), "size": "xs",
                     "color": "#555555", "wrap": True},
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {"type": "button", "style": "primary",
                     "action": {"type": "uri", "label": "觀看影片", "uri": lesson.video_url}},
                    {"type": "button", "style": "secondary",
                     "action": {"type": "message", "label": "完成這堂課", "text": "完成"}},
                    {"type": "button", "style": "secondary",
                     "action": {"type": "message", "label": "所有課程", "text": "課程列表"}},
                ],
            },
        },
    }


def quiz_text(lesson: Lesson) -> str:
    lines = [f"✅ 第 {lesson.order} 堂完成！來檢核一下："]
    lines += [f"{i}. {q.q}" for i, q in enumerate(lesson.quiz, 1)]
    lines.append("\n（想對答案就回「答案」）")
    return "\n".join(lines)


def quiz_answers_text(lesson: Lesson) -> str:
    return "\n".join(f"{i}. {q.a}" for i, q in enumerate(lesson.quiz, 1))
