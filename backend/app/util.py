from datetime import datetime, timezone


def now_iso() -> str:
    """UTC 当前时间，ISO 8601 格式（秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
