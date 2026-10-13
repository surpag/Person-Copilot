"""FastAPI 依赖注入：从 JWT 解析当前用户。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, Request

from auth.schema import init_auth_tables
from auth.service import decode_jwt, get_user_by_id
from api.pool import DEFAULT_DB_PATH
from session.schema import connect as schema_connect


async def get_current_user(request: Request) -> dict:
    """从 Authorization: Bearer <JWT> 解析用户。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[7:].strip()
    payload = decode_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")

    db = await schema_connect(DEFAULT_DB_PATH)
    try:
        await init_auth_tables(db)
        user = await get_user_by_id(db, user_id)
    finally:
        await db.close()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_admin(request: Request) -> dict:
    user = await get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user
