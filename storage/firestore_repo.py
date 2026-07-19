"""資料存取層。GCP_PROJECT 有設定時走 Firestore，否則走 in-memory（本機開發/測試）。

集合結構見架構文件第七節：
users / points_ledger / usage_logs / courses(+lessons) / progress / config
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from config import (DEFAULT_LLM_SETTINGS, adapt_settings_to_available_keys,
                    get_settings)


class BaseRepo:
    # ---- config ----
    def get_llm_settings(self) -> dict: ...
    def set_llm_settings(self, patch: dict) -> dict: ...

    # ---- users ----
    def get_user(self, user_id: str) -> Optional[dict]: ...
    def upsert_user(self, user_id: str, data: dict) -> dict: ...

    # ---- points ledger ----
    def append_ledger(self, user_id: str, type_: str, points: int,
                      related_job_id: Optional[str], note: str = "") -> int: ...
    def points_balance(self, user_id: str) -> int: ...

    # ---- usage logs ----
    def append_usage_call(self, user_id: str, billing_mode: str, job_id: str, call: dict) -> None: ...
    def get_usage_log(self, job_id: str) -> Optional[dict]: ...

    # ---- courses ----
    def create_course(self, owner: str, topic: str, lesson_count: int) -> str: ...
    def update_course(self, course_id: str, patch: dict) -> None: ...
    def get_course(self, course_id: str) -> Optional[dict]: ...
    def list_courses_by_status(self, status: str) -> list[dict]: ...
    def save_lessons(self, course_id: str, lessons: list[dict]) -> None: ...
    def get_lessons(self, course_id: str) -> list[dict]: ...
    def update_lesson(self, course_id: str, order: int, patch: dict) -> None: ...

    # ---- lesson feedback（讚/爛 → 策展品質資料） ----
    def add_feedback(self, user_id: str, course_id: str, lesson_order: int,
                     rating: str) -> None: ...

    # ---- daily counters（免費配額、YouTube quota 等，key 內含日期自然歸零） ----
    def get_daily_counter(self, key: str) -> int: ...
    def incr_daily_counter(self, key: str, amount: int) -> int: ...

    # ---- progress ----
    def get_progress(self, user_id: str, course_id: str) -> Optional[dict]: ...
    def set_progress(self, user_id: str, course_id: str, data: dict) -> None: ...


class InMemoryRepo(BaseRepo):
    """本機開發與單元測試用，介面與 Firestore 版完全一致。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._config: dict = {"llm_settings": dict(DEFAULT_LLM_SETTINGS)}
        self._users: dict[str, dict] = {}
        self._ledger: list[dict] = []
        self._usage: dict[str, dict] = {}
        self._courses: dict[str, dict] = {}
        self._lessons: dict[str, list[dict]] = {}
        self._progress: dict[str, dict] = {}
        self._counters: dict[str, int] = {}
        self._feedback: list[dict] = []

    def get_llm_settings(self) -> dict:
        return adapt_settings_to_available_keys(self._config["llm_settings"])

    def set_llm_settings(self, patch: dict) -> dict:
        with self._lock:
            self._config["llm_settings"].update(patch)
            return dict(self._config["llm_settings"])

    def get_user(self, user_id: str):
        return self._users.get(user_id)

    def upsert_user(self, user_id: str, data: dict) -> dict:
        with self._lock:
            user = self._users.setdefault(user_id, {"created_at": time.time(), "language": "zh-TW",
                                                    "billing_mode": "points", "points_balance": 0})
            user.update(data)
            return dict(user)

    def append_ledger(self, user_id, type_, points, related_job_id, note="") -> int:
        with self._lock:
            self._ledger.append({
                "entry_id": uuid.uuid4().hex, "user_id": user_id, "type": type_,
                "points": points, "related_job_id": related_job_id,
                "note": note, "created_at": time.time(),
            })
            bal = sum(e["points"] for e in self._ledger if e["user_id"] == user_id)
            self._users.setdefault(user_id, {})["points_balance"] = bal
            return bal

    def points_balance(self, user_id: str) -> int:
        return sum(e["points"] for e in self._ledger if e["user_id"] == user_id)

    def append_usage_call(self, user_id, billing_mode, job_id, call) -> None:
        with self._lock:
            log = self._usage.setdefault(job_id, {
                "user_id": user_id, "billing_mode": billing_mode,
                "calls": [], "total_cost_usd": 0.0, "created_at": time.time(),
            })
            log["calls"].append(call)
            log["total_cost_usd"] = round(sum(c["cost_usd"] for c in log["calls"]), 8)

    def get_usage_log(self, job_id: str):
        return self._usage.get(job_id)

    def create_course(self, owner, topic, lesson_count) -> str:
        course_id = uuid.uuid4().hex[:12]
        self._courses[course_id] = {
            "course_id": course_id, "owner": owner, "topic": topic,
            "lesson_count": lesson_count, "status": "generating", "created_at": time.time(),
        }
        return course_id

    def update_course(self, course_id, patch) -> None:
        self._courses[course_id].update(patch)

    def get_course(self, course_id):
        return self._courses.get(course_id)

    def list_courses_by_status(self, status: str) -> list[dict]:
        return [dict(c) for c in self._courses.values() if c.get("status") == status]

    def get_daily_counter(self, key: str) -> int:
        return self._counters.get(key, 0)

    def incr_daily_counter(self, key: str, amount: int) -> int:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount
            return self._counters[key]

    def save_lessons(self, course_id, lessons) -> None:
        self._lessons[course_id] = lessons

    def get_lessons(self, course_id) -> list[dict]:
        return self._lessons.get(course_id, [])

    def update_lesson(self, course_id, order, patch) -> None:
        with self._lock:
            for lesson in self._lessons.get(course_id, []):
                if lesson["order"] == order:
                    lesson.update(patch)

    def add_feedback(self, user_id, course_id, lesson_order, rating) -> None:
        with self._lock:
            self._feedback.append({
                "user_id": user_id, "course_id": course_id,
                "lesson_order": lesson_order, "rating": rating, "created_at": time.time(),
            })

    def get_progress(self, user_id, course_id):
        return self._progress.get(f"{user_id}_{course_id}")

    def set_progress(self, user_id, course_id, data) -> None:
        self._progress[f"{user_id}_{course_id}"] = data


