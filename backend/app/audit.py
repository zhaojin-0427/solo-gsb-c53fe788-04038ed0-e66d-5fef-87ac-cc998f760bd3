"""追加式审计日志：每条记录链接前一条的哈希，构成可验证的全局链。"""
import json

from .hashing import GENESIS, audit_hash
from .util import now_iso


def write_audit(conn, actor: str, action: str, entity: str, entity_id, detail: dict) -> int:
    """在调用方的事务内追加一条审计记录（随业务操作同生共死）。"""
    ts = now_iso()
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS
    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    entity_id = str(entity_id)
    h = audit_hash(prev_hash, ts, actor, action, entity, entity_id, detail_json)
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor, action, entity, entity_id, detail_json, prev_hash, h),
    )
    return cur.lastrowid
