"""待办 CRUD。

数据模型：
    todos(id INTEGER PK, content, due, status, created_at, completed_at, session_id)

状态机：
    pending --complete()--> done
    *       --delete()----> (物理删除)

due 格式：ISO 8601（YYYY-MM-DD 或 YYYY-MM-DDTHH:MM）
"""

from datetime import datetime
from typing import Any

import aiosqlite

VALID_STATUSES = {"pending", "done"}


class TodosStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 写 ----------
    async def add(
        self,
        content: str,
        due: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """
        添加待办，返回新 id。
        抛异常：content 为空，due 格式错误。
        """
        content = (content or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        due = self._normalize_due(due)

        cursor = await self._db.execute(
            """
            INSERT INTO todos (content, due, session_id)
            VALUES (?, ?, ?)
            """,
            (content, due, session_id),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def complete(self, todo_id: int) -> bool:
        """
        标记完成。返回 True 表示本次生效，False 表示不存在。
        幂等：已完成的待办再次 complete 返回 True（不报错）。
        """
        cursor = await self._db.execute(
            """
            UPDATE todos
            SET status = 'done', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (todo_id,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete(self, todo_id: int) -> bool:
        cursor = await self._db.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    # ---------- 读 ----------
    async def get(self, todo_id: int) -> dict[str, Any] | None:
        cursor = await self._db.execute("SELECT * FROM todos WHERE id = ?", (todo_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def list(self, status: str | None = "pending") -> list[dict[str, Any]]:
        """
        status=None 表示列全部；否则只列指定状态。
        排序：pending 优先按 due（NULL 排最后），再按 id DESC。
        """
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(f"无效状态 {status!r}，应为 {VALID_STATUSES} 之一或 None")

        if status is None:
            sql = """
                SELECT * FROM todos
                ORDER BY
                    CASE WHEN due IS NULL THEN 1 ELSE 0 END,
                    due ASC,
                    id DESC
            """
            cursor = await self._db.execute(sql)
        else:
            sql = """
                SELECT * FROM todos
                WHERE status = ?
                ORDER BY
                    CASE WHEN due IS NULL THEN 1 ELSE 0 END,
                    due ASC,
                    id DESC
            """
            cursor = await self._db.execute(sql, (status,))

        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def count(self, status: str | None = None) -> int:
        if status is None:
            cursor = await self._db.execute("SELECT COUNT(*) AS n FROM todos")
        else:
            if status not in VALID_STATUSES:
                raise ValueError(f"无效状态 {status!r}")
            cursor = await self._db.execute(
                "SELECT COUNT(*) AS n FROM todos WHERE status = ?", (status,)
            )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"])

    # ---------- 内部 ----------
    @staticmethod
    def _normalize_due(due: str | None) -> str | None:
        if due is None:
            return None
        due = due.strip()
        if not due:
            return None
        try:
            datetime.fromisoformat(due)
        except ValueError:
            raise ValueError(
                f"due 格式错误: {due!r}。请使用 ISO 8601（如 2026-09-20 或 2026-09-20T15:30）"
            )
        return due
