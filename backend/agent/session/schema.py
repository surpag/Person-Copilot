"""
会话持久化存储（SQLite + aiosqlite）。

表结构：
- sessions  ：会话元信息（session_id / system_prompt / 时间戳）
- messages  ：消息明细（按 id 递增，保证顺序）
"""

import json
from typing import Any

import aiosqlite

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id     TEXT PRIMARY KEY,
        system_prompt  TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    TEXT NOT NULL,
        role          TEXT NOT NULL,
        content       TEXT NOT NULL DEFAULT '',
        tool_calls    TEXT,
        tool_call_id  TEXT,
        image_path    TEXT,
        elapsed_ms    INTEGER,
        ttft_ms       INTEGER,
        interrupted   INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_session
        ON messages(session_id, id)
    """,
]

_STATEMENTS = [
    # ... 现有的 sessions / messages / 索引 ...
    """
    CREATE TABLE IF NOT EXISTS summaries (
        session_id  TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


async def _migrate_messages_add_image_path(db: aiosqlite.Connection) -> None:
    """给已存在的 messages 表补 image_path 字段。"""
    cursor = await db.execute("PRAGMA table_info(messages)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "image_path" not in cols:
        await db.execute("ALTER TABLE messages ADD COLUMN image_path TEXT")
        await db.commit()


async def _migrate_messages_add_archived(db: aiosqlite.Connection) -> None:
    """给已存在的 messages 表补 archived 字段（兼容旧 DB）。"""
    cursor = await db.execute("PRAGMA table_info(messages)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "archived" not in cols:
        await db.execute(
            "ALTER TABLE messages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
        # 旧的 system 消息归档（现在 persona 由 sessions 表管理）
        await db.execute("UPDATE messages SET archived = 1 WHERE role = 'system'")
        await db.commit()


async def _migrate_messages_add_interrupted(db: aiosqlite.Connection) -> None:
    """给已存在的 messages 表补 interrupted 字段。"""
    cursor = await db.execute("PRAGMA table_info(messages)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "interrupted" not in cols:
        await db.execute(
            "ALTER TABLE messages ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0"
        )
        await db.commit()


async def _migrate_messages_add_metrics(db: aiosqlite.Connection) -> None:
    """给已存在的 messages 表补 elapsed_ms / ttft_ms 字段。"""
    cursor = await db.execute("PRAGMA table_info(messages)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if "elapsed_ms" not in cols:
        await db.execute("ALTER TABLE messages ADD COLUMN elapsed_ms INTEGER")
    if "ttft_ms" not in cols:
        await db.execute("ALTER TABLE messages ADD COLUMN ttft_ms INTEGER")
    await db.commit()


async def connect(db_path: str = "agent_memory.db") -> aiosqlite.Connection:
    """打开数据库、启用 Row 工厂、初始化表结构。"""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row  # 关键：让 row 支持 row["column"]
    for stmt in _STATEMENTS:
        await db.execute(stmt)
    await db.commit()
    await _migrate_messages_add_image_path(db)
    await _migrate_messages_add_archived(db)
    await _migrate_messages_add_metrics(db)
    await _migrate_messages_add_interrupted(db)
    return db
