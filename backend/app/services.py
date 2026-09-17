"""业务逻辑层：不依赖 Web 框架，全部通过 db.write_tx/read_conn 操作 SQLite。

关键保证：
- 交接幂等：客户端为每次交接生成 idempotency_key，重复/重试提交返回首次结果（replay）；
- 并发安全：写事务 BEGIN IMMEDIATE 串行化，custody_events(sample_id, seq) 唯一约束兜底，
  同一时刻只有一个交接能延长链路，绝不产生双重链路；
- 拒绝也留痕：持有人校验失败、冻结期交接等冲突在独立提交的事务中持久化到 conflicts；
- 超限冻结：存在 open 状态超限事件的样本（或整批）禁止交接，直到授权人员处置。
"""
import sqlite3

from .audit import write_audit
from .db import read_conn, write_tx
from .hashing import GENESIS, custody_hash, audit_hash
from .util import now_iso

GENESIS_HOLDER = "登记"


class ServiceError(Exception):
    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------- 批次与样本

def create_batch(actor: str, code: str, name: str, temp_min: float, temp_max: float) -> dict:
    code = code.strip()
    name = name.strip()
    if not code or not name:
        raise ServiceError(400, "批次号与名称不能为空")
    if temp_min >= temp_max:
        raise ServiceError(400, "温度下限必须小于上限")
    with write_tx() as conn:
        existing = conn.execute("SELECT id FROM batches WHERE code = ?", (code,)).fetchone()
        if existing:
            raise ServiceError(409, f"批次 {code} 已存在")
        cur = conn.execute(
            "INSERT INTO batches (code, name, temp_min, temp_max, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (code, name, temp_min, temp_max, actor, now_iso()),
        )
        write_audit(conn, actor, "batch.created", "batch", code,
                    {"name": name, "temp_min": temp_min, "temp_max": temp_max})
        return {"id": cur.lastrowid, "code": code, "name": name,
                "temp_min": temp_min, "temp_max": temp_max}


def list_batches() -> list:
    with read_conn() as conn:
        rows = conn.execute(
            """SELECT b.*, (SELECT COUNT(*) FROM samples s WHERE s.batch_id = b.id) AS sample_count,
                      (SELECT COUNT(*) FROM excursions e WHERE e.batch_id = b.id AND e.status = 'open') AS open_excursions
               FROM batches b ORDER BY b.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_batch(code: str) -> dict:
    with read_conn() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE code = ?", (code,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次 {code} 不存在")
        samples = conn.execute(
            "SELECT id, barcode, created_at FROM samples WHERE batch_id = ? ORDER BY id", (batch["id"],)
        ).fetchall()
        result = dict(batch)
        result["samples"] = [dict(s) for s in samples]
        return result


def register_samples(actor: str, batch_code: str, barcodes: list,
                     initial_holder: str, location: str) -> dict:
    """批量登记样本条码。条码为天然幂等键：重复条码跳过并计入 duplicates。"""
    initial_holder = initial_holder.strip()
    location = location.strip()
    if not initial_holder or not location:
        raise ServiceError(400, "初始持有人与地点不能为空")
    added, duplicates = [], []
    with write_tx() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE code = ?", (batch_code,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次 {batch_code} 不存在")
        for raw in barcodes:
            barcode = str(raw).strip()
            if not barcode:
                continue
            if conn.execute("SELECT 1 FROM samples WHERE barcode = ?", (barcode,)).fetchone():
                duplicates.append(barcode)
                continue
            cur = conn.execute(
                "INSERT INTO samples (batch_id, barcode, created_at) VALUES (?,?,?)",
                (batch["id"], barcode, now_iso()),
            )
            sid = cur.lastrowid
            # 创世事件：seq=1，从"登记"到初始持有人，构成链起点
            key = f"genesis:{sid}"
            ts = now_iso()
            h = custody_hash(GENESIS, sid, 1, GENESIS_HOLDER, initial_holder, location, ts, actor, key)
            conn.execute(
                """INSERT INTO custody_events
                   (sample_id, seq, from_holder, to_holder, location, scanned_at, actor,
                    idempotency_key, prev_hash, hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, 1, GENESIS_HOLDER, initial_holder, location, ts, actor, key, GENESIS, h, ts),
            )
            conn.execute(
                "INSERT INTO custody_state (sample_id, current_holder, last_seq, last_hash, updated_at) VALUES (?,?,?,?,?)",
                (sid, initial_holder, 1, h, ts),
            )
            write_audit(conn, actor, "sample.registered", "sample", barcode,
                        {"batch": batch_code, "initial_holder": initial_holder, "location": location})
            added.append(barcode)
    return {"added": added, "duplicates": duplicates}


