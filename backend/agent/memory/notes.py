"""笔记 CRUD。

数据模型：
    notes(id, session_id, title, content, tags, created_at, updated_at)

设计要点：
- title 必填：让用户/Agent 明确在记什么
- content 长度限制 10000 字符：防止 LLM 塞入超长内容
- tags 用 JSON 数组存：可过滤
- list() 只返回 100 字预览：避免把全文注入给 LLM
"""

import json
from typing import Any

import aiosqlite

MAX_CONTENT_LEN = 10000
MAX_TITLE_LEN = 200
PREVIEW_LEN = 100


class NotesStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 写 ----------
    async def save(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        summary: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """新建笔记，返回新 id。"""
        title = (title or "").strip()
        content = (content or "").strip()
        summary = (summary or "").strip() or None

        if not title:
            raise ValueError("title 不能为空")
        if len(title) > MAX_TITLE_LEN:
            raise ValueError(f"title 过长（>{MAX_TITLE_LEN} 字符）")
        if not content:
            raise ValueError("content 不能为空")
        if len(content) > MAX_CONTENT_LEN:
            raise ValueError(f"content 过长（>{MAX_CONTENT_LEN} 字符）")

        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None

        cursor = await self._db.execute(
            """
            INSERT INTO notes (session_id, title, content, summary, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, title, content, summary, tags_json),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def update(
        self,
        note_id: int,
        title: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """局部更新。返回 True 表示有改动。"""
        sets: list[str] = []
        values: list[Any] = []

        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("title 不能为空")
            if len(title) > MAX_TITLE_LEN:
                raise ValueError(f"title 过长（>{MAX_TITLE_LEN} 字符）")
            sets.append("title = ?")
            values.append(title)

        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("content 不能为空")
            if len(content) > MAX_CONTENT_LEN:
                raise ValueError(f"content 过长（>{MAX_CONTENT_LEN} 字符）")
            sets.append("content = ?")
            values.append(content)

        if tags is not None:
            sets.append("tags = ?")
            values.append(json.dumps(tags, ensure_ascii=False) if tags else None)

        if not sets:
            return False

        sets.append("updated_at = CURRENT_TIMESTAMP")
        values.append(note_id)

        cursor = await self._db.execute(
            f"UPDATE notes SET {', '.join(sets)} WHERE id = ?",
            values,
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete(self, note_id: int) -> bool:
        cursor = await self._db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    # ---------- 读 ----------
    async def get(self, note_id: int) -> dict[str, Any] | None:
        """返回完整笔记（含全文）。"""
        cursor = await self._db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_dict(row) if row else None

    async def list(
        self,
        tag: str | None = None,
        keyword: str | None = None,
        limit: int | None = 20,
    ) -> list[dict[str, Any]]:
        """
        列表查询（只返回预览，不含全文）。
        - tag: 精确匹配 tags 数组中的一项
        - keyword: 模糊匹配 title 或 content
        """
        sql = (
            "SELECT id, title, summary, tags, "
            "substr(content, 1, ?) AS preview, "
            "created_at, updated_at "
            "FROM notes WHERE 1=1"
        )
        params: list[Any] = [PREVIEW_LEN]

        if tag:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')

        if keyword:
            sql += " AND (title LIKE ? OR content LIKE ?)"
            params.append(f"%{keyword}%")
            params.append(f"%{keyword}%")

        sql += " ORDER BY updated_at DESC, id DESC"

        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_dict(r) for r in rows]

    async def count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) AS n FROM notes")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"])

    # ---------- 内部 ----------
    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        d = dict(row)
        if d.get("tags"):
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        else:
            d["tags"] = []
        return d
