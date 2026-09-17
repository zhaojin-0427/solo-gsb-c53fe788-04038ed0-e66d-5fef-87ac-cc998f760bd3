"""核心业务逻辑。所有函数都在 db.tx() 写事务内被调用（除 verify 外）。"""
from __future__ import annotations

import sqlite3
import uuid

from fastapi import HTTPException

from .schemas import BatchIn, HandoverIn, TempImportIn
from .util import canonical, now_iso, parse_dt, sha256_hex

GENESIS_HASH = "0" * 64


def err(status: int, code: str, message: str, **extra):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ---------------------------------------------------------------- 审计

def audit(conn, actor: str, action: str, entity: str, entity_id, payload: dict) -> str:
    """追加一条哈希链审计记录（与调用方同事务）。"""
    row = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (row["seq"] + 1) if row else 1
    prev = row["hash"] if row else GENESIS_HASH
    created_at = now_iso()
    payload_s = canonical(payload)
    digest = sha256_hex(canonical({
        "seq": seq, "actor": actor, "action": action, "entity": entity,
        "entity_id": str(entity_id), "payload": payload_s,
        "prev_hash": prev, "created_at": created_at,
    }))
    conn.execute(
        "INSERT INTO audit_log(seq, actor, action, entity, entity_id, payload, prev_hash, hash, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (seq, actor, action, entity, str(entity_id), payload_s, prev, digest, created_at),
    )
    return digest


# ---------------------------------------------------------------- 批次 / 样本

def create_batch(conn, data: BatchIn, actor: str) -> dict:
    if conn.execute("SELECT 1 FROM batches WHERE batch_code=?", (data.batch_code,)).fetchone():
        err(409, "BATCH_EXISTS", f"批次 {data.batch_code} 已存在")
    conn.execute(
        "INSERT INTO batches(batch_code, name, description, temp_min, temp_max, frozen, created_by, created_at)"
        " VALUES (?,?,?,?,?,0,?,?)",
        (data.batch_code, data.name, data.description, data.temp_min, data.temp_max, actor, now_iso()),
    )
    audit(conn, actor, "BATCH_CREATE", "batch", data.batch_code, data.model_dump())
    return get_batch(conn, data.batch_code)


def get_batch(conn, batch_code: str) -> dict:
    row = conn.execute(
        "SELECT b.*, "
        " (SELECT COUNT(*) FROM samples s WHERE s.batch_id=b.id) AS sample_count,"
        " (SELECT COUNT(*) FROM events e WHERE e.batch_id=b.id AND e.type='OUT_OF_RANGE' AND e.status='OPEN') AS open_events"
        " FROM batches b WHERE b.batch_code=?",
        (batch_code,),
    ).fetchone()
    if not row:
        err(404, "BATCH_NOT_FOUND", f"批次 {batch_code} 不存在")
    return dict(row)


def register_samples(conn, batch_code: str, barcodes: list[str], actor: str) -> dict:
    batch = get_batch(conn, batch_code)
    added, existing = [], []
    for barcode in barcodes:  # 逐条查库判定，请求内/跨请求的重复都会如实报告
        if conn.execute("SELECT 1 FROM samples WHERE barcode=?", (barcode,)).fetchone():
            if barcode not in existing:
                existing.append(barcode)
            continue
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO samples(barcode, batch_id, created_at) VALUES (?,?,?)",
            (barcode, batch["id"], ts),
        )
        sample_id = cur.lastrowid
        # 创世链路：登记人即第一任持有人，保证“前一持有人”校验从起点就成立
        key = f"genesis:{barcode}"
        fields = {
            "sample_id": sample_id, "seq": 0, "from_holder": "注册",
            "to_holder": actor, "location": "样本登记", "scanned_at": ts,
            "idempotency_key": key, "prev_hash": GENESIS_HASH,
        }
        digest = sha256_hex(canonical(fields))
        conn.execute(
            "INSERT INTO custody_links(sample_id, seq, from_holder, to_holder, location, scanned_at,"
            " idempotency_key, prev_hash, hash, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sample_id, 0, "注册", actor, "样本登记", ts, key, GENESIS_HASH, digest, actor, ts),
        )
        added.append(barcode)
    if added:
        audit(conn, actor, "SAMPLES_REGISTER", "batch", batch_code, {"barcodes": added})
    return {"added": added, "existing": existing}


# ---------------------------------------------------------------- 交接（核心幂等逻辑）

def _link_fields(row) -> dict:
    return {
        "sample_id": row["sample_id"], "seq": row["seq"],
        "from_holder": row["from_holder"], "to_holder": row["to_holder"],
        "location": row["location"], "scanned_at": row["scanned_at"],
        "idempotency_key": row["idempotency_key"], "prev_hash": row["prev_hash"],
    }


