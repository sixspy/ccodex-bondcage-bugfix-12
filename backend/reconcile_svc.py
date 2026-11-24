"""对账互证：笼面可用 + 已拨付 + 已罚没 须等于笼面金额（同代口径）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])

ACTIVE_STATUSES = ("已冻结", "核验通过", "核验违约", "部分解笼", "部分罚没")


def _has_col(row, name: str) -> bool:
    try:
        row[name]
        return True
    except (IndexError, KeyError):
        return False


def _reconcile_cage(conn, cage) -> dict:
    cage_id = cage["id"]
    gen = int(cage["generation"])
    amount = float(cage["amount"])
    available = float(cage["available"])
    released = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM releases
           WHERE cage_id=? AND status='已拨付' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    forfeited = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM forfeits
           WHERE cage_id=? AND status='已罚没' AND cage_generation=?""",
        (cage_id, gen),
    ).fetchone()["s"]
    # 已拆分原笼：切出子笼金额计入「拆出」，原笼可用归零后仍须闭合
    split_out = conn.execute(
        """SELECT COALESCE(SUM(amount),0) s FROM cages
           WHERE parent_cage_id=? AND status!='已回退'""",
        (cage_id,),
    ).fetchone()["s"]
    # 双轨检：同一原笼既关闭又有子笼出金轨 = 假绿；或原笼活跃却存在可拨付孤儿子笼
    child_live = conn.execute(
        """SELECT COUNT(*) c FROM cages
           WHERE parent_cage_id=? AND status IN ('核验通过','部分解笼')""",
        (cage_id,),
    ).fetchone()["c"]
    accounted = round(float(available) + float(released) + float(forfeited) + float(split_out), 6)
    ok = abs(accounted - amount) < 1e-6
    closed_readonly = bool(int(cage["readonly"] or 0)) if _has_col(cage, "readonly") else False
    dual_track = bool(closed_readonly and (float(released) > 0 or float(forfeited) > 0))
    dual_track = dual_track or bool(child_live > 0 and not closed_readonly)
    # 账户侧：该当事人冻结额应覆盖所有仍冻结/核验中的笼可用
    party = cage["party_key"]
    acct = conn.execute(
        "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
        (party,),
    ).fetchone()
    live_frozen = conn.execute(
        """SELECT COALESCE(SUM(available),0) s FROM cages
           WHERE party_key=? AND status IN ('已冻结','核验通过','核验违约','部分解笼','部分罚没')""",
        (party,),
    ).fetchone()["s"]
    return {
        "cage_id": cage_id,
        "cage_code": cage["cage_code"],
        "party_key": party,
        "contract_no": cage["contract_no"],
        "generation": gen,
        "status": cage["status"],
        "amount": amount,
        "available": available,
        "released": float(released),
        "forfeited": float(forfeited),
        "accounted": accounted,
        "balanced": ok,
        "gap": round(amount - accounted, 6),
        "split_out": float(split_out),
        "closed_readonly": closed_readonly,
        "dual_track": dual_track,
        "account_balance": float(acct["balance"]) if acct else None,
        "account_frozen": float(acct["frozen"]) if acct else None,
        "live_cage_available": float(live_frozen),
        "proof": {
            "formula": "available + released + forfeited + split_out == amount（同笼代）",
            "left": accounted,
            "right": amount,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cages = conn.execute("SELECT * FROM cages ORDER BY id").fetchall()
        items = [_reconcile_cage(conn, c) for c in cages]
        bad = [i for i in items if not i["balanced"]]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
            },
        }


@router.get("/api/reconcile/{cage_code}")
def reconcile_one(cage_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute(
            "SELECT * FROM cages WHERE cage_code=?", (cage_code,)
        ).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        return _reconcile_cage(conn, cage)


@router.get("/api/proof/{party_key}")
def party_proof(party_key: str, authorization: AuthHeader = None):
    """当事人级互证：账户冻结 vs 活笼可用合计。"""
    require_user(authorization)
    with connect() as conn:
        acct = conn.execute(
            "SELECT * FROM bond_accounts WHERE party_key=?", (party_key,)
        ).fetchone()
        if not acct:
            raise HTTPException(404, "保证金户不存在")
        live = conn.execute(
            """SELECT COALESCE(SUM(available),0) s FROM cages
               WHERE party_key=? AND status IN
               ('已冻结','核验通过','核验违约','部分解笼','部分罚没')""",
            (party_key,),
        ).fetchone()["s"]
        released = conn.execute(
            """SELECT COALESCE(SUM(r.amount),0) s FROM releases r
               JOIN cages c ON c.id=r.cage_id
               WHERE c.party_key=? AND r.status='已拨付'""",
            (party_key,),
        ).fetchone()["s"]
        forfeited = conn.execute(
            """SELECT COALESCE(SUM(f.amount),0) s FROM forfeits f
               JOIN cages c ON c.id=f.cage_id
               WHERE c.party_key=? AND f.status='已罚没'""",
            (party_key,),
        ).fetchone()["s"]
        frozen = float(acct["frozen"])
        live_f = float(live)
        return {
            "party_key": party_key,
            "account": dict(acct),
            "live_cage_available": live_f,
            "released_total": float(released),
            "forfeited_total": float(forfeited),
            "frozen_matches_live": abs(frozen - live_f) < 1e-6,
            "proof": {
                "formula": "account.frozen == sum(live cage available)",
                "left": frozen,
                "right": live_f,
            },
        }
