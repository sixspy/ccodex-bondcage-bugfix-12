"""本机文件库走查：先拆笼，再对原笼点核验。

用途：在 data/app.db（本机文件库）上复现/验证原笼关闭口径。
- 修复前：拆笼后原笼仍活跃且偷偷生子笼，再核验仍成功 → 双轨假绿。
- 修复后：拆笼后原笼「已拆分」只读，任何再核验/出金写口正式拒收，对账不得双轨。

运行：
    .venv/bin/python scripts/local_db_walkthrough.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import DB_PATH, connect, init_db  # noqa: E402


def auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post("/api/login", json={"username": user, "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def main() -> int:
    init_db()
    c = TestClient(app)
    h = auth(c)
    tag = time.strftime("%H%M%S")
    party = "P-WALK-" + tag
    code = "CG-WALK-" + tag

    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": party, "party_name": "走查户", "initial_deposit": 40000},
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": "HT-WALK-" + tag,
            "amount": 40000,
            "beneficiary": "走查受益人",
            "cage_code": code,
        },
    )
    if cage.status_code != 200:
        print("入笼失败", cage.status_code, cage.text)
        return 1
    cage = cage.json()

    split = c.post(
        "/api/split-stub",
        headers=h,
        json={
            "cage_id": cage["id"],
            "left_amount": 15000,
            "right_amount": 25000,
            "mode": "整笔切分",
        },
    )
    attest = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage["id"], "outcome": "通过", "evidence": "拆笼后再点核验"},
    )
    release = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    detail = c.get(f"/api/cages/{cage['id']}", headers=h).json()
    recon = c.get(f"/api/reconcile/{code}", headers=h).json()
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cages)")}
        if "parent_cage_id" in cols:
            children = conn.execute(
                "SELECT id,cage_code,status,available FROM cages WHERE parent_cage_id=?",
                (cage["id"],),
            ).fetchall()
            child_rows = [dict(r) for r in children]
        else:
            child_rows = []
        if not child_rows:
            # 修复前旧实现：-L 子笼无 parent_cage_id，按编码前缀兜底
            child_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,cage_code,status,available FROM cages WHERE cage_code LIKE ?",
                    (code + "-%",),
                ).fetchall()
            ]
        codes = {
            r["cage_code"]: dict(r)
            for r in conn.execute(
                "SELECT cage_code,status,available FROM cages WHERE cage_code LIKE ?",
                (code + "%",),
            ).fetchall()
        }
        drift = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_closure_drift'"
        ).fetchone()["c"]

    out = {
        "db": str(DB_PATH),
        "cage_id": cage["id"],
        "cage_code": code,
        "split_http": split.status_code,
        "split_body": split.json() if split.headers.get("content-type", "").startswith("application/json") else split.text,
        "original_status": detail["cage"]["status"],
        "original_available": detail["cage"]["available"],
        "original_readonly": detail["cage"].get("readonly"),
        "attest_after_split_http": attest.status_code,
        "release_after_split_http": release.status_code,
        "children_in_cages": child_rows,
        "same_obligation_codes": codes,
        "reconcile": {
            "balanced": recon["balanced"],
            "status": recon["status"],
            "closed_readonly": recon.get("closed_readonly"),
            "dual_track": recon.get("dual_track"),
            "split_out": recon.get("split_out"),
            "gap": recon["gap"],
        },
        "closure_drift_audits": drift,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    dual_track_fake_green = (
        detail["cage"]["status"] != "已拆分"
        and attest.status_code == 200
        and len(child_rows) >= 1
    )
    out["verdict"] = "DUAL_TRACK_FAKE_GREEN" if dual_track_fake_green else "SINGLE_TRACK_OK"
    print("VERDICT:", out["verdict"])
    return 0 if not dual_track_fake_green else 2


if __name__ == "__main__":
    raise SystemExit(main())