def handover(conn, data: HandoverIn, actor: str) -> tuple[dict, bool]:
    """登记一次交接。返回 (链路记录, 是否幂等重放)。

    幂等与并发保障：
    1. idempotency_key 唯一 —— 同一客户端重试直接返回原记录；
    2. UNIQUE(sample_id, seq) —— 不同 key 的并发提交只有第一个能占住序号；
    3. 写事务由 BEGIN IMMEDIATE + 进程写锁串行化，冲突方在锁内看到最新链顶。
    """
    replay = conn.execute(
        "SELECT * FROM custody_links WHERE idempotency_key=?", (data.idempotency_key,)
    ).fetchone()
    if replay:
        return dict(replay), True

    sample = conn.execute(
        "SELECT s.id AS sample_id, s.barcode, b.id AS batch_id, b.batch_code, b.frozen"
        " FROM samples s JOIN batches b ON b.id=s.batch_id WHERE s.barcode=?",
        (data.barcode,),
    ).fetchone()
    if not sample:
        err(404, "SAMPLE_NOT_FOUND", f"条码 {data.barcode} 未登记")
    if sample["frozen"]:
        err(409, "BATCH_FROZEN", f"批次 {sample['batch_code']} 已因超限被冻结，需授权人员解除隔离后才能交接",
            batch_code=sample["batch_code"])

    last = conn.execute(
        "SELECT * FROM custody_links WHERE sample_id=? ORDER BY seq DESC LIMIT 1",
        (sample["sample_id"],),
    ).fetchone()
    if not last:
        err(409, "CHAIN_BROKEN", "该样本缺少登记链路，无法交接")
    if last["to_holder"] != data.from_holder:
        err(409, "HOLDER_MISMATCH",
            f"前一持有人校验失败：当前持有人为「{last['to_holder']}」，而非「{data.from_holder}」",
            current_holder=last["to_holder"], current_seq=last["seq"])

    seq = last["seq"] + 1
    scanned_at = parse_dt(data.scanned_at)
    fields = {
        "sample_id": sample["sample_id"], "seq": seq,
        "from_holder": data.from_holder, "to_holder": data.to_holder,
        "location": data.location, "scanned_at": scanned_at,
        "idempotency_key": data.idempotency_key, "prev_hash": last["hash"],
    }
    digest = sha256_hex(canonical(fields))
    try:
        conn.execute(
            "INSERT INTO custody_links(sample_id, seq, from_holder, to_holder, location, scanned_at,"
            " idempotency_key, prev_hash, hash, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sample["sample_id"], seq, data.from_holder, data.to_holder, data.location,
             scanned_at, data.idempotency_key, last["hash"], digest, actor, now_iso()),
        )
    except sqlite3.IntegrityError:
        # 兜底：唯一约束冲突时优先按幂等重放处理
        replay = conn.execute(
            "SELECT * FROM custody_links WHERE idempotency_key=?", (data.idempotency_key,)
        ).fetchone()
        if replay:
            return dict(replay), True
        err(409, "CHAIN_CONFLICT", "并发交接冲突：链路已被其他提交更新，请刷新后重试")

    audit(conn, actor, "HANDOVER", "sample", data.barcode, {**fields, "hash": digest})
    link = conn.execute("SELECT * FROM custody_links WHERE idempotency_key=?",
                        (data.idempotency_key,)).fetchone()
    return dict(link), False


# ---------------------------------------------------------------- 温度导入 / 超限冻结

def import_temperatures(conn, data: TempImportIn, actor: str) -> dict:
    batch = get_batch(conn, data.batch_code)
    inserted, skipped, errors, new_events = 0, 0, [], []
    for idx, r in enumerate(data.readings, start=1):
        barcode = (r.barcode or "").strip()
        if barcode:
            owner = conn.execute(
                "SELECT batch_id FROM samples WHERE barcode=?", (barcode,)
            ).fetchone()
            if not owner:
                errors.append({"line": idx, "barcode": barcode, "reason": "条码未登记"})
                continue
            if owner["batch_id"] != batch["id"]:
                errors.append({"line": idx, "barcode": barcode, "reason": f"条码不属于批次 {data.batch_code}"})
                continue
        recorded_at = parse_dt(r.recorded_at)
        uid = sha256_hex(f"{data.batch_code}|{barcode}|{r.temp}|{recorded_at}")
        try:
            cur = conn.execute(
                "INSERT INTO temperature_readings(batch_id, barcode, temp, recorded_at, import_id, reading_uid, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (batch["id"], barcode, r.temp, recorded_at, data.import_id, uid, now_iso()),
            )
        except sqlite3.IntegrityError:
            skipped += 1  # 同一记录重复导入：幂等跳过
            continue
        inserted += 1
        if r.temp < batch["temp_min"] or r.temp > batch["temp_max"]:
            details = {
                "barcode": barcode, "temp": r.temp, "recorded_at": recorded_at,
                "temp_min": batch["temp_min"], "temp_max": batch["temp_max"],
            }
            # event_uid 由记录唯一标识派生，重复导入不会产生重复事件
            conn.execute(
                "INSERT OR IGNORE INTO events(event_uid, type, status, batch_id, reading_id, details, reason, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f"OOR:{uid}", "OUT_OF_RANGE", "OPEN", batch["id"], cur.lastrowid,
                 canonical(details), "", actor, now_iso()),
            )
            new_events.append({"barcode": barcode, "temp": r.temp, "recorded_at": recorded_at})
    if new_events:
        conn.execute("UPDATE batches SET frozen=1 WHERE id=?", (batch["id"],))
    audit(conn, actor, "TEMP_IMPORT", "batch", data.batch_code, {
        "import_id": data.import_id, "inserted": inserted, "skipped": skipped,
        "errors": errors, "out_of_range": new_events,
    })
    return {
        "inserted": inserted, "skipped": skipped, "errors": errors,
        "events_generated": len(new_events), "frozen": bool(new_events) or bool(batch["frozen"]),
    }