class FirestoreRepo(BaseRepo):
    """正式環境。集合命名對齊架構文件第七節。"""

    _LLM_SETTINGS_CACHE_TTL = 60  # 秒；後台改設定最慢一分鐘生效

    def __init__(self, project: str):
        from google.cloud import firestore  # 延遲 import：本機無此套件也能跑 in-memory

        self._db = firestore.Client(project=project)
        self._settings_cache: tuple[float, dict] | None = None

    def get_llm_settings(self) -> dict:
        now = time.time()
        if self._settings_cache and now - self._settings_cache[0] < self._LLM_SETTINGS_CACHE_TTL:
            return dict(self._settings_cache[1])
        doc = self._db.collection("config").document("llm_settings").get()
        merged = adapt_settings_to_available_keys(
            {**DEFAULT_LLM_SETTINGS, **(doc.to_dict() or {})})
        self._settings_cache = (now, merged)
        return dict(merged)

    def set_llm_settings(self, patch: dict) -> dict:
        self._db.collection("config").document("llm_settings").set(patch, merge=True)
        self._settings_cache = None
        return self.get_llm_settings()

    def get_user(self, user_id):
        doc = self._db.collection("users").document(user_id).get()
        return doc.to_dict() if doc.exists else None

    def upsert_user(self, user_id, data) -> dict:
        ref = self._db.collection("users").document(user_id)
        ref.set(data, merge=True)
        return ref.get().to_dict()

    def append_ledger(self, user_id, type_, points, related_job_id, note="") -> int:
        from google.cloud import firestore

        self._db.collection("points_ledger").add({
            "user_id": user_id, "type": type_, "points": points,
            "related_job_id": related_job_id, "note": note,
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        user_ref = self._db.collection("users").document(user_id)
        user_ref.set({"points_balance": firestore.Increment(points)}, merge=True)
        return self.points_balance(user_id)

    def points_balance(self, user_id: str) -> int:
        user = self.get_user(user_id)
        return int(user.get("points_balance", 0)) if user else 0

    def append_usage_call(self, user_id, billing_mode, job_id, call) -> None:
        from google.cloud import firestore

        ref = self._db.collection("usage_logs").document(job_id)
        ref.set({
            "user_id": user_id, "billing_mode": billing_mode,
            "calls": firestore.ArrayUnion([call]),
            "total_cost_usd": firestore.Increment(call["cost_usd"]),
            "created_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    def get_usage_log(self, job_id):
        doc = self._db.collection("usage_logs").document(job_id).get()
        return doc.to_dict() if doc.exists else None

    def create_course(self, owner, topic, lesson_count) -> str:
        from google.cloud import firestore

        ref = self._db.collection("courses").document()
        ref.set({
            "owner": owner, "topic": topic, "lesson_count": lesson_count,
            "status": "generating", "created_at": firestore.SERVER_TIMESTAMP,
        })
        return ref.id

    def update_course(self, course_id, patch) -> None:
        self._db.collection("courses").document(course_id).set(patch, merge=True)

    def get_course(self, course_id):
        doc = self._db.collection("courses").document(course_id).get()
        return ({"course_id": course_id, **doc.to_dict()}) if doc.exists else None

    def list_courses_by_status(self, status: str) -> list[dict]:
        docs = self._db.collection("courses").where("status", "==", status).stream()
        return [{"course_id": d.id, **d.to_dict()} for d in docs]

    def get_daily_counter(self, key: str) -> int:
        doc = self._db.collection("counters").document(key).get()
        return int((doc.to_dict() or {}).get("value", 0)) if doc.exists else 0

    def incr_daily_counter(self, key: str, amount: int) -> int:
        from google.cloud import firestore

        ref = self._db.collection("counters").document(key)
        ref.set({"value": firestore.Increment(amount)}, merge=True)
        return self.get_daily_counter(key)

    def save_lessons(self, course_id, lessons) -> None:
        batch = self._db.batch()
        col = self._db.collection("courses").document(course_id).collection("lessons")
        for lesson in lessons:
            batch.set(col.document(str(lesson["order"])), lesson)
        batch.commit()

    def get_lessons(self, course_id) -> list[dict]:
        col = self._db.collection("courses").document(course_id).collection("lessons")
        return sorted((d.to_dict() for d in col.stream()), key=lambda x: x["order"])

    def update_lesson(self, course_id, order, patch) -> None:
        (self._db.collection("courses").document(course_id)
         .collection("lessons").document(str(order)).set(patch, merge=True))

    def add_feedback(self, user_id, course_id, lesson_order, rating) -> None:
        from google.cloud import firestore

        self._db.collection("feedback").add({
            "user_id": user_id, "course_id": course_id,
            "lesson_order": lesson_order, "rating": rating,
            "created_at": firestore.SERVER_TIMESTAMP,
        })

    def get_progress(self, user_id, course_id):
        doc = self._db.collection("progress").document(f"{user_id}_{course_id}").get()
        return doc.to_dict() if doc.exists else None

    def set_progress(self, user_id, course_id, data) -> None:
        self._db.collection("progress").document(f"{user_id}_{course_id}").set(data, merge=True)


_repo: BaseRepo | None = None


def get_repo() -> BaseRepo:
    global _repo
    if _repo is None:
        project = get_settings().gcp_project
        _repo = FirestoreRepo(project) if project else InMemoryRepo()
    return _repo
