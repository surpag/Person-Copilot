"""账号设置：改昵称 / 改密码 / 传头像。"""
import os
import sys
import uuid as uuid_lib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from auth.deps import get_current_user
from auth.schema import init_auth_tables
from auth.service import hash_password, verify_password
from api.pool import DEFAULT_DB_PATH
from session.schema import connect as schema_connect

router = APIRouter(prefix="/account", tags=["account"])

# 头像目录（与主数据目录一致，支持 DATA_DIR 覆盖）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))
AVATAR_DIR = _DATA_DIR / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)


class UpdateProfileRequest(BaseModel):
    nickname: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.patch("/profile")
async def update_profile(
    req: UpdateProfileRequest, user: dict = Depends(get_current_user)
):
    nickname = (req.nickname or "").strip()
    if not nickname:
        return {"updated": False}
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    try:
        await db.execute(
            "UPDATE users SET nickname = ? WHERE id = ?", (nickname, user["id"])
        )
        await db.commit()
    finally:
        await db.close()
    return {"updated": True, "nickname": nickname}


@router.post("/password")
async def change_password(
    req: ChangePasswordRequest, user: dict = Depends(get_current_user)
):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    try:
        if not verify_password(req.old_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="旧密码错误")
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(req.new_password), user["id"]),
        )
        await db.commit()
    finally:
        await db.close()
    return {"updated": True}


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...), user: dict = Depends(get_current_user)
):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="只支持图片")
    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像不超过 2MB")

    ext = file.content_type.split("/")[-1] if file.content_type else "png"
    fname = f"{user['id']}_{uuid_lib.uuid4().hex[:8]}.{ext}"
    fpath = AVATAR_DIR / fname
    fpath.write_bytes(raw)

    rel = f"avatars/{fname}"
    db = await schema_connect(DEFAULT_DB_PATH)
    await init_auth_tables(db)
    try:
        await db.execute(
            "UPDATE users SET avatar_path = ? WHERE id = ?", (rel, user["id"])
        )
        await db.commit()
    finally:
        await db.close()
    return {"avatar_path": rel}