# ---------------------------------------------------------------- 异常处置

def release_event(conn, event_id: int, reason: str, actor: str) -> dict:
    ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        err(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")
    if ev["type"] != "OUT_OF_RANGE":
        err(409, "NOT_AN_EXCEPTION", "只能对超限异常执行解除隔离")
    if ev["status"] != "OPEN":
        err(409, "ALREADY_RESOLVED", "该异常已处置，请勿重复提交")
    try:
        conn.execute(
            "INSERT INTO events(event_uid, type, status, batch_id, parent_id, reading_id, details, reason, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"REL:{event_id}", "RELEASE", "CLOSED", ev["batch_id"], event_id,
             ev["reading_id"], "{}", reason, actor, now_iso()),
        )
    except sqlite3.IntegrityError:
        err(409, "ALREADY_RESOLVED", "该异常已处置，请勿重复提交")
    conn.execute("UPDATE events SET status='RESOLVED' WHERE id=?", (event_id,))
    open_cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE batch_id=? AND type='OUT_OF_RANGE' AND status='OPEN'",
        (ev["batch_id"],),
    ).fetchone()["c"]
    unfrozen = False
    if open_cnt == 0:
        conn.execute("UPDATE batches SET frozen=0 WHERE id=?", (ev["batch_id"],))
        unfrozen = True
    audit(conn, actor, "EVENT_RELEASE", "event", event_id,
          {"reason": reason, "batch_id": ev["batch_id"], "unfrozen": unfrozen})
    return {"unfrozen": unfrozen, "open_events": open_cnt}


def add_correction(conn, event_id: int, reason: str, actor: str) -> dict:
    ev = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not ev:
        err(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")
    uid = f"COR:{event_id}:{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO events(event_uid, type, status, batch_id, parent_id, reading_id, details, reason, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, "CORRECTION", "CLOSED", ev["batch_id"], event_id, ev["reading_id"],
         "{}", reason, actor, now_iso()),
    )
    audit(conn, actor, "EVENT_CORRECTION", "event", event_id, {"reason": reason})
    return {"event_uid": uid}


# ---------------------------------------------------------------- 校验（只读）

def verify_custody_chain(conn, barcode: str) -> dict:
    sample = conn.execute("SELECT id FROM samples WHERE barcode=?", (barcode,)).fetchone()
    if not sample:
        err(404, "SAMPLE_NOT_FOUND", f"条码 {barcode} 未登记")
    links = conn.execute(
        "SELECT * FROM custody_links WHERE sample_id=? ORDER BY seq", (sample["id"],)
    ).fetchall()
    prev = GENESIS_HASH
    for expect_seq, link in enumerate(links):
        if link["seq"] != expect_seq:
            return {"valid": False, "checked": expect_seq,
                    "message": f"链路序号断裂：期望 {expect_seq}，实际 {link['seq']}"}
        if link["prev_hash"] != prev:
            return {"valid": False, "checked": expect_seq,
                    "message": f"第 {link['seq']} 环 prev_hash 与上一环不匹配"}
        if sha256_hex(canonical(_link_fields(link))) != link["hash"]:
            return {"valid": False, "checked": expect_seq,
                    "message": f"第 {link['seq']} 环哈希校验失败，记录可能被篡改"}
        prev = link["hash"]
    return {"valid": True, "checked": len(links), "message": f"链路完整，共 {len(links)} 环"}


def verify_audit_chain(conn) -> dict:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    prev = GENESIS_HASH
    for row in rows:
        expect = sha256_hex(canonical({
            "seq": row["seq"], "actor": row["actor"], "action": row["action"],
            "entity": row["entity"], "entity_id": row["entity_id"],
            "payload": row["payload"], "prev_hash": row["prev_hash"],
            "created_at": row["created_at"],
        }))
        if row["prev_hash"] != prev or expect != row["hash"]:
            return {"valid": False, "checked": row["seq"],
                    "message": f"审计链在第 {row['seq']} 条校验失败"}
        prev = row["hash"]
    return {"valid": True, "checked": len(rows), "message": f"审计链完整，共 {len(rows)} 条"}
