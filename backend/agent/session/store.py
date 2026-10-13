import json
import aiosqlite
from typing import Any

SUMMARY_TAG = "[摘要]"


class SessionStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get_summary(self, user_id: int, session_id: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT content FROM summaries WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["content"] if row else None

    async def upsert_summary(self, user_id: int, session_id: str, content: str) -> None:
        await self._db.execute(
            """
        INSERT INTO summaries (user_id, session_id, content)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, session_id) DO UPDATE SET
            content    = excluded.content,
            updated_at = CURRENT_TIMESTAMP
        """,
            (user_id, session_id, content),
        )
        await self._db.commit()

    async def archive_older_messages(self, user_id: int, session_id: str, keep_recent: int) -> int:
        """
        归档除最近 keep_recent 条之外的所有未归档非 system 消息。
        返回被归档的数量。
        """
        if keep_recent < 0:
            keep_recent = 0

        cursor = await self._db.execute(
            """
        UPDATE messages
        SET archived = 1
        WHERE user_id = ? AND session_id = ? AND archived = 0 AND role != 'system'
          AND id NOT IN (
              SELECT id FROM messages
              WHERE user_id = ? AND session_id = ? AND archived = 0 AND role != 'system'
              ORDER BY id DESC
              LIMIT ?
          )
            """,
            (user_id, session_id, user_id, session_id, keep_recent),
        )

        count = cursor.rowcount
        await self._db.commit()
        return count

    # ---------- 会话级别 ----------
    async def create_session(self, user_id: int, session_id: str, system_prompt: str) -> None:
        """幂等地创建会话（存在则忽略）。"""
        await self._db.execute(
            """
            INSERT OR IGNORE INTO sessions (user_id, session_id, system_prompt)
            VALUES (?, ?, ?)
            """,
            (user_id, session_id, system_prompt),
        )
        await self._db.commit()

    async def list_sessions(self, user_id: int) -> list[dict[str, Any]]:
        cursor = await self._db.execute("""
            SELECT session_id, system_prompt, created_at, updated_at
            FROM sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """, (user_id,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def delete_session(self, user_id: int, session_id: str) -> None:
        await self._db.execute(
            "DELETE FROM messages WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.commit()

    # ---------- 消息级别 ----------
    async def append(self, user_id: int, session_id: str, message: dict[str, Any]) -> None:
        """追加一条消息。"""
        tool_calls = message.get("tool_calls")
        image_path = message.get("image_path")
        interrupted = message.get("interrupted", 0)

        await self._db.execute(
            """
            INSERT INTO messages
                (user_id, session_id, role, content, tool_calls, tool_call_id,
                 image_path, interrupted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                session_id,
                message.get("role", ""),
                message.get("content") or "",
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                message.get("tool_call_id"),
                image_path,
                interrupted,
            ),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.commit()

    async def update_last_assistant_metrics(
        self,
        user_id: int,
        session_id: str,
        elapsed_ms: int,
        ttft_ms: int | None,
    ) -> None:
        """
        给最近一条 assistant 消息补上耗时信息。
        用于记录 LLM 响应时间，供前端历史渲染显示。
        """
        await self._db.execute(
            """
            UPDATE messages
            SET elapsed_ms = ?, ttft_ms = ?
            WHERE id = (
                SELECT id FROM messages
                WHERE user_id = ? AND session_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
            )
            """,
            (elapsed_ms, ttft_ms, user_id, session_id),
        )
        await self._db.commit()

    async def load(self, user_id: int, session_id: str) -> list[dict[str, Any]]:
        """
        加载顺序：[persona] → [摘要] → [活跃消息]

        - persona 从 sessions.system_prompt 读
        - 摘要从 summaries 表读
        - 活跃消息从 messages 读（archived=0 且 role != system）
        """
        result: list[dict[str, Any]] = []

        # 1. persona
        cursor = await self._db.execute(
            "SELECT system_prompt FROM sessions WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row and row["system_prompt"]:
            result.append({"role": "system", "content": row["system_prompt"]})

        # 2. 摘要
        summary = await self.get_summary(user_id, session_id)
        if summary:
            result.append({"role": "system", "content": f"{SUMMARY_TAG} {summary}"})

        # 3. 活跃消息
        cursor = await self._db.execute(
            """
        SELECT role, content, tool_calls, tool_call_id, image_path,
               elapsed_ms, ttft_ms, interrupted
        FROM messages
        WHERE user_id = ? AND session_id = ? AND archived = 0 AND role != 'system'
        ORDER BY id
        """,
            (user_id, session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        for row in rows:
            msg: dict[str, Any] = {"role": row["role"], "content": row["content"] or ""}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["image_path"]:
                msg["image_path"] = row["image_path"]
            if row["elapsed_ms"] is not None:
                msg["elapsed_ms"] = row["elapsed_ms"]
            if row["ttft_ms"] is not None:
                msg["ttft_ms"] = row["ttft_ms"]
            if row["interrupted"]:
                msg["interrupted"] = 1
            result.append(msg)

        return result

    async def count(self, user_id: int, session_id: str) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["n"])
