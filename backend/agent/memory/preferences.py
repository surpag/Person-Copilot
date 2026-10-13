"""用户偏好 / 事实的 CRUD。

结构：
    preferences(id, key, value, scope, category, lifetime, source, ...)

关键概念：
- scope:    user / agent         —— "关于我" 还是 "关于助手"
- category: profile/work/...     —— 具体维度
- lifetime: transient/mid_term/long_term —— 时效性
"""
from typing import Any

import aiosqlite


VALID_SCOPES = {"user", "agent"}
VALID_LIFETIMES = {"transient", "mid_term", "long_term"}
VALID_SOURCES = {"user", "agent_inferred"}


class PreferencesStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 写 ----------
    async def remember(
        self,
        key: str,
        value: str,
        scope: str = "user",
        category: str = "profile",
        lifetime: str = "long_term",
        source: str = "user",
    ) -> bool:
        """upsert 一个偏好。返回 True 表示新建，False 表示更新。"""
        key = (key or "").strip()
        value = (value or "").strip()
        scope = (scope or "user").strip()
        category = (category or "profile").strip()
        lifetime = (lifetime or "long_term").strip()

        if not key:
            raise ValueError("key 不能为空")
        if not value:
            raise ValueError("value 不能为空")
        if scope not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {VALID_SCOPES} 之一")
        if lifetime not in VALID_LIFETIMES:
            raise ValueError(f"lifetime 必须是 {VALID_LIFETIMES} 之一")
        if source not in VALID_SOURCES:
            raise ValueError(f"source 必须是 {VALID_SOURCES} 之一")

        existed = await self._exists(key, scope, category)
        await self._db.execute(
            """
            INSERT INTO preferences (key, value, scope, category, lifetime, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key, scope, category) DO UPDATE SET
                value      = excluded.value,
                lifetime   = excluded.lifetime,
                source     = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, scope, category, lifetime, source),
        )
        await self._db.commit()
        return not existed

    async def update(
        self,
        pref_id: int,
        key: str | None = None,
        value: str | None = None,
        category: str | None = None,
        lifetime: str | None = None,
    ) -> bool:
        """按 id 更新（用于前端编辑）。"""
        sets: list[str] = []
        values: list[Any] = []

        if key is not None:
            sets.append("key = ?")
            values.append(key.strip())
        if value is not None:
            sets.append("value = ?")
            values.append(value.strip())
        if category is not None:
            sets.append("category = ?")
            values.append(category.strip())
        if lifetime is not None:
            if lifetime not in VALID_LIFETIMES:
                raise ValueError(f"lifetime 必须是 {VALID_LIFETIMES} 之一")
            sets.append("lifetime = ?")
            values.append(lifetime)

        if not sets:
            return False

        sets.append("updated_at = CURRENT_TIMESTAMP")
        values.append(pref_id)

        cursor = await self._db.execute(
            f"UPDATE preferences SET {', '.join(sets)} WHERE id = ?",
            values,
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def forget(self, key: str, scope: str = "user", category: str = "profile") -> bool:
        """按 key+scope+category 删除。"""
        cursor = await self._db.execute(
            "DELETE FROM preferences WHERE key = ? AND scope = ? AND category = ?",
            (key.strip(), scope, category),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_by_id(self, pref_id: int) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM preferences WHERE id = ?", (pref_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ---------- 读 ----------
    async def get(self, key: str, scope: str = "user", category: str = "profile") -> str | None:
        cursor = await self._db.execute(
            "SELECT value FROM preferences WHERE key = ? AND scope = ? AND category = ?",
            (key, scope, category),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["value"] if row else None

    async def get_all(self) -> dict[str, str]:
        """返回 {key: value}，只含 scope=user 的（用于注入 system prompt）。"""
        cursor = await self._db.execute(
            "SELECT key, value FROM preferences WHERE scope = 'user' ORDER BY key"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {r["key"]: r["value"] for r in rows}

    async def list_all(self) -> list[dict[str, Any]]:
        """扁平列表（用于 CLI）。"""
        cursor = await self._db.execute(
            """
            SELECT id, key, value, scope, category, lifetime, source,
                   created_at, updated_at
            FROM preferences
            ORDER BY scope, category, updated_at DESC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def list_grouped(self) -> dict[str, dict[str, list[dict]]]:
        """
        返回 {scope: {category: [条目列表]}}。
        前端"记忆"页用它渲染。
        """
        rows = await self.list_all()
        grouped: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            scope = r["scope"] or "user"
            cat = r["category"] or "profile"
            grouped.setdefault(scope, {}).setdefault(cat, []).append(r)
        return grouped

    async def count(self, scope: str | None = None) -> int:
        if scope:
            cursor = await self._db.execute(
                "SELECT COUNT(*) AS n FROM preferences WHERE scope = ?", (scope,)
            )
        else:
            cursor = await self._db.execute("SELECT COUNT(*) AS n FROM preferences")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"])

    # ---------- 内部 ----------
    async def _exists(self, key: str, scope: str, category: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM preferences WHERE key = ? AND scope = ? AND category = ?",
            (key, scope, category),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None