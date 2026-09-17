"""通用工具：规范化 JSON、哈希、时间。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def canonical(obj) -> str:
    """确定性 JSON 序列化，用于哈希链计算。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    """当前 UTC 时间，ISO 8601 格式（毫秒精度，Z 结尾）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_dt(value: str | None) -> str:
    """解析客户端时间；为空则取当前时间。naive 时间按 UTC 处理。"""
    if not value:
        return now_iso()
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"无法解析时间格式: {value!r}，请使用 ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
