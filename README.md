# 保证金笼

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（≥2000 行像产品）。  
> FastAPI + Vue 3 + SQLite；`docker compose up -d --build` 一键起。  
> **禁止同构换皮**：主链路不是「占位→确认入册」也不是「入轨→裁定赔付」也不是「业务窗裁切物化」。

## 骨架一句话

保证金先入笼冻结，履约核验通过才解笼拨付，违约进罚没册，笼代与拨付罚没对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名 | 保证金笼 |
| 目录 | `11-bondcage` |
| 接口 | http://127.0.0.1:11337 |
| 库 | `data/app.db` |
| 账号 | admin / clerk / viewer ，密码均为 123456（viewer 只读） |

## 厚度

| 项 | 内容 |
|----|------|
| 业务 LOC | **≥2900**（py+前端，不含 vue.global / Docker / README；自检约 2984） |
| 后端模块 | db / auth / locks / bonds_svc / cages_svc / attest_svc / release_svc / forfeit_svc / reconcile_svc / audit_svc / topup_svc / split_svc |
| 前端视图 | 保证金户 / 入笼冻结 / 履约核验 / 解笼拨付 / 罚没册 / 对账互证 / 审计争用 / 增补拆笼（8 页） |
| 空桩 | topup（增补入笼）——前后端均为空桩；split（拆笼分拨）已落地 |

## 原笼关闭口径（纪要锁定）

拆笼成功后原笼命运只能落一路，两条路径互斥，当日不翻案：

- **甲 · 整笔切分**：切分事务成功 ⇒ 原笼号标「已拆分」、可用归零、只读；
  核验 / 解笼 / 罚没 / 增补 / 再拆 写口一律 **409 零增拒收**，并落「原笼关闭偏航」审计。
  子笼 `-L` / `-R` 为生效同义务笼，可各自核验、解笼，金额守恒。
- **乙 · 影子子笼**：原笼保持活跃态、自身核验/解笼轨照旧；子笼晋升行**零改回退**，
  只留 `split_legs` 查询快照（`status=快照`、无 `child_cage_id`），**不入拨付**。
- 同一原笼第二次拆笼或换路 = 翻案，**409 拒写**并落偏航审计。
- 对账口径：`可用 + 已拨付 + 已罚没 + 拆出子笼 = 笼总额`；已拆分原笼若仍出现拨付/罚没，
  对账页标「双轨」。

并发：拆笼与核验争同一把 `cage:{id}` 锁，后到只记争用，不写业务。

## 主链路

开户入金 → 按合约入笼冻结（笼代）→ 履约核验通过 → 解笼拨付到收款方 / 违约罚没 → 笼代余额与拨付+罚没对账

## 互斥切口候选

- 解笼失败整笔回滚 vs 影子拨付后晋升
- 罚没立即入册 vs 影子罚没待复核晋升
- 增补同笼扩代 vs 新笼桥接双写
- 拆笼整笔事务切分 vs 影子子笼后晋升

## 启动

```powershell
cd projects/11-bondcage
docker compose up -d --build
```

浏览器：http://127.0.0.1:11337/

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10608
python -m pytest -q
```

本机文件库走查（先拆笼、再对原笼核验）：

```powershell
python scripts/local_db_walkthrough.py   # 终态须输出 VERDICT: SINGLE_TRACK_OK
python scripts/contention_repro.py       # 须输出 VERDICT: SINGLE_WINNER_OK（服务已起）
```
