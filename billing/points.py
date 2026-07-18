"""點數帳本：儲值、扣點、餘額、退點。真相在 ledger，users.points_balance 是冗餘快取。"""
from __future__ import annotations


class InsufficientPoints(Exception):
    def __init__(self, balance: int, needed: int):
        self.balance, self.needed = balance, needed
        super().__init__(f"點數不足：餘額 {balance}，需要 {needed}")


def balance(user_id: str) -> int:
    from storage.firestore_repo import get_repo

    return get_repo().points_balance(user_id)


def topup(user_id: str, points: int, note: str = "") -> int:
    from storage.firestore_repo import get_repo

    if points <= 0:
        raise ValueError("儲值點數必須為正")
    return get_repo().append_ledger(user_id, "topup", points, related_job_id=None, note=note)


def charge(user_id: str, points: int, job_id: str) -> int:
    """扣點。餘額不足時丟 InsufficientPoints，不寫入任何紀錄。"""
    from storage.firestore_repo import get_repo

    repo = get_repo()
    bal = repo.points_balance(user_id)
    if bal < points:
        raise InsufficientPoints(bal, points)
    return repo.append_ledger(user_id, "charge", -points, related_job_id=job_id)


def refund(user_id: str, points: int, job_id: str) -> int:
    """生成失敗全額退點；沉沒 token 成本由平台吸收（markup 要 >1.1 的原因之一）。"""
    from storage.firestore_repo import get_repo

    return get_repo().append_ledger(user_id, "refund", points, related_job_id=job_id)
