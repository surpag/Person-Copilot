"""认证相关 API。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.deps import get_current_admin, get_current_user
from auth.schema import init_auth_tables
from auth.service import (
    create_invite,
    create_jwt,
    create_user,
    get_user_by_email,
    get_user_by_id,
    list_invites,
    update_last_login,
    use_invite,
    verify_password,
)
from api.pool import DEFAULT_DB_PATH
from session.schema import connect as schema_connect

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    invite_code: str
    nickname: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    nickname: str
    avatar_path: str | None = None
    is_admin: int = 0


def _user_out(u: dict) -> UserOut:
    return UserOut(
        id=u["id"],
        email=u["email"],
        nickname=u.get("nickname") or "",
        avatar_path=u.get("avatar_path"),
        is_admin=u.get("is_admin", 0),
    )


async def _open_db():
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    return db


@router.post("/register")
async def register(req: RegisterRequest):
    db = await _open_db()
    try:
        try:
            user_id = await create_user(
                db, req.email, req.password, req.nickname
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        ok = await use_invite(db, req.invite_code, user_id)
        if not ok:
            await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await db.commit()
            raise HTTPException(status_code=400, detail="邀请码无效或已使用")

        user = await get_user_by_id(db, user_id)
    finally:
        await db.close()

    token = create_jwt(user_id)
    return {"token": token, "user": _user_out(user)}


@router.post("/login")
async def login(req: LoginRequest):
    db = await _open_db()
    try:
        user = await get_user_by_email(db, req.email)
        if not user or not verify_password(req.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        await update_last_login(db, user["id"])
    finally:
        await db.close()

    token = create_jwt(user["id"])
    return {"token": token, "user": _user_out(user)}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return _user_out(user)


@router.get("/invites")
async def list_invites_admin(admin: dict = Depends(get_current_admin)):
    db = await _open_db()
    try:
        items = await list_invites(db)
    finally:
        await db.close()
    return {"invites": items}


@router.post("/invites")
async def create_invite_admin(admin: dict = Depends(get_current_admin)):
    db = await _open_db()
    try:
        code = await create_invite(db, created_by=admin["id"])
    finally:
        await db.close()
    return {"code": code}
