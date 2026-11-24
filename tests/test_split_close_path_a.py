"""业务失败测甲：整笔切分成功后原笼必须关闭只读，核验/出金写口零增拒收。

红因（修复前）：split_svc 切分成功后原笼仍为「已冻结」，柜员可对原笼再核验、
再解笼，形成原笼 + 子笼双轨假绿。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_cage(c: TestClient, h: dict) -> dict:
    assert (
        c.post(
            "/api/bonds",
            headers=h,
            json={"party_key": "P-CLOSE-A", "party_name": "甲关闭", "initial_deposit": 40000},
        ).status_code
        == 200
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-CLOSE-A",
            "contract_no": "HT-CLOSE-A",
            "amount": 40000,
            "beneficiary": "甲受益人",
            "cage_code": "CG-CLOSE-A-001",
        },
    ).json()
    assert cage["status"] == "已冻结"
    return cage


def test_split_full_path_closes_original_and_rejects_every_write():
    c = TestClient(app)
    h = _auth(c)
    cage = _make_cage(c, h)

    r = c.post(
        "/api/split-stub",
        headers=h,
        json={
            "cage_id": cage["id"],
            "left_amount": 15000,
            "right_amount": 25000,
            "mode": "整笔切分",
        },
    )
    assert r.status_code == 200, r.text

    # 甲路径：原笼号必须标已拆分、可用归零、只读
    original = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert original["status"] == "已拆分", "切分成功后原笼必须收口为已拆分"
    assert original["available"] == 0, "原笼可用额必须归零，不得继续出金"
    assert original.get("readonly") is True

    # 子笼两腿生效，金额守恒（同义务，允许各自核验/解笼）
    with connect() as conn:
        legs = conn.execute(
            "SELECT * FROM split_legs WHERE parent_cage_id=? ORDER BY leg",
            (cage["id"],),
        ).fetchall()
        assert len(legs) == 2, "整笔切分必须落两条生效子腿"
        assert all(x["status"] == "生效" and x["child_cage_id"] for x in legs)
        child_ids = [x["child_cage_id"] for x in legs]
        children = conn.execute(
            "SELECT * FROM cages WHERE id IN (?,?) ORDER BY id", child_ids
        ).fetchall()
        assert len(children) == 2
        assert abs(sum(float(x["amount"]) for x in children) - 40000) < 1e-6
        assert all(x["status"] == "已冻结" and x["parent_cage_id"] == cage["id"] for x in children)

    # 一切核验/出金写口：正式零增拒收
    with connect() as conn:
        before = {
            t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("attestations", "releases", "forfeits", "topup_jobs")
        }
        frozen_before = conn.execute(
            "SELECT frozen FROM bond_accounts WHERE party_key='P-CLOSE-A'"
        ).fetchone()["frozen"]

    attempts = [
        ("/api/attestations", {"cage_id": cage["id"], "outcome": "通过", "evidence": "验收合格"}),
        ("/api/attestations", {"cage_id": cage["id"], "outcome": "违约", "evidence": "逾期"}),
        ("/api/releases", {"cage_id": cage["id"]}),
        ("/api/forfeits", {"cage_id": cage["id"], "reason": "违约"}),
        ("/api/topup-stub", {"cage_id": cage["id"], "amount": 1000, "mode": "同笼扩代"}),
    ]
    for path, payload in attempts:
        resp = c.post(path, headers=h, json=payload)
        assert resp.status_code == 409, f"{path} 对已拆分原笼必须 409 拒收，实际 {resp.status_code}"

    with connect() as conn:
        after = {
            t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in before
        }
        assert after == before, "已拆分原笼的核验/出金写口必须零增"
        assert (
            conn.execute(
                "SELECT available FROM cages WHERE id=?", (cage["id"],)
            ).fetchone()["available"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT frozen FROM bond_accounts WHERE party_key='P-CLOSE-A'"
            ).fetchone()["frozen"]
            == frozen_before
        )
        drift = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_closure_drift'"
        ).fetchone()["c"]
        assert drift >= 3, "偏航必须落审计：原笼关闭偏航并拒写"

    # 纪要锁口径：甲路径、已拆分、当日不翻案
    minutes = c.get("/api/split/minutes", headers=h).json()["items"]
    mine = [m for m in minutes if m["cage_id"] == cage["id"]]
    assert len(mine) == 1
    assert mine[0]["path"] == "甲-整笔切分"
    assert mine[0]["closed_status"] == "已拆分"
    assert mine[0]["no_reopen"] == 1

    # 对账必须真绿，且原笼计入拆出子笼，不得双轨假绿
    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["closed_readonly"] is True
    assert recon["dual_track"] is False
    assert abs(recon["split_out"] - 40000) < 1e-6
    assert recon["proof"]["left"] == recon["proof"]["right"]

    # 当日翻案：原笼不能再走第二种拆笼路径
    again = c.post(
        "/api/split-stub",
        headers=h,
        json={
            "cage_id": cage["id"],
            "left_amount": 1000,
            "right_amount": 999,
            "mode": "影子子笼",
        },
    )
    assert again.status_code == 409, "纪要已锁口径，禁止当日翻案/混用"
    with connect() as conn:
        drift2 = conn.execute(
            "SELECT payload FROM audit_trail WHERE action='cage_closure_drift' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert drift2 is not None and "翻案" in (drift2["payload"] or "")
