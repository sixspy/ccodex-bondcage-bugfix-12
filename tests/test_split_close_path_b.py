"""业务失败测乙：影子子笼路径下原笼保持活跃，子笼晋升行零改回退只留查询快照。

红因（修复前）：影子子笼也偷插 -L 子笼并保持可解笼，原笼 + 子笼双轨并存。
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
            json={"party_key": "P-CLOSE-B", "party_name": "乙活跃", "initial_deposit": 40000},
        ).status_code
        == 200
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-CLOSE-B",
            "contract_no": "HT-CLOSE-B",
            "amount": 40000,
            "beneficiary": "乙受益人",
            "cage_code": "CG-CLOSE-B-001",
        },
    ).json()
    assert cage["status"] == "已冻结"
    return cage


def test_shadow_split_keeps_original_active_and_no_child_can_be_disbursed():
    c = TestClient(app)
    h = _auth(c)
    cage = _make_cage(c, h)

    with connect() as conn:
        codes_before = {r["cage_code"] for r in conn.execute("SELECT cage_code FROM cages")}
        count_before = len(codes_before)

    r = c.post(
        "/api/split-stub",
        headers=h,
        json={
            "cage_id": cage["id"],
            "left_amount": 15000,
            "right_amount": 25000,
            "mode": "影子子笼",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["cages_unchanged"] is True

    # 乙路径：原笼保持活跃，不得出现任何可拨付子笼
    with connect() as conn:
        codes_after = {r["cage_code"] for r in conn.execute("SELECT cage_code FROM cages")}
    assert codes_after == codes_before, "影子子笼不得产出 cages 行（子笼晋升行须零改回退）"
    assert not any(x.startswith(cage["cage_code"] + "-") for x in codes_after)

    original = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert original["status"] == "已冻结", "乙路径原笼仍呈活跃态"
    assert original["available"] == 40000

    # 只留查询快照：split_jobs 有影腿，split_legs 为快照且无 child_cage_id
    with connect() as conn:
        job = conn.execute(
            "SELECT * FROM split_jobs WHERE cage_id=? ORDER BY id DESC LIMIT 1",
            (cage["id"],),
        ).fetchone()
        assert job is not None
        assert job["status"] == "影子快照"
        detail = json.loads(job["detail"])
        assert detail["promotable"] is False
        assert detail["disbursable"] is False
        legs = conn.execute(
            "SELECT * FROM split_legs WHERE parent_cage_id=? ORDER BY leg", (cage["id"],)
        ).fetchall()
        assert len(legs) == 2
        assert all(x["status"] == "快照" and x["child_cage_id"] is None for x in legs)

    minutes = c.get("/api/split/minutes", headers=h).json()["items"]
    mine = [m for m in minutes if m["cage_id"] == cage["id"]]
    assert len(mine) == 1
    assert mine[0]["path"] == "乙-影子子笼"
    assert mine[0]["closed_status"] == "活跃"
    assert mine[0]["no_reopen"] == 1

    # 乙路径下原笼仍可走自身核验/解笼生命周期（只此一轨）
    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage["id"], "outcome": "通过", "evidence": "验收"},
        ).status_code
        == 200
    )
    rel = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert rel.status_code == 200, rel.text
    assert rel.json()["amount"] == 40000
    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["dual_track"] is False

    # 纪要锁口径：乙路径当日不得翻案改走整笔切分
    with connect() as conn:
        drift_before = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_closure_drift'"
        ).fetchone()["c"]
    again = c.post(
        "/api/split-stub",
        headers=h,
        json={
            "cage_id": cage["id"],
            "left_amount": 1000,
            "right_amount": 1000,
            "mode": "整笔切分",
        },
    )
    assert again.status_code == 409, "乙路径已锁口径，禁止当日翻案/混用"
    with connect() as conn:
        drift_after = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='cage_closure_drift'"
        ).fetchone()["c"]
        assert drift_after > drift_before
        assert (
            conn.execute("SELECT COUNT(*) c FROM cages").fetchone()["c"] == count_before
        )
