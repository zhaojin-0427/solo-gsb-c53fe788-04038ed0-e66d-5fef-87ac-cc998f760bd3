"""哈希链计算：保管链与审计链均为 SHA-256 前向链接，可离线验证。"""
import hashlib

GENESIS = "GENESIS"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def custody_hash(prev_hash: str, sample_id: int, seq: int, from_holder: str,
                 to_holder: str, location: str, scanned_at: str,
                 actor: str, idempotency_key: str) -> str:
    parts = [prev_hash, str(sample_id), str(seq), from_holder, to_holder,
             location, scanned_at, actor, idempotency_key]
    return sha256_text("|".join(parts))


def audit_hash(prev_hash: str, ts: str, actor: str, action: str,
               entity: str, entity_id: str, detail_json: str) -> str:
    return sha256_text("|".join([prev_hash, ts, actor, action, entity, entity_id, detail_json]))
