"""应用配置：全部支持环境变量覆盖，默认值仅用于本地开发。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_USERS = {
    "alice": {"password": "alice123", "role": "staff"},
    "bob": {"password": "bob123", "role": "staff"},
    "carol": {"password": "carol123", "role": "supervisor"},
}


def _load_users() -> dict:
    raw = (os.environ.get("APP_USERS") or "").strip()
    if raw:
        users = json.loads(raw)
        if not isinstance(users, dict) or not users:
            raise ValueError("APP_USERS 必须是非空 JSON 对象")
        return users
    return DEFAULT_USERS


class Settings:
    def __init__(self) -> None:
        self.db_path = os.environ.get("APP_DB_PATH", str(ROOT / "data" / "app.db"))
        self.static_dir = os.environ.get("APP_STATIC_DIR", str(ROOT / "frontend"))
        self.secret = os.environ.get("APP_SECRET", "dev-only-secret-change-me")
        self.token_ttl_minutes = int(os.environ.get("APP_TOKEN_TTL_MINUTES", "720"))
        self.users = _load_users()


settings = Settings()
