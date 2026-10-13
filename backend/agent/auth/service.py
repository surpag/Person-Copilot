"""认证核心逻辑：密码 / JWT / 邀请码。"""
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import aiosqlite
import bcrypt
import jwt

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALG = "HS256"
JWT_EXPIRE_DAYS = 7

INVITE_ALPHABET = string.ascii_uppercase + string.digits
INVITE_LENGTH = 8
INVITE_EXPIRE_DAYS = 7

if not JWT_SECRET:
    logger.warning(
        "[auth] JWT_SECRET 未配置。请在 .env 里设置一个随机字符串（64 位）"
    )


# ============ 密码 ============
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ============ JWT ============
def create_jwt(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_EXPIRE_DAYS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ============ 邀请码 ============
def _generate_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


async def create_invite(
    db: aiosqlite.Connection,
    created_by: int | None = None,
    expires_days: int = INVITE_EXPIRE_DAYS,
) -> str:
    code = ""
    for _ in range(10):
        candidate = _generate_invite_code()
        cursor = await db.execute(
            "SELECT code FROM invites WHERE code = ?", (candidate,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            code = candidate
            break
    else:
        raise RuntimeError("生成邀请码失败（撞码太多次）")

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_days)
    ).isoformat()
    await db.execute(
        "INSERT INTO invites (code, created_by, expires_at) VALUES (?, ?, ?)",
        (code, created_by, expires_at),
    )
    await db.commit()
    return code


async def use_invite(db: aiosqlite.Connection, code: str, user_id: int) -> bool:
    code = (code or "").strip().upper()
    cursor = await db.execute("SELECT * FROM invites WHERE code = ?", (code,))
    row = await cursor.fetchone()
    await cursor.close()
    if not row:
        return False
    if row["used_by"] is not None:
        return False

    if row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return False
        except Exception:
            pass

    await db.execute(
        "UPDATE invites SET used_by = ?, used_at = CURRENT_TIMESTAMP WHERE code = ?",
        (user_id, code),
    )
    await db.commit()
    return True


async def list_invites(
    db: aiosqlite.Connection, only_unused: bool = False
) -> list[dict]:
    sql = "SELECT * FROM invites"
    if only_unused:
        sql += " WHERE used_by IS NULL"
    sql += " ORDER BY created_at DESC"
    cursor = await db.execute(sql)
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


# ============ 用户 ============
async def create_user(
    db: aiosqlite.Connection,
    email: str,
    password: str,
    nickname: str = "",
    is_admin: int = 0,
) -> int:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("邮箱格式不正确")
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位")

    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    await cursor.close()
    if row:
        raise ValueError("该邮箱已注册")

    pwd_hash = hash_password(password)
    cursor = await db.execute(
        """
        INSERT INTO users (email, password_hash, nickname, is_admin)
        VALUES (?, ?, ?, ?)
        """,
        (email, pwd_hash, nickname or email.split("@")[0], is_admin),
    )
    await db.commit()
    return int(cursor.lastrowid)


async def get_user_by_email(db: aiosqlite.Connection, email: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row else None


async def get_user_by_id(db: aiosqlite.Connection, user_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row else None


async def update_last_login(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user_id,),
    )
    await db.commit()
