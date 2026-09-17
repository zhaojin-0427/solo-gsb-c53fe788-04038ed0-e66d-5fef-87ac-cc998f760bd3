"""FastAPI 依赖：会话认证与角色校验。"""
from fastapi import Depends, Header, HTTPException

from .db import read_conn
from .security import get_user_by_token


def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少令牌")
    token = authorization[len("Bearer "):].strip()
    with read_conn() as conn:
        user = get_user_by_token(conn, token)
    if not user:
        raise HTTPException(status_code=401, detail="会话无效或已过期，请重新登录")
    return dict(user)


def require_approver(user: dict = Depends(current_user)):
    if user["role"] not in ("approver", "admin"):
        raise HTTPException(status_code=403, detail="需要授权人员（approver/admin）权限")
    return user
