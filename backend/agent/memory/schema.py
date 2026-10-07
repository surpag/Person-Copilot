"""
长期记忆的表结构。复用 session 的 aiosqlite 连接。

与 session/schema.py 分离：
- session 表：会话历史（可能被压缩/清理）
- memory 表：跨会话的长期记忆（永不被压缩）

两个 schema 可以用同一个 DB 文件，互不干扰。
"""

import os
from typing import Final

import aiosqlite

_MEMORY_SCHEMA: Final[list[str]] = [
    # 用户偏好：key 唯一，upsert 语义
    """
    CREATE TABLE IF NOT EXISTS preferences (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'user',
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 待办：自增 id，状态机 pending -> done
    """
    CREATE TABLE IF NOT EXISTS todos (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        content      TEXT NOT NULL,
        due          TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        session_id   TEXT
    )
    """,
    # 状态索引：list_todos 频繁按 status 过滤
    """
    CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status)
    """,
    # 笔记：结构化存储
    """
    CREATE TABLE IF NOT EXISTS notes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT,
        title        TEXT NOT NULL,
        content      TEXT NOT NULL,
        summary      TEXT,
        tags         TEXT,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)
    """,
]


async def init_tables(db: aiosqlite.Connection) -> None:
    """
    在已打开的连接上创建 memory 相关表。

    幂等：表已存在时不重复创建。
    """
    for stmt in _MEMORY_SCHEMA:
        await db.execute(stmt)
    await db.commit()
    await _migrate_notes_add_summary(db)  # ← 新增


async def connect(db_path: str) -> aiosqlite.Connection:
    """
    独立打开一个 memory 专用连接（用于 CLI 等不启动完整 Agent 的场景）。
    会自动创建目录和表。
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await init_tables(db)
    return db


async def _migrate_notes_add_summary(db: aiosqlite.Connection) -> None:
    """给已存在的 notes 表补 summary 字段（兼容旧 DB）。"""
    cursor = await db.execute("PRAGMA table_info(notes)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "summary" not in cols:
        await db.execute("ALTER TABLE notes ADD COLUMN summary TEXT")
        await db.commit()
