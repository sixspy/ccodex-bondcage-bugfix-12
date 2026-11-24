"""入笼冻结：可用保证金划入笼内，写笼代。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import CageFreezeIn

router = APIRouter(tags=["cages"])

# 原笼仍活跃的唯一身份：只有已冻结笼可拆/可增补；已拆分笼只读
LIVE_STATUSES = ("已冻结",)


def _cage_dict(row) -> dict:
    """统一外发口径：readonly 归一为布尔，供前端禁用写入口。"""
    d = dict(row)
    d["readonly"] = bool(int(d.get("readonly") or 0))
    return d


def create_child_cage(conn, parent, amount: float, suffix: str, now: str):
    """拆笼子腿：同义务子笼，冻结额从原笼可用平移，不动账户冻结总额。"""
    child_code = f"{parent['cage_code']}{suffix}"
    dup = conn.execute(
        "SELECT id FROM cages WHERE cage_code=?", (child_code,)
    ).fetchone()
    if dup:
        raise HTTPException(409, "子笼编码已存在，禁止重复拆笼")
    cur = conn.execute(
        """INSERT INTO cages(cage_code,party_key,contract_no,amount,available,
           generation,status,beneficiary,created_at,frozen_at,parent_cage_id,readonly)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
        (
            child_code,
            parent["party_key"],
            parent["contract_no"],
            float(amount),
            float(amount),
            int(parent["generation"]),
            "已冻结",
            parent["beneficiary"],
            now,
            now,
            parent["id"],
        ),
    )
    return cur.lastrowid, child_code


def _next_cage_code(conn, party_key: str) -> str:
    n = conn.execute(
        "SELECT COUNT(*) c FROM cages WHERE party_key=?", (party_key,)
    ).fetchone()["c"]
    return f"CG-{party_key}-{n + 1:03d}"


@router.get("/api/cages")
def list_cages(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cages WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cages ORDER BY id DESC").fetchall()
        return {"items": [_cage_dict(r) for r in rows]}


@router.get("/api/cages/{cage_id}")
def get_cage(cage_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        atts = conn.execute(
            "SELECT * FROM attestations WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        rels = conn.execute(
            "SELECT * FROM releases WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        fors = conn.execute(
            "SELECT * FROM forfeits WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        legs = conn.execute(
            """SELECT l.*, ch.cage_code AS child_code, ch.status AS child_status
               FROM split_legs l LEFT JOIN cages ch ON ch.id=l.child_cage_id
               WHERE l.parent_cage_id=? ORDER BY l.id""",
            (cage_id,),
        ).fetchall()
        minutes = conn.execute(
            "SELECT * FROM split_decisions WHERE cage_id=?", (cage_id,)
        ).fetchone()
        return {
            "cage": _cage_dict(cage),
            "attestations": [dict(a) for a in atts],
            "releases": [dict(r) for r in rels],
            "forfeits": [dict(f) for f in fors],
            "split_legs": [dict(x) for x in legs],
            "split_decision": dict(minutes) if minutes else None,
        }


@router.post("/api/cages/freeze")
def freeze_cage(body: CageFreezeIn, authorization: AuthHeader = None):
    """从账户可用余额划入新笼并冻结。"""
    user = require_writer(authorization)
    lock_key = f"bond:{body.party_key}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            acct = conn.execute(
                "SELECT * FROM bond_accounts WHERE party_key=?",
                (body.party_key,),
            ).fetchone()
            if not acct:
                raise HTTPException(404, "保证金户不存在")
            if acct["status"] != "正常":
                raise HTTPException(409, "账户状态禁止入笼")
            amount = float(body.amount)
            free = float(acct["balance"]) - float(acct["frozen"])
            if amount > free + 1e-9:
                raise HTTPException(409, "可用保证金不足，无法入笼")
            code = body.cage_code or _next_cage_code(conn, body.party_key)
            dup = conn.execute(
                "SELECT id FROM cages WHERE cage_code=?", (code,)
            ).fetchone()
            if dup:
                raise HTTPException(409, "笼编码已存在")
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO cages(cage_code,party_key,contract_no,amount,available,
                   generation,status,beneficiary,created_at,frozen_at,parent_cage_id,readonly)
                   VALUES (?,?,?,?,?,?,?,?,?,?,NULL,0)""",
                (
                    code,
                    body.party_key,
                    body.contract_no,
                    amount,
                    amount,
                    1,
                    "已冻结",
                    body.beneficiary,
                    now,
                    now,
                ),
            )
            cage_id = cur.lastrowid
            conn.execute(
                "UPDATE bond_accounts SET frozen = frozen + ? WHERE party_key=?",
                (amount, body.party_key),
            )
            write_audit(
                conn,
                user,
                "cage_freeze",
                code,
                {
                    "cage_id": cage_id,
                    "amount": amount,
                    "contract_no": body.contract_no,
                    "beneficiary": body.beneficiary,
                    "generation": 1,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()
