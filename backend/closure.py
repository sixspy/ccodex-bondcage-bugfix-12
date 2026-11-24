"""原笼关闭口径锁：切分成功后原笼收口为「已拆分」只读，一切写口零增拒收。

口径（纪要锁定，当日不翻案）：
- 甲路径「整笔切分」：切分事务成功 ⇒ 原笼号标已拆分、可用归零、只读；
  核验 / 解笼 / 罚没 / 增补 / 再拆 等写口一律正式拒收，且落「原笼关闭偏航」审计。
- 乙路径「影子子笼」：原笼仍呈活跃态 ⇒ 子笼晋升行正式零改回退，只留查询快照，
  快照不得进入拨付；原笼自身核验/解笼生命周期照旧（只此一轨）。
- 两条路径互斥，禁止第三路中性描述，禁止同一原笼两边混用。
"""
from __future__ import annotations

from fastapi import HTTPException

from db import write_audit_standalone

CLOSED_STATUS = "已拆分"
ACTIVE_STATUS = "活跃"
DRIFT_ACTION = "cage_closure_drift"
RULING = "原笼关闭偏航并拒写"
PATH_FULL = "甲-整笔切分"
PATH_SHADOW = "乙-影子子笼"


def is_closed(cage) -> bool:
    if cage is None:
        return False
    try:
        readonly = int(cage["readonly"] or 0)
    except (IndexError, KeyError):
        readonly = 0
    return cage["status"] == CLOSED_STATUS or readonly == 1


def drift_payload(cage, attempt: str, note: str = "") -> dict:
    return {
        "cage_id": cage["id"],
        "cage_code": cage["cage_code"],
        "status": cage["status"],
        "attempt": attempt,
        "ruling": RULING,
        "note": note,
    }


def reject_closed_write(conn, cage, actor: str, attempt: str, note: str = "") -> None:
    """已拆分原笼：落偏航审计后 409，调用方无任何业务写入。"""
    if not is_closed(cage):
        return
    write_audit_standalone(
        actor, DRIFT_ACTION, cage["cage_code"], drift_payload(cage, attempt, note)
    )
    raise HTTPException(409, f"原笼{CLOSED_STATUS}只读，禁止{attempt}：{RULING}")


def reject_reopen(conn, cage, actor: str, path: str, note: str) -> None:
    """纪要已锁口径：同一原笼禁止第二次拆笼或换路（翻案）。"""
    write_audit_standalone(
        actor,
        DRIFT_ACTION,
        cage["cage_code"],
        drift_payload(cage, path, f"纪要已锁口径，当日翻案拒写；{note}"),
    )
    raise HTTPException(409, "原笼关闭命运已落纪要，当日不予翻案，禁止混用拆笼路径")
