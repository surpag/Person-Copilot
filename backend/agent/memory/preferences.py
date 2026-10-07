"""用户偏好 CRUD。

数据模型：
    preferences(key TEXT PRIMARY KEY, value TEXT, source TEXT, ...)

语义：
- remember: upsert。同 key 覆盖
- forget:   物理删除
- get:      取单条
- list_all: 取全部（带时间戳）
- count:    取总数
"""

from typing import Any

import aiosqlite

VALID_SOURCES = {"user", "agent_inferred"}


class PreferencesStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 写 ----------
    async def remember(
        self,
        key: str,
        value: str,
        source: str = "user",
    ) -> bool:
        """
        upsert 一个偏好。

        返回：True 表示新建，False 表示覆盖已有。
        抛异常：key/value 为空，source 非法。
        """
        key = (key or "").strip()
        value = (value or "").strip()
        if not key:
            raise ValueError("key 不能为空")
        if not value:
            raise ValueError("value 不能为空")
        if source not in VALID_SOURCES:
            raise ValueError(f"source 必须是 {VALID_SOURCES} 之一，实际是 {source!r}")

        existed = await self._exists(key)
        await self._db.execute(
            """
            INSERT INTO preferences (key, value, source)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                source     = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, source),
        )
        await self._db.commit()
        return not existed

    async def forget(self, key: str) -> bool:
        """删除一个偏好。返回 True 表示删掉了，False 表示本来就不存在。"""
        key = (key or "").strip()
        if not key:
            raise ValueError("key 不能为空")

        cursor = await self._db.execute("DELETE FROM preferences WHERE key = ?", (key,))
        await self._db.commit()
        return cursor.rowcount > 0

    # ---------- 读 ----------
    async def get(self, key: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["value"] if row else None

    async def get_all(self) -> dict[str, str]:
        """返回 {key: value} 字典，方便注入 prompt。"""
        cursor = await self._db.execute(
            "SELECT key, value FROM preferences ORDER BY key"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {r["key"]: r["value"] for r in rows}

    async def list_all(self) -> list[dict[str, Any]]:
        """返回带元信息的列表，供 CLI 展示。"""
        cursor = await self._db.execute("""
            SELECT key, value, source, created_at, updated_at
            FROM preferences
            ORDER BY updated_at DESC
            """)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) AS n FROM preferences")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"])

    # ---------- 内部 ----------
    async def _exists(self, key: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM preferences WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None
