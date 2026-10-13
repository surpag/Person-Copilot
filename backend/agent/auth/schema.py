"""认证相关表。"""
from typing import Final

import aiosqlite


_AUTH_SCHEMA: Final[list[str]] = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nickname      TEXT NOT NULL DEFAULT '',
        avatar_path   TEXT,
        is_admin      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_login_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invites (
        code        TEXT PRIMARY KEY,
        created_by  INTEGER,
        used_by     INTEGER,
        used_at     TEXT,
        expires_at  TEXT,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


async def init_auth_tables(db: aiosqlite.Connection) -> None:
    """建认证表（幂等）。"""
    for stmt in _AUTH_SCHEMA:
        await db.execute(stmt)
    await db.commit()
