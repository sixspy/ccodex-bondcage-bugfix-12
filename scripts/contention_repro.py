"""争用脚本复现：HTTP 双请求同时发起「拆笼」与「原笼核验」，撞同一笼锁。

用法（服务已在 127.0.0.1:10608 起）：
    .venv/bin/python scripts/contention_repro.py
退出码：0 = 一放一拒且无业务双写；2 = 违背口径。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:10608"


def call(path, token, payload=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def login(user):
    req = urllib.request.Request(
        BASE + "/api/login",
        data=json.dumps({"username": user, "password": "123456"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["token"]


def main() -> int:
    tag = time.strftime("%H%M%S") + f"-{int(time.time_ns() % 1000000):06d}"
    party = "P-RACE-HTTP-" + tag
    code = "CG-RACE-HTTP-" + tag
    admin = login("admin")
    clerk = login("clerk")

    s, _ = call(
        "/api/bonds",
        admin,
        {"party_key": party, "party_name": "HTTP争用", "initial_deposit": 40000},
    )
    print("开户", s)
    st, cage = call(
        "/api/cages/freeze",
        admin,
        {
            "party_key": party,
            "contract_no": "HT-RACE-" + tag,
            "amount": 40000,
            "beneficiary": "争用受益人",
            "cage_code": code,
        },
    )
    if st != 200:
        print("入笼失败", st, cage)
        return 2
    cage_id = cage["id"]

    results = {}
    barrier = threading.Barrier(2)

    def do_split():
        barrier.wait(timeout=10)
        results["split"] = call(
            "/api/split-stub",
            admin,
            {"cage_id": cage_id, "left_amount": 15000, "right_amount": 25000, "mode": "整笔切分"},
        )

    def do_attest():
        barrier.wait(timeout=10)
        results["attest"] = call(
            "/api/attestations",
            clerk,
            {"cage_id": cage_id, "outcome": "通过", "evidence": "并发核验"},
        )

    t1 = threading.Thread(target=do_split)
    t2 = threading.Thread(target=do_attest)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted([results["split"][0], results["attest"][0]])
    _, detail = call(f"/api/cages/{cage_id}", admin)
    _, recon = call(f"/api/reconcile/{code}", admin)
    _, cont = call("/api/contention", admin)
    _, audit = call("/api/audit?limit=120", admin)
    drift = [a for a in audit["items"] if a["action"] in ("cage_closure_drift", "split_attest_contention")]

    out = {
        "split_http": results["split"][0],
        "attest_http": results["attest"][0],
        "codes": codes,
        "original_status": detail["cage"]["status"],
        "original_available": detail["cage"]["available"],
        "attestations_on_original": len(detail["attestations"]),
        "split_legs": len(detail["split_legs"]),
        "reconcile_balanced": recon["balanced"],
        "dual_track": recon["dual_track"],
        "contention_rows": [c for c in cont["items"] if c["lock_key"] == f"cage:{cage_id}"],
        "race_or_drift_audits": [json.loads(d["payload"] or "{}") for d in drift][:4],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    ok = codes == [200, 409]
    if out["split_http"] == 200:
        ok = ok and out["original_status"] == "已拆分" and out["attestations_on_original"] == 0
        ok = ok and out["split_legs"] == 2 and out["dual_track"] is False
    else:
        ok = ok and out["original_status"] == "核验通过" and out["split_legs"] == 0
        ok = ok and out["dual_track"] is False
    ok = ok and (len(out["contention_rows"]) >= 1 or len(out["race_or_drift_audits"]) >= 1)
    print("VERDICT:", "SINGLE_WINNER_OK" if ok else "VIOLATION")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