def _open_excursions(conn, sample_id: int, batch_id: int) -> list:
    return conn.execute(
        """SELECT id, temperature, temp_min, temp_max, created_at FROM excursions
           WHERE status = 'open' AND (sample_id = ? OR (sample_id = 0 AND batch_id = ?))
           ORDER BY id""",
        (sample_id, batch_id),
    ).fetchall()


def get_sample_by_barcode(barcode: str) -> dict:
    with read_conn() as conn:
        sample = conn.execute(
            """SELECT s.id, s.barcode, s.batch_id, b.code AS batch_code, b.name AS batch_name,
                      b.temp_min, b.temp_max
               FROM samples s JOIN batches b ON b.id = s.batch_id WHERE s.barcode = ?""",
            (barcode,),
        ).fetchone()
        if not sample:
            raise ServiceError(404, f"条码 {barcode} 未登记")
        state = conn.execute("SELECT * FROM custody_state WHERE sample_id = ?", (sample["id"],)).fetchone()
        open_exc = _open_excursions(conn, sample["id"], sample["batch_id"])
        recent = conn.execute(
            "SELECT * FROM custody_events WHERE sample_id = ? ORDER BY seq DESC LIMIT 5", (sample["id"],)
        ).fetchall()
        return {
            "sample": dict(sample),
            "custody": dict(state) if state else None,
            "frozen": len(open_exc) > 0,
            "open_excursions": [dict(e) for e in open_exc],
            "recent_events": [dict(e) for e in recent],
        }


# ---------------------------------------------------------------- 交接（核心）

def _record_conflict(conn, actor, sample_id, barcode, kind,
                     expected_holder, actual_holder, attempted_to, location, detail) -> int:
    cur = conn.execute(
        """INSERT INTO conflicts
           (sample_id, barcode, kind, expected_holder, actual_holder, attempted_to,
            location, actor, detail, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'open', ?)""",
        (sample_id, barcode, kind, expected_holder, actual_holder, attempted_to,
         location, actor, detail, now_iso()),
    )
    return cur.lastrowid


def _event_payload(conn, row) -> dict:
    state = conn.execute("SELECT * FROM custody_state WHERE sample_id = ?", (row["sample_id"],)).fetchone()
    barcode = conn.execute("SELECT barcode FROM samples WHERE id = ?", (row["sample_id"],)).fetchone()["barcode"]
    return {
        "event": dict(row),
        "barcode": barcode,
        "current_holder": state["current_holder"],
        "last_seq": state["last_seq"],
    }


