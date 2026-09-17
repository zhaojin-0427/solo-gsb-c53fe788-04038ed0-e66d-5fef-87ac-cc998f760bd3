"""服务层冒烟测试：仅依赖标准库，直接测试业务逻辑（不启动 Web 服务）。

运行：python3 tests/smoke_test.py
覆盖：登记幂等、交接链、幂等重放、持有人校验、并发无双链、温度导入去重、
      超限冻结、授权处置解冻、保管链/审计链验证、重启后状态一致。
"""
import os
import sys
import tempfile
import threading

# 独立临时数据库，避免污染真实数据
_tmpdir = tempfile.mkdtemp(prefix="custody-test-")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import db, services  # noqa: E402
from app.services import ServiceError  # noqa: E402

PASSED = 0


def check(name, cond, extra=""):
    global PASSED
    assert cond, f"FAIL: {name} {extra}"
    PASSED += 1
    print(f"  ok - {name}")


def expect_error(name, status, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ServiceError as e:
        check(name, e.status_code == status, f"(status={e.status_code}, detail={e.detail})")
        return e
    raise AssertionError(f"FAIL: {name} 未抛出预期的 ServiceError({status})")


def main():
    db.init_db()

    print("[1] 批次与样本登记")
    services.create_batch("staff1", "B-001", "测试批次", 2.0, 8.0)
    expect_error("重复批次号 → 409", 409, services.create_batch, "staff1", "B-001", "x", 2.0, 8.0)
    expect_error("阈值倒置 → 400", 400, services.create_batch, "staff1", "B-002", "x", 8.0, 2.0)

    r = services.register_samples("staff1", "B-001", ["S-1", "S-2", "S-3"], "张倩", "接收室")
    check("登记 3 个样本", r["added"] == ["S-1", "S-2", "S-3"])
    r2 = services.register_samples("staff1", "B-001", ["S-1", "S-4"], "张倩", "接收室")
    check("重复条码幂等跳过", r2["added"] == ["S-4"] and r2["duplicates"] == ["S-1"])

    info = services.get_sample_by_barcode("S-1")
    check("初始持有人为登记人", info["custody"]["current_holder"] == "张倩")
    check("创世事件 seq=1", info["custody"]["last_seq"] == 1)

    print("[2] 交接与前一持有人校验")
    t = services.create_transfer("staff1", "S-1", "张倩", "李牧", "冷藏库A", "key-001")
    check("交接成功 seq=2", t["event"]["seq"] == 2 and not t["replay"])
    check("状态推进", services.get_sample_by_barcode("S-1")["custody"]["current_holder"] == "李牧")

    e = expect_error("前一持有人不符 → 409", 409, services.create_transfer,
                     "staff2", "S-1", "张倩", "王五", "冷藏库A", "key-002")
    check("冲突已持久化", e.detail["conflict_id"] > 0)
    conflicts = services.list_exceptions()["conflicts"]
    check("冲突列表可见", any(c["kind"] == "holder_mismatch" and c["barcode"] == "S-1" for c in conflicts))

    expect_error("未登记条码 → 404", 404, services.create_transfer,
                 "staff1", "S-XXX", "张倩", "王五", "x", "key-003")

    print("[3] 幂等：同 key 重放不产生双重链路")
    before = len(services.get_timeline(info["sample"]["id"]))
    replay = services.create_transfer("staff1", "S-1", "李牧", "王五", "冷藏库B", "key-dup")
    check("首次提交成功", not replay["replay"] and replay["event"]["seq"] == 3)
    replay2 = services.create_transfer("staff1", "S-1", "李牧", "王五", "冷藏库B", "key-dup")
    check("同 key 重放", replay2["replay"] and replay2["event"]["seq"] == 3)
    after = len(services.get_timeline(info["sample"]["id"]))
    check("链长度不变", after == before + 1)

    print("[4] 并发：多线程同一样本交接，只允许一个延长链")
    results = {"ok": 0, "conflict": 0, "replay": 0}
    lock = threading.Lock()

    def worker(i):
        try:
            r = services.create_transfer("staff1", "S-2", "张倩", f"持有人{i}", "地点", f"race-{i}")
            with lock:
                results["replay" if r["replay"] else "ok"] += 1
        except ServiceError as e:
            with lock:
                if e.status_code == 409:
                    results["conflict"] += 1
                else:
                    raise

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for th in threads: th.start()
    for th in threads: th.join()
    s2 = services.get_sample_by_barcode("S-2")
    check("恰好 1 个交接成功", results["ok"] == 1, str(results))
    check("其余 9 个均为冲突", results["conflict"] == 9, str(results))
    check("链只前进 1 环", s2["custody"]["last_seq"] == 2)
    check("无双重链路", len(services.get_timeline(s2["sample"]["id"])) == 2)

    print("[5] 并发：同 key 并发提交全部收敛为同一事件")
    same_key_results = []
    lock2 = threading.Lock()

    def worker_same_key(i):
        try:
            r = services.create_transfer("staff1", "S-3", "张倩", "同一人", "地点", "same-key")
            with lock2:
                same_key_results.append(r)
        except ServiceError:
            pass

    threads = [threading.Thread(target=worker_same_key, args=(i,)) for i in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()
    seqs = {r["event"]["seq"] for r in same_key_results}
    check("同 key 并发全部返回同一环", seqs == {2}, str(seqs))
    s3 = services.get_sample_by_barcode("S-3")
    check("链只前进 1 环", s3["custody"]["last_seq"] == 2)

    print("[6] 温度导入 → 超限事件 → 冻结")
    imp = services.import_temperatures("staff1", "B-001", [
        {"recorded_at": "2026-09-17T08:00:00+00:00", "temperature": 4.0, "barcode": "S-1"},
        {"recorded_at": "2026-09-17T09:00:00+00:00", "temperature": 12.5, "barcode": "S-1"},
        {"recorded_at": "2026-09-17T09:00:00+00:00", "temperature": 5.0},
        {"recorded_at": "2026-09-17T10:00:00+00:00", "temperature": 6.0, "barcode": "UNKNOWN"},
    ])
    check("接受 3 行", imp["accepted"] == 3, str(imp))
    check("拒绝未登记条码", imp["rejected"] == 1)
    check("生成 1 个超限事件", len(imp["excursions_created"]) == 1)

    imp2 = services.import_temperatures("staff1", "B-001", [
        {"recorded_at": "2026-09-17T08:00:00+00:00", "temperature": 4.0, "barcode": "S-1"},
        {"recorded_at": "2026-09-17T09:00:00+00:00", "temperature": 12.5, "barcode": "S-1"},
    ])
    check("重复导入全部去重", imp2["duplicates"] == 2 and imp2["accepted"] == 0)
    check("不产生新超限事件", len(imp2["excursions_created"]) == 0)

    s1 = services.get_sample_by_barcode("S-1")
    check("S-1 已冻结", s1["frozen"])
    expect_error("冻结期交接 → 409", 409, services.create_transfer,
                 "staff1", "S-1", "王五", "赵六", "地点", "key-frozen")
    check("S-2 未受影响", not services.get_sample_by_barcode("S-2")["frozen"])

    print("[7] 授权处置 → 解冻")
    exc_id = imp["excursions_created"][0]
    expect_error("空理由 → 400", 400, services.dispose_excursion, "lead1", exc_id, "release", "")
    expect_error("非法动作 → 400", 400, services.dispose_excursion, "lead1", exc_id, "noop", "理由")
    d = services.dispose_excursion("lead1", exc_id, "release", "经评估偏差 30 分钟内，样本未受影响")
    check("解除隔离成功", d["status"] == "released")
    expect_error("重复处置 → 409", 409, services.dispose_excursion, "lead1", exc_id, "correct", "x")
    check("解冻后可交接", not services.get_sample_by_barcode("S-1")["frozen"])
    t2 = services.create_transfer("staff1", "S-1", "王五", "赵六", "冷藏库C", "key-after-release")
    check("解冻后交接成功", t2["event"]["seq"] == 4)

    print("[8] 冲突确认")
    open_conflicts = services.list_exceptions()["conflicts"]
    cid = open_conflicts[0]["id"]
    services.acknowledge_conflict("lead1", cid, "已联系双方核实")
    expect_error("重复确认 → 409", 409, services.acknowledge_conflict, "lead1", cid, "x")

    print("[9] 可验证性")
    v = services.verify_sample_chain(s1["sample"]["id"])
    check("S-1 保管链完整", v["ok"] and v["checked"] == 4, str(v))
    va = services.verify_audit_chain()
    check("审计链完整", va["ok"] and va["checked"] > 0, str(va))

    print("[10] 模拟重启（关闭连接后重开）状态一致")
    db_path = os.environ["DB_PATH"]
    # 所有连接均已关闭（每次操作独立连接），直接重新初始化并读取
    db.init_db()
    s1r = services.get_sample_by_barcode("S-1")
    check("重启后持有人一致", s1r["custody"]["current_holder"] == "赵六")
    check("重启后链长一致", len(services.get_timeline(s1["sample"]["id"])) == 4)
    check("重启后冲突仍在", len(services.list_exceptions()["conflicts"]) >= 1)
    check("重启后审计链仍完整", services.verify_audit_chain()["ok"])
    check("重启后幂等键仍去重",
          services.create_transfer("staff1", "S-1", "王五", "赵六", "冷藏库C", "key-after-release")["replay"])

    print(f"\n全部通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
