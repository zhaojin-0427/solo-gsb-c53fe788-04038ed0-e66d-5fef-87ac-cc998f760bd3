from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..db import read_conn, write_tx
from ..deps import current_user
from ..security import create_session, delete_session, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn):
    with write_tx() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (body.username.strip(),)
        ).fetchone()
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = create_session(conn, user["id"])
    return {"token": token,
            "user": {"username": user["username"], "role": user["role"]}}


@router.post("/logout")
def logout(user: dict = Depends(current_user), authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    with read_conn() as conn:
        delete_session(conn, token)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "role": user["role"]}
