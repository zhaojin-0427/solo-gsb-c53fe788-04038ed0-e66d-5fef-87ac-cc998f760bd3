#!/usr/bin/env python3
"""端到端冒烟测试：覆盖交接链路、幂等重放、并发冲突、超限冻结、解除隔离、审计链。

用法：先启动服务（uvicorn backend.app.main:app），再执行
    python3 backend/tests/smoke_test.py [BASE_URL]
注意：测试会使用随机批次编码，可重复运行。
"""
from __future__ import annotations

import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RUN = uuid.uuid4().hex[:8]
BATCH = f"SMOKE-{RUN}"
S1, S2 = f"SC-{RUN}-1", f"SC-{RUN}-2"

tokens: dict[str, str] = {}
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    assert cond, f"❌ {name} 失败 {detail}"
    passed += 1
    print(f"✔ {name}")


def login(user: str, password: str) -> str:
    r = requests.post(f"{BASE}/api/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def hdr(user: str) -> dict:
    return {"Authorization": f"Bearer {tokens[user]}"}


def handover(barcode: str, frm: str, to: str, key: str, user: str = "alice"):
    return requests.post(f"{BASE}/api/handovers", headers=hdr(user), json={
        "barcode": barcode, "from_holder": frm, "to_holder": to,
        "location": "冷库A", "idempotency_key": key,
    })


def main() -> None:
    tokens["alice"] = login("alice", "alice123")
    tokens["bob"] = login("bob", "bob123")
    tokens["carol"] = login("carol", "carol123")
    check("登录三种角色", True)

    # 未认证拒绝
    r = requests.get(f"{BASE}/api/batches")
    check("未登录访问被拒绝(401)", r.status_code == 401)

    # 创建批次 + 登记条码
    r = requests.post(f"{BASE}/api/batches", headers=hdr("alice"), json={
        "batch_code": BATCH, "name": "冒烟测试批次", "temp_min": 2.0, "temp_max": 8.0,
    })
    check("创建批次", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/batches/{BATCH}/samples", headers=hdr("alice"),
                      json={"barcodes": [S1, S2, S1]})
    body = r.json()
    check("登记条码（重复条码幂等跳过）",
          r.status_code == 201 and body["added"] == [S1, S2] and body["existing"] == [S1], r.text)

    # 创世链：登记人为第一任持有人
    st = requests.get(f"{BASE}/api/samples/{S1}", headers=hdr("alice")).json()
    check("创世链当前持有人为登记人", st["current_holder"] == "alice" and st["seq"] == 0, str(st))

    # 正常交接 alice -> bob
    key1 = f"key-{RUN}-1"
    r = handover(S1, "alice", "bob", key1)
    check("交接 alice→bob", r.status_code == 200 and r.json()["replay"] is False, r.text)
    link_id = r.json()["link"]["id"]

    # 幂等重放：同 key 重复提交
    r = handover(S1, "alice", "bob", key1)
    check("同幂等键重放返回原记录", r.status_code == 200 and r.json()["replay"] is True
          and r.json()["link"]["id"] == link_id, r.text)

    # 前一持有人校验
    r = handover(S1, "alice", "carol", f"key-{RUN}-bad")
    check("前一持有人校验失败(409)", r.status_code == 409
          and r.json()["detail"]["code"] == "HOLDER_MISMATCH", r.text)

    # 并发：10 个不同 key 同时交接同一样本 —— 恰好 1 个成功，其余 409
    keys = [f"key-{RUN}-race-{i}" for i in range(10)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda k: handover(S1, "bob", "carol", k, user="bob"), keys))
    oks = [x for x in results if x.status_code == 200 and not x.json()["replay"]]
    conflicts = [x for x in results if x.status_code == 409]
    check("并发不同 key：恰好 1 个成功", len(oks) == 1 and len(conflicts) == 9,
          f"ok={len(oks)} conflict={len(conflicts)}")

    # 并发：10 个相同 key —— 全部 200，链路只增加 1 环
    key2 = f"key-{RUN}-2"
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: handover(S1, "carol", "alice", key2, user="bob"), range(10)))
    check("并发同 key：全部返回成功", all(x.status_code == 200 for x in results))
    ids = {x.json()["link"]["id"] for x in results}
    check("并发同 key：指向同一条链路记录", len(ids) == 1, str(ids))
    tl = requests.get(f"{BASE}/api/samples/{S1}/timeline", headers=hdr("alice")).json()
    seqs = [l["seq"] for l in tl["links"]]
    check("链路无双重环节（seq 连续无重复）", seqs == list(range(len(seqs))), str(seqs))
    check("链长符合预期（创世+3次交接）", len(seqs) == 4, str(seqs))

    # 温度导入：一条超限 -> 事件 + 冻结
    imp = {"batch_code": BATCH, "import_id": f"imp-{RUN}", "readings": [
        {"barcode": S1, "temp": 5.0, "recorded_at": "2026-09-17T08:00:00Z"},
        {"barcode": S1, "temp": 10.5, "recorded_at": "2026-09-17T09:00:00Z"},
    ]}
    r = requests.post(f"{BASE}/api/temperature/import", headers=hdr("alice"), json=imp)
    body = r.json()
    check("温度导入并生成超限事件", r.status_code == 200 and body["inserted"] == 2
          and body["events_generated"] == 1 and body["frozen"] is True, r.text)

    # 重复导入：全部幂等跳过，无重复事件
    r = requests.post(f"{BASE}/api/temperature/import", headers=hdr("alice"), json=imp)
    body = r.json()
    check("重复导入幂等跳过", body["inserted"] == 0 and body["skipped"] == 2
          and body["events_generated"] == 0, r.text)

    # 冻结后交接被拒绝
    r = handover(S1, "alice", "bob", f"key-{RUN}-frozen")
    check("冻结批次交接被拒绝(409)", r.status_code == 409
          and r.json()["detail"]["code"] == "BATCH_FROZEN", r.text)

    # 员工无权解除隔离
    ev_id = requests.get(f"{BASE}/api/events?status=OPEN", headers=hdr("alice")).json()["events"][0]["id"]
    r = requests.post(f"{BASE}/api/events/{ev_id}/release", headers=hdr("alice"),
                      json={"reason": "尝试越权"})
    check("员工解除隔离被拒绝(403)", r.status_code == 403, r.text)

    # 主管纠正记录 + 解除隔离（带理由）
    r = requests.post(f"{BASE}/api/events/{ev_id}/corrections", headers=hdr("carol"),
                      json={"reason": "已校准冷库温度探头"})
    check("主管登记纠正事件", r.status_code == 201, r.text)
    r = requests.post(f"{BASE}/api/events/{ev_id}/release", headers=hdr("carol"),
                      json={"reason": "确认偏差为探头故障，样本未实际超限"})
    check("主管解除隔离并解冻批次", r.status_code == 200 and r.json()["unfrozen"] is True, r.text)
    r = requests.post(f"{BASE}/api/events/{ev_id}/release", headers=hdr("carol"),
                      json={"reason": "重复解除"})
    check("重复解除被拒绝(409)", r.status_code == 409, r.text)

    # 解冻后交接恢复
    r = handover(S1, "alice", "bob", f"key-{RUN}-3")
    check("解冻后交接恢复", r.status_code == 200 and r.json()["replay"] is False, r.text)

    # 链路 / 审计链校验
    v = requests.get(f"{BASE}/api/samples/{S1}/verify", headers=hdr("alice")).json()
    check("保管链路哈希校验通过", v["valid"] is True, str(v))
    v = requests.get(f"{BASE}/api/audit/verify", headers=hdr("alice")).json()
    check("审计链哈希校验通过", v["valid"] is True, str(v))

    # 审计导出
    r = requests.get(f"{BASE}/api/audit/export?format=csv", headers=hdr("alice"))
    check("审计导出 CSV", r.status_code == 200 and "HANDOVER" in r.text, r.text[:100])
    r = requests.get(f"{BASE}/api/audit/export?format=json", headers=hdr("alice"))
    check("审计导出 JSON", r.status_code == 200 and isinstance(r.json(), list))

    print(f"\n全部 {passed} 项检查通过 ✅")


if __name__ == "__main__":
    main()
