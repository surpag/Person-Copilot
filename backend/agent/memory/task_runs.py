"""任务执行记录（TaskRun）的 CRUD。

一次任务执行 = 一条记录，生命周期：
    running → success / failed

用途：
- 前端"今天/昨天"展示执行历史
- 未来统计"这个任务多久跑一次、成功率多少"
"""
from datetime import datetime, timedelta
from typing import Any

import aiosqlite


class TaskRunsStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 写 ----------
    async def start(self, task_id: int, session_id: str | None = None) -> int:
        """任务开始执行，插入一条 running 记录。返回 run_id。"""
        cursor = await self._db.execute(
            """
            INSERT INTO task_runs (task_id, status, session_id)
            VALUES (?, 'running', ?)
            """,
            (task_id, session_id),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def finish(
        self,
        run_id: int,
        success: bool,
        output: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """任务结束，更新记录状态。"""
        await self._db.execute(
            """
            UPDATE task_runs
            SET status = ?,
                output = ?,
                error = ?,
                duration_ms = ?
            WHERE id = ?
            """,
            (
                "success" if success else "failed",
                (output or "")[:2000],   # 只存前 2000 字，避免爆表
                error,
                duration_ms,
                run_id,
            ),
        )
        await self._db.commit()

    # ---------- 读 ----------
    async def list_recent(
        self,
        limit: int = 50,
        task_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """最近的执行记录。task_id 非空时只查该任务的。"""
        sql = """
            SELECT r.*, t.name AS task_name, t.icon AS task_icon
            FROM task_runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            WHERE 1=1
        """
        params: list[Any] = []
        if task_id is not None:
            sql += " AND r.task_id = ?"
            params.append(task_id)
        sql += " ORDER BY r.run_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def list_grouped_by_day(self, days: int = 7) -> dict[str, list[dict]]:
        """
        按天分组。返回 {"today": [...], "yesterday": [...], "2026-10-10": [...]}
        用于前端"今天/昨天/更早"展示。
        """
        # 取最近 N 天的记录
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            """
            SELECT r.*, t.name AS task_name, t.icon AS task_icon
            FROM task_runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            WHERE date(r.run_at) >= ?
            ORDER BY r.run_at DESC
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        grouped: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            run_date = (d.get("run_at") or "")[:10]
            if run_date == today:
                key = "today"
            elif run_date == yesterday:
                key = "yesterday"
            else:
                key = run_date
            grouped.setdefault(key, []).append(d)

        return grouped

    async def get(self, run_id: int) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM task_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def count_by_status(self) -> dict[str, int]:
        cursor = await self._db.execute(
            "SELECT status, COUNT(*) AS n FROM task_runs GROUP BY status"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {r["status"]: int(r["n"]) for r in rows}