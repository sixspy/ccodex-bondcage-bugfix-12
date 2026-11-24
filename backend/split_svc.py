"""拆笼分拨：整笔切分收口原笼 / 影子子笼只留查询快照（两路互斥）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from cages_svc import create_child_cage
from closure import (
    ACTIVE_STATUS,
    CLOSED_STATUS,
    PATH_FULL,
    PATH_SHADOW,
    is_closed,
    reject_closed_write,
    reject_reopen,
)
from db import connect, now_iso, write_audit, write_audit_standalone
from locks import release, require_lock
from models import SplitIn

router = APIRouter(tags=["split"])

MODES = {"整笔切分", "影子子笼"}


@router.get("/api/split/jobs")
def list_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT j.*, c.cage_code FROM split_jobs j
               JOIN cages c ON c.id=j.cage_id
               ORDER BY j.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/split/legs")
def list_legs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.*, c.cage_code AS parent_code, ch.cage_code AS child_code
               FROM split_legs l
               JOIN cages c ON c.id=l.parent_cage_id
               LEFT JOIN cages ch ON ch.id=l.child_cage_id
               ORDER BY l.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/split/minutes")
def list_minutes(authorization: AuthHeader = None):
    """拆笼纪要：原笼关闭命运一次落定，no_reopen=1 当日不翻案。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM split_decisions ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


def _record_decision(conn, cage, job_id: int, path: str, mode: str, closed_status: str, now: str):
    conn.execute(
        """INSERT INTO split_decisions(cage_id,cage_code,job_id,path,mode,closed_status,
           no_reopen,locked_on,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            cage["id"],
            cage["cage_code"],
            job_id,
            path,
            mode,
            closed_status,
            1,
            now[:10],
            now,
        ),
    )


@router.post("/api/split-stub")
def split_stub(body: SplitIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    if body.mode not in MODES:
        raise HTTPException(400, "mode 须为整笔切分或影子子笼")
    lock_key = f"cage:{body.cage_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        committed = False
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            # 纪要锁口径：原笼关闭命运只落一次，第二路/第二遍皆属翻案
            prior = conn.execute(
                "SELECT * FROM split_decisions WHERE cage_id=?", (body.cage_id,)
            ).fetchone()
            if prior:
                reject_reopen(
                    conn,
                    cage,
                    user,
                    body.mode,
                    f"已落 {prior['path']}（原笼 {prior['closed_status']}）",
                )
            # 已拆分原笼只读：任何写口零增拒收
            if is_closed(cage):
                reject_closed_write(conn, cage, user, "拆笼", "原笼已关闭")
            # 单轨：只有仍活跃未核验的已冻结笼可拆，已走核验/解笼的笼不得另开一路
            if cage["status"] != "已冻结":
                write_audit_standalone(
                    user,
                    "split_attest_contention",
                    cage["cage_code"],
                    {
                        "cage_id": cage["id"],
                        "status": cage["status"],
                        "attempt": body.mode,
                        "note": "原笼已在核验/解笼轨，拆笼不得另开一路",
                    },
                )
                raise HTTPException(409, "仅已冻结笼可拆笼；已核验/已解笼笼禁止另开拆笼轨")

            left = float(body.left_amount)
            right = float(body.right_amount)
            if abs(left + right - float(cage["available"])) > 1e-6:
                raise HTTPException(
                    400,
                    "左右金额之和须等于笼可用额",
                )
            now = now_iso()
            job_status = "已切分" if body.mode == "整笔切分" else "影子快照"

            if body.mode == "整笔切分":
                detail = {
                    "path": PATH_FULL,
                    "promotable": True,
                    "disbursable": True,
                    "closure": CLOSED_STATUS,
                }
                cur = conn.execute(
                    """INSERT INTO split_jobs(cage_id,left_amount,right_amount,mode,status,detail,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (body.cage_id, left, right, body.mode, job_status,
                     json.dumps(detail, ensure_ascii=False), now),
                )
                job_id = cur.lastrowid
                children = []
                for leg, amount, suffix in ((1, left, "-L"), (2, right, "-R")):
                    child_id, child_code = create_child_cage(
                        conn, cage, amount, suffix, now
                    )
                    conn.execute(
                        """INSERT INTO split_legs(job_id,parent_cage_id,leg,amount,mode,
                           status,child_cage_id,snapshot,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            job_id,
                            cage["id"],
                            leg,
                            amount,
                            body.mode,
                            "生效",
                            child_id,
                            json.dumps(
                                {"leg": leg, "amount": amount, "child_code": child_code},
                                ensure_ascii=False,
                            ),
                            now,
                        ),
                    )
                    children.append({"leg": leg, "cage_id": child_id, "cage_code": child_code,
                                     "amount": amount, "status": "已冻结"})
                # 原笼收口：已拆分、可用归零、只读
                conn.execute(
                    """UPDATE cages SET status=?, available=0, readonly=1,
                       closed_at=?, close_reason=? WHERE id=?""",
                    (CLOSED_STATUS, now, PATH_FULL, cage["id"]),
                )
                _record_decision(
                    conn, cage, job_id, PATH_FULL, body.mode, CLOSED_STATUS, now
                )
                write_audit(
                    conn,
                    user,
                    "split_full",
                    cage["cage_code"],
                    {
                        "job_id": job_id,
                        "left": left,
                        "right": right,
                        "path": PATH_FULL,
                        "original_status": CLOSED_STATUS,
                        "children": children,
                    },
                )
                conn.commit()
                committed = True
                return {
                    "job_id": job_id,
                    "status": job_status,
                    "cages_unchanged": False,
                    "mode": body.mode,
                    "path": PATH_FULL,
                    "original_status": CLOSED_STATUS,
                    "children": children,
                }

            # 乙路径：影子子笼只留查询快照，子笼晋升行零改回退，原笼保持活跃
            detail = {
                "path": PATH_SHADOW,
                "promotable": False,
                "disbursable": False,
                "closure": ACTIVE_STATUS,
                "note": "影子快照仅供查询，不入拨付",
            }
            cur = conn.execute(
                """INSERT INTO split_jobs(cage_id,left_amount,right_amount,mode,status,detail,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (body.cage_id, left, right, body.mode, job_status,
                 json.dumps(detail, ensure_ascii=False), now),
            )
            job_id = cur.lastrowid
            for leg, amount in ((1, left), (2, right)):
                conn.execute(
                    """INSERT INTO split_legs(job_id,parent_cage_id,leg,amount,mode,
                       status,child_cage_id,snapshot,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        cage["id"],
                        leg,
                        amount,
                        body.mode,
                        "快照",
                        None,
                        json.dumps(
                            {"leg": leg, "amount": amount, "promoted": False},
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
            _record_decision(
                conn, cage, job_id, PATH_SHADOW, body.mode, ACTIVE_STATUS, now
            )
            write_audit(
                conn,
                user,
                "split_shadow_snapshot",
                cage["cage_code"],
                {
                    "job_id": job_id,
                    "left": left,
                    "right": right,
                    "path": PATH_SHADOW,
                    "original_status": cage["status"],
                    "note": "shadow_snapshot_only",
                },
            )
            conn.commit()
            committed = True
            return {
                "job_id": job_id,
                "status": job_status,
                "cages_unchanged": True,
                "mode": body.mode,
                "path": PATH_SHADOW,
                "original_status": cage["status"],
            }
        finally:
            if not committed:
                # 拒写/中途异常：拆笼绝不留半截 job/leg/子笼
                conn.rollback()
            release(conn, lock_key, user)
            conn.commit()