def create_transfer(actor: str, barcode: str, expected_from_holder: str, to_holder: str,
                    location: str, idempotency_key: str, scanned_at: str | None = None) -> dict:
    """记录一次交接。

    返回 {"replay": bool, ...}；校验失败时先把冲突落库再抛 ServiceError(4xx)。
    """
    barcode = barcode.strip()
    to_holder = to_holder.strip()
    location = location.strip()
    expected_from_holder = expected_from_holder.strip()
    idempotency_key = idempotency_key.strip()
    if not all([barcode, to_holder, location, expected_from_holder, idempotency_key]):
        raise ServiceError(400, "条码、前一持有人、新持有人、地点、幂等键均为必填")
    scanned_at = (scanned_at or "").strip() or now_iso()

    outcome = None
    try:
        with write_tx() as conn:
            # 1) 幂等重放：同一 key 直接返回首次结果
            existing = conn.execute(
                "SELECT * FROM custody_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                outcome = ("ok", {"replay": True, **_event_payload(conn, existing)})
                continue_guard = False
            else:
                continue_guard = True

            if continue_guard:
                sample = conn.execute(
                    "SELECT s.id, s.barcode, s.batch_id FROM samples s WHERE s.barcode = ?",
                    (barcode,),
                ).fetchone()
                if not sample:
                    cid = _record_conflict(conn, actor, None, barcode, "unknown_sample",
                                           expected_from_holder, None, to_holder, location,
                                           "扫描的条码未在系统中登记")
                    write_audit(conn, actor, "transfer.rejected", "sample", barcode,
                                {"reason": "unknown_sample", "conflict_id": cid})
                    outcome = ("error", 404, {"message": f"条码 {barcode} 未登记", "conflict_id": cid})
                    continue_guard = False

            if continue_guard:
                state = conn.execute(
                    "SELECT * FROM custody_state WHERE sample_id = ?", (sample["id"],)
                ).fetchone()
                # 2) 冻结检查：存在未处置超限事件则禁止交接
                open_exc = _open_excursions(conn, sample["id"], sample["batch_id"])
                if open_exc:
                    ids = [e["id"] for e in open_exc]
                    cid = _record_conflict(conn, actor, sample["id"], barcode, "frozen",
                                           expected_from_holder, state["current_holder"],
                                           to_holder, location,
                                           f"存在 {len(ids)} 个未处置超限事件: {ids}")
                    write_audit(conn, actor, "transfer.rejected", "sample", barcode,
                                {"reason": "frozen", "excursion_ids": ids, "conflict_id": cid})
                    outcome = ("error", 409, {
                        "message": "样本已冻结：存在未处置的超限事件，请先完成处置",
                        "excursion_ids": ids, "conflict_id": cid})
                    continue_guard = False

            if continue_guard:
                # 3) 前一持有人校验
                if state["current_holder"] != expected_from_holder:
                    cid = _record_conflict(conn, actor, sample["id"], barcode, "holder_mismatch",
                                           expected_from_holder, state["current_holder"],
                                           to_holder, location,
                                           f"期望前一持有人 [{expected_from_holder}]，实际当前持有人 [{state['current_holder']}]")
                    write_audit(conn, actor, "transfer.rejected", "sample", barcode,
                                {"reason": "holder_mismatch", "expected": expected_from_holder,
                                 "actual": state["current_holder"], "conflict_id": cid})
                    outcome = ("error", 409, {
                        "message": "前一持有人校验失败",
                        "expected": expected_from_holder,
                        "actual": state["current_holder"],
                        "conflict_id": cid})
                    continue_guard = False

            if continue_guard:
                # 4) 追加链事件并推进状态（同事务）
                seq = state["last_seq"] + 1
                prev_hash = state["last_hash"]
                h = custody_hash(prev_hash, sample["id"], seq, state["current_holder"],
                                 to_holder, location, scanned_at, actor, idempotency_key)
                cur = conn.execute(
                    """INSERT INTO custody_events
                       (sample_id, seq, from_holder, to_holder, location, scanned_at, actor,
                        idempotency_key, prev_hash, hash, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (sample["id"], seq, state["current_holder"], to_holder, location,
                     scanned_at, actor, idempotency_key, prev_hash, h, now_iso()),
                )
                updated = conn.execute(
                    """UPDATE custody_state
                       SET current_holder = ?, last_seq = ?, last_hash = ?, updated_at = ?
                       WHERE sample_id = ? AND last_seq = ?""",
                    (to_holder, seq, h, now_iso(), sample["id"], state["last_seq"]),
                ).rowcount
                if updated != 1:
                    raise ServiceError(409, "并发冲突：样本状态已被其他交接更新，请刷新后重试")
                event = conn.execute("SELECT * FROM custody_events WHERE id = ?", (cur.lastrowid,)).fetchone()
                write_audit(conn, actor, "transfer.created", "sample", barcode,
                            {"seq": seq, "from": state["current_holder"], "to": to_holder,
                             "location": location, "hash": h})
                outcome = ("ok", {"replay": False, **_event_payload(conn, event)})
    except sqlite3.IntegrityError:
        # 唯一约束兜底（极端并发）：同 key 视为重放，否则提示重试
        with read_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM custody_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return {"replay": True, **_event_payload(conn, existing)}
        raise ServiceError(409, "并发冲突：请刷新后重试")

    kind, *rest = outcome
    if kind == "error":
        _, status, detail = outcome
        raise ServiceError(status, detail)
    return rest[0]


def get_timeline(sample_id: int) -> list:
    with read_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM custody_events WHERE sample_id = ? ORDER BY seq", (sample_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def verify_sample_chain(sample_id: int) -> dict:
    """重放整条保管链，校验序号连续性、前向链接与哈希值，并与当前状态对账。"""
    with read_conn() as conn:
        events = conn.execute(
            "SELECT * FROM custody_events WHERE sample_id = ? ORDER BY seq", (sample_id,)
        ).fetchall()
        state = conn.execute("SELECT * FROM custody_state WHERE sample_id = ?", (sample_id,)).fetchone()
    prev = GENESIS
    for expected_seq, e in enumerate(events, start=1):
        if e["seq"] != expected_seq:
            return {"ok": False, "broken_at": e["seq"], "reason": "链序号不连续，可能存在缺失事件"}
        if e["prev_hash"] != prev:
            return {"ok": False, "broken_at": e["seq"], "reason": "前向哈希链接断裂"}
        h = custody_hash(e["prev_hash"], e["sample_id"], e["seq"], e["from_holder"],
                         e["to_holder"], e["location"], e["scanned_at"], e["actor"],
                         e["idempotency_key"])
        if h != e["hash"]:
            return {"ok": False, "broken_at": e["seq"], "reason": "事件哈希校验失败，记录可能被篡改"}
        prev = h
    if state and state["last_hash"] != prev:
        return {"ok": False, "reason": "当前保管状态与链尾不一致", "checked": len(events)}
    return {"ok": True, "checked": len(events), "head": prev}


# ---------------------------------------------------------------- 温度导入与超限

def import_temperatures(actor: str, batch_code: str, readings: list, filename: str | None = None) -> dict:
    """导入温度读数并按批次阈值自动生成超限事件。

    读数以 (批次, 样本, 时间, 温度) 为天然幂等键，重复导入自动去重。
    """
    if not readings:
        raise ServiceError(400, "读数列表为空")
    with write_tx() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE code = ?", (batch_code,)).fetchone()
        if not batch:
            raise ServiceError(404, f"批次 {batch_code} 不存在")
        cur = conn.execute(
            "INSERT INTO temp_imports (batch_id, filename, imported_by, imported_at) VALUES (?,?,?,?)",
            (batch["id"], filename, actor, now_iso()),
        )
        import_id = cur.lastrowid
        accepted = duplicates = rejected = 0
        rejected_rows = []
        excursion_ids = []
        for idx, r in enumerate(readings, start=1):
            recorded_at = str(r.get("recorded_at") or "").strip()
            barcode = str(r.get("barcode") or "").strip()
            try:
                temperature = float(r.get("temperature"))
            except (TypeError, ValueError):
                rejected += 1
                rejected_rows.append({"row": idx, "reason": "温度不是有效数字"})
                continue
            if not recorded_at:
                rejected += 1
                rejected_rows.append({"row": idx, "reason": "缺少记录时间"})
                continue
            sample_id = 0
            if barcode:
                s = conn.execute(
                    "SELECT id, batch_id FROM samples WHERE barcode = ?", (barcode,)
                ).fetchone()
                if not s:
                    rejected += 1
                    rejected_rows.append({"row": idx, "reason": f"条码 {barcode} 未登记"})
                    continue
                if s["batch_id"] != batch["id"]:
                    rejected += 1
                    rejected_rows.append({"row": idx, "reason": f"条码 {barcode} 不属于批次 {batch_code}"})
                    continue
                sample_id = s["id"]
            try:
                cur = conn.execute(
                    "INSERT INTO temp_readings (import_id, batch_id, sample_id, recorded_at, temperature) VALUES (?,?,?,?,?)",
                    (import_id, batch["id"], sample_id, recorded_at, temperature),
                )
            except sqlite3.IntegrityError:
                duplicates += 1
                continue
            accepted += 1
            if temperature < batch["temp_min"] or temperature > batch["temp_max"]:
                cur = conn.execute(
                    """INSERT INTO excursions
                       (reading_id, batch_id, sample_id, temperature, temp_min, temp_max, status, created_at)
                       VALUES (?,?,?,?,?,?, 'open', ?)""",
                    (cur.lastrowid, batch["id"], sample_id, temperature,
                     batch["temp_min"], batch["temp_max"], now_iso()),
                )
                excursion_ids.append(cur.lastrowid)
                write_audit(conn, actor, "excursion.created", "excursion", cur.lastrowid,
                            {"batch": batch_code, "sample_id": sample_id, "temperature": temperature,
                             "temp_min": batch["temp_min"], "temp_max": batch["temp_max"],
                             "recorded_at": recorded_at})
        conn.execute(
            "UPDATE temp_imports SET total = ?, accepted = ?, duplicates = ?, rejected = ? WHERE id = ?",
            (len(readings), accepted, duplicates, rejected, import_id),
        )
        write_audit(conn, actor, "temperature.imported", "batch", batch_code,
                    {"import_id": import_id, "total": len(readings), "accepted": accepted,
                     "duplicates": duplicates, "rejected": rejected, "excursions": excursion_ids})
    return {"import_id": import_id, "total": len(readings), "accepted": accepted,
            "duplicates": duplicates, "rejected": rejected,
            "rejected_rows": rejected_rows, "excursions_created": excursion_ids}


# ---------------------------------------------------------------- 异常与处置

def list_exceptions(include_resolved: bool = False) -> dict:
    with read_conn() as conn:
        where = "" if include_resolved else "WHERE e.status = 'open'"
        excursions = conn.execute(
            f"""SELECT e.id, e.status, e.temperature, e.temp_min, e.temp_max, e.created_at,
                       b.code AS batch_code, s.barcode, r.recorded_at
                FROM excursions e
                JOIN batches b ON b.id = e.batch_id
                LEFT JOIN samples s ON s.id = e.sample_id
                JOIN temp_readings r ON r.id = e.reading_id
                {where} ORDER BY e.id DESC"""
        ).fetchall()
        dispositions = conn.execute(
            "SELECT * FROM dispositions ORDER BY id"
        ).fetchall()
        disp_by_exc = {}
        for d in dispositions:
            disp_by_exc.setdefault(d["excursion_id"], []).append(dict(d))
        cwhere = "" if include_resolved else "WHERE status = 'open'"
        conflicts = conn.execute(
            f"SELECT * FROM conflicts {cwhere} ORDER BY id DESC"
        ).fetchall()
        return {
            "excursions": [{**dict(e), "dispositions": disp_by_exc.get(e["id"], [])} for e in excursions],
            "conflicts": [dict(c) for c in conflicts],
        }


def dispose_excursion(actor: str, excursion_id: int, action: str, reason: str) -> dict:
    """授权人员提交隔离解除(release)或纠正(correct)，必须带理由。"""
    reason = (reason or "").strip()
    if action not in ("release", "correct"):
        raise ServiceError(400, "action 必须为 release 或 correct")
    if not reason:
        raise ServiceError(400, "必须填写处置理由")
    with write_tx() as conn:
        exc = conn.execute("SELECT * FROM excursions WHERE id = ?", (excursion_id,)).fetchone()
        if not exc:
            raise ServiceError(404, f"超限事件 {excursion_id} 不存在")
        if exc["status"] != "open":
            raise ServiceError(409, {"message": "该事件已处置，请勿重复提交", "status": exc["status"]})
        new_status = "released" if action == "release" else "corrected"
        conn.execute(
            "INSERT INTO dispositions (excursion_id, action, reason, actor, created_at) VALUES (?,?,?,?,?)",
            (excursion_id, action, reason, actor, now_iso()),
        )
        conn.execute("UPDATE excursions SET status = ? WHERE id = ?", (new_status, excursion_id))
        write_audit(conn, actor, f"excursion.{action}", "excursion", excursion_id,
                    {"reason": reason, "new_status": new_status})
    return {"id": excursion_id, "status": new_status, "action": action, "reason": reason, "actor": actor}


def acknowledge_conflict(actor: str, conflict_id: int, note: str) -> dict:
    note = (note or "").strip()
    with write_tx() as conn:
        c = conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        if not c:
            raise ServiceError(404, f"冲突 {conflict_id} 不存在")
        if c["status"] != "open":
            raise ServiceError(409, {"message": "该冲突已处理", "status": c["status"]})
        conn.execute(
            "UPDATE conflicts SET status = 'acknowledged', resolved_by = ?, resolved_at = ?, resolution_note = ? WHERE id = ?",
            (actor, now_iso(), note, conflict_id),
        )
        write_audit(conn, actor, "conflict.acknowledged", "conflict", conflict_id, {"note": note})
    return {"id": conflict_id, "status": "acknowledged"}


# ---------------------------------------------------------------- 审计

def list_audit(limit: int = 200, offset: int = 0, action: str | None = None) -> list:
    limit = max(1, min(limit, 1000))
    with read_conn() as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (action, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]


def export_audit_rows() -> list:
    with read_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def verify_audit_chain() -> dict:
    with read_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    prev = GENESIS
    for row in rows:
        if row["prev_hash"] != prev:
            return {"ok": False, "broken_at": row["id"], "reason": "审计链前向链接断裂"}
        h = audit_hash(row["prev_hash"], row["ts"], row["actor"], row["action"],
                       row["entity"], row["entity_id"], row["detail"])
        if h != row["hash"]:
            return {"ok": False, "broken_at": row["id"], "reason": "审计记录哈希校验失败"}
        prev = h
    return {"ok": True, "checked": len(rows), "head": prev}
