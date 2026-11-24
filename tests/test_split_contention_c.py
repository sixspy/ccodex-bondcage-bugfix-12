"""争用失败测丙：双会话同时发起「拆笼」与「原笼核验」，同笼锁只能放行一侧。

红因（修复前）：拆笼与核验各自短暂持锁后释放，且拆笼不关原笼，
两请求可同时成功，原笼子笼双轨互证假绿。
"""
import threading

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
            json={"party_key": "P-RACE-C", "party_name": "丙争用", "initial_deposit": 40000},
        ).status_code
        == 200
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-RACE-C",
            "contract_no": "HT-RACE-C",
            "amount": 40000,
            "beneficiary": "丙受益人",
            "cage_code": "CG-RACE-C-001",
        },
    ).json()
    assert cage["status"] == "已冻结"
    return cage


def test_concurrent_split_and_attest_contend_on_same_cage_lock():
    cage = None
    results = {}
    errors = {}

    # 双会话：admin 发起拆笼，clerk 同时发起原笼核验（各自独立 TestClient）
    c_split = TestClient(app)
    c_attest = TestClient(app)
    h_split = _auth(c_split, "admin")
    h_attest = _auth(c_attest, "clerk")
    cage = _make_cage(c_split, h_split)
    assert cage["id"]

    barrier = threading.Barrier(2)

    def run_split():
        try:
            barrier.wait(timeout=10)
            results["split"] = c_split.post(
                "/api/split-stub",
                headers=h_split,
                json={
                    "cage_id": cage["id"],
                    "left_amount": 15000,
                    "right_amount": 25000,
                    "mode": "整笔切分",
                },
            )
        except Exception as exc:  # pragma: no cover - 失败时给出可读信息
            errors["split"] = repr(exc)

    def run_attest():
        try:
            barrier.wait(timeout=10)
            results["attest"] = c_attest.post(
                "/api/attestations",
                headers=h_attest,
                json={"cage_id": cage["id"], "outcome": "通过", "evidence": "并发核验"},
            )
        except Exception as exc:  # pragma: no cover
            errors["attest"] = repr(exc)

    t1 = threading.Thread(target=run_split)
    t2 = threading.Thread(target=run_attest)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors, errors
    assert "split" in results and "attest" in results

    codes = sorted([results["split"].status_code, results["attest"].status_code])
    assert codes == [200, 409], f"同拆同核并发必须一放一拒，实际 {codes}"

    split_ok = results["split"].status_code == 200
    with connect() as conn:
        original = conn.execute("SELECT * FROM cages WHERE id=?", (cage["id"],)).fetchone()
        att_n = conn.execute(
            "SELECT COUNT(*) c FROM attestations WHERE cage_id=?", (cage["id"],)
        ).fetchone()["c"]
        job = conn.execute(
            "SELECT * FROM split_jobs WHERE cage_id=? ORDER BY id DESC LIMIT 1",
            (cage["id"],),
        ).fetchone()
        legs = conn.execute(
            "SELECT * FROM split_legs WHERE parent_cage_id=?", (cage["id"],)
        ).fetchall()
        child_n = conn.execute(
            "SELECT COUNT(*) c FROM cages WHERE parent_cage_id=?", (cage["id"],)
        ).fetchone()["c"]
        cont_n = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (f"cage:{cage['id']}",),
        ).fetchone()["c"]
        race_audit = conn.execute(
            """SELECT COUNT(*) c FROM audit_trail
               WHERE action IN ('split_attest_contention','cage_closure_drift')"""
        ).fetchone()["c"]

    if split_ok:
        # 拆笼胜出：原笼关闭只读，核验零增
        assert original["status"] == "已拆分"
        assert original["available"] == 0
        assert att_n == 0, "拆笼胜出后原笼核验必须零增"
        assert job is not None and job["status"] == "已切分"
        assert len(legs) == 2 and all(x["child_cage_id"] for x in legs)
        assert child_n == 2
        recon = c_split.get(f"/api/reconcile/{cage['cage_code']}", headers=h_split).json()
        assert recon["balanced"] is True
        assert recon["dual_track"] is False
    else:
        # 核验胜出：原笼保持活跃单轨，拆笼正式零改拒收
        assert original["status"] == "核验通过"
        assert att_n == 1
        assert job is None, "核验胜出后拆笼不得落 job"
        assert legs == []
        assert child_n == 0
        recon = c_split.get(f"/api/reconcile/{cage['cage_code']}", headers=h_split).json()
        assert recon["balanced"] is True
        assert recon["dual_track"] is False

    # 后到争用必须留下可脚本复现的争用证据
    assert cont_n >= 1 or race_audit >= 1, "并发撞锁必须落 contention 或争用审计"
