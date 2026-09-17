"""认证与授权：HMAC 签名的无状态令牌 + 角色校验。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Depends, Header, HTTPException

from .config import settings

ROLE_RANK = {"staff": 1, "supervisor": 2}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_password(username: str, password: str) -> str | None:
    """校验账号密码，成功返回角色，失败返回 None。"""
    entry = settings.users.get(username)
    if not entry:
        return None
    if not hmac.compare_digest(entry.get("password", ""), password):
        return None
    return entry.get("role", "staff")


def make_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": int(time.time()) + settings.token_ttl_minutes * 60,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def parse_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(settings.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    if payload.get("sub") not in settings.users:
        return None
    return payload


def get_current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "缺少登录令牌"})
    payload = parse_token(authorization[7:].strip())
    if not payload:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "令牌无效或已过期"})
    return {"username": payload["sub"], "role": payload.get("role", "staff")}


def require_role(min_role: str):
    """角色门槛：supervisor 拥有 staff 的全部权限。"""

    def dependency(user: dict = Depends(get_current_user)) -> dict:
        if ROLE_RANK.get(user["role"], 0) < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail={"code": "FORBIDDEN", "message": f"该操作需要 {min_role} 及以上权限"},
            )
        return user

    return dependency
