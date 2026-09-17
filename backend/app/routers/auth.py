from fastapi import APIRouter, Depends

from ..schemas import LoginIn
from ..security import check_password, get_current_user, make_token
from ..services import err

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(data: LoginIn):
    role = check_password(data.username, data.password)
    if not role:
        err(401, "BAD_CREDENTIALS", "账号或密码错误")
    return {
        "token": make_token(data.username, role),
        "user": {"username": data.username, "role": role},
    }


@router.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}
