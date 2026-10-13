"""任务模板（Task）的 CRUD。

Task = 名字 + 图标 + Prompt + 工具白名单 + 触发方式

关键字段：
- user_id:     任务归属用户（多用户隔离）
- tools:       JSON 数组字符串，如 '["list_todos"]'；NULL 或 '[]' 表示不限制
- trigger_type: 'manual' | 'scheduled'
- schedule:    cron 表达式（仅 scheduled 用）
- is_builtin:  1 = 内置任务（全局模板，user_id=0，可禁用不可删），0 = 用户创建
- run_count:   执行次数
- last_run_at: 上次执行时间

多用户语义：
- 内置任务（is_builtin=1, user_id=0）对所有用户可见、不可删，可禁用
- 其余任务严格按 user_id 隔离
"""
import json
from typing import Any

import aiosqlite


VALID_CATEGORIES = {"general", "work", "life", "health", "learning"}
VALID_TRIGGERS = {"manual", "scheduled"}


class TasksStore:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---------- 读 ----------
    async def list_all(
        self,
        user_id: int,
        category: str | None = None,
        trigger_type: str | None = None,
        enabled_only: bool = False,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """列出任务（含内置全局模板）。可按分类/触发方式/启用状态过滤。"""
        sql = "SELECT * FROM tasks WHERE (user_id = ? OR is_builtin = 1)"
        params: list[Any] = [user_id]

        # status 过滤：默认只看 active，传 'all' 查全部
        if status != "all":
            sql += " AND status = ?"
            params.append(status)

        if category:
            sql += " AND category = ?"
            params.append(category)
        if trigger_type:
            sql += " AND trigger_type = ?"
            params.append(trigger_type)
        if enabled_only:
            sql += " AND enabled = 1"

        sql += " ORDER BY is_builtin DESC, category, id"

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._to_dict(r) for r in rows]

    async def get(self, user_id: int, task_id: int) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE id = ? AND (user_id = ? OR is_builtin = 1)",
            (task_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._to_dict(row) if row else None

    async def get_by_name(self, user_id: int, name: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE name = ? AND (user_id = ? OR is_builtin = 1)",
            (name, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._to_dict(row) if row else None

    async def list_scheduled_all(self) -> list[dict[str, Any]]:
        """列出所有用户的 scheduled + active + enabled 任务（仅供调度器启动时加载）。"""
        cursor = await self._db.execute(
            "SELECT * FROM tasks "
            "WHERE trigger_type = 'scheduled' AND status = 'active' AND enabled = 1"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._to_dict(r) for r in rows]

    # ---------- 写 ----------
    async def create(
        self,
        user_id: int,
        name: str,
        prompt: str,
        icon: str = "📋",
        description: str = "",
        category: str = "general",
        tools: list[str] | None = None,
        trigger_type: str = "manual",
        schedule: str | None = None,
        is_builtin: int = 0,
        status: str = "active",
    ) -> int:
        """新建任务，返回 id。"""
        name = (name or "").strip()
        prompt = (prompt or "").strip()

        # draft 允许空（用户在对话里逐步完善）
        if status != "draft":
            if not name:
                raise ValueError("name 不能为空")
            if not prompt:
                raise ValueError("prompt 不能为空")
        # 但 name 至少要有占位
        if not name:
            name = "（未命名任务）"
        if category not in VALID_CATEGORIES:
            raise ValueError(f"category 必须是 {VALID_CATEGORIES} 之一")
        if trigger_type not in VALID_TRIGGERS:
            raise ValueError(f"trigger_type 必须是 {VALID_TRIGGERS} 之一")

        tools_json = json.dumps(tools or [], ensure_ascii=False)

        cursor = await self._db.execute(
            """
            INSERT INTO tasks
                (user_id, name, icon, description, category, prompt, tools,
                 trigger_type, schedule, is_builtin, enabled, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (user_id, name, icon, description, category, prompt, tools_json,
             trigger_type, schedule, is_builtin, status),
        )
        await self._db.commit()
        return int(cursor.lastrowid)

    async def create_draft(self, user_id: int, category: str = "general") -> int:
        """
        创建一个 draft 任务。用于"点 + 新建任务"的对话式创建流程。
        返回 task_id，前端跳 task-{id} 会话。
        """
        from datetime import datetime
        ts = datetime.now().strftime("%m%d-%H%M%S")
        return await self.create(
            user_id=user_id,
            name=f"新任务 {ts}",        # ← 带时间戳，保证唯一
            prompt="（待完善）",
            icon="✏️",
            description="",
            category=category,
            tools=[],
            trigger_type="manual",
            schedule=None,
            is_builtin=0,
            status="draft",
        )

    async def activate(self, user_id: int, task_id: int) -> bool:
        """draft → active。"""
        cursor = await self._db.execute(
            """
            UPDATE tasks
            SET status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ? AND status = 'draft'
            """,
            (task_id, user_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_old_drafts(self, user_id: int, days: int = 7) -> int:
        """清理超过 N 天的未完成 draft。返回删除数量。"""
        cursor = await self._db.execute(
            """
            DELETE FROM tasks
            WHERE user_id = ? AND status = 'draft'
              AND created_at < datetime('now', ?)
            """,
            (user_id, f"-{days} days"),
        )
        await self._db.commit()
        return cursor.rowcount

    async def update(self, user_id: int, task_id: int, **fields: Any) -> bool:
        """局部更新。允许字段：name/icon/description/category/prompt/tools/trigger_type/schedule/enabled。

        内置任务（is_builtin=1）也允许更新（主要改 enabled 开关）。
        """
        allowed = {
            "name", "icon", "description", "category", "prompt",
            "tools", "trigger_type", "schedule", "enabled",
        }

        sets: list[str] = []
        values: list[Any] = []

        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "tools" and isinstance(v, list):
                v = json.dumps(v, ensure_ascii=False)
            if k == "category" and v not in VALID_CATEGORIES:
                raise ValueError(f"category 必须是 {VALID_CATEGORIES} 之一")
            if k == "trigger_type" and v not in VALID_TRIGGERS:
                raise ValueError(f"trigger_type 必须是 {VALID_TRIGGERS} 之一")
            sets.append(f"{k} = ?")
            values.append(v)

        if not sets:
            return False

        sets.append("updated_at = CURRENT_TIMESTAMP")
        values.append(task_id)
        values.append(user_id)

        cursor = await self._db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND (user_id = ? OR is_builtin = 1)",
            values,
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete(self, user_id: int, task_id: int) -> tuple[bool, str]:
        """
        删除任务。
        返回 (成功, 说明)。
        内置任务 (is_builtin=1) 不允许删除——只能禁用。
        """
        task = await self.get(user_id, task_id)
        if not task:
            return False, "任务不存在"
        if task["is_builtin"]:
            return False, "内置任务不允许删除，可改为禁用"

        # 级联删除该任务的所有执行记录
        await self._db.execute(
            "DELETE FROM task_runs WHERE user_id = ? AND task_id = ?", (user_id, task_id)
        )
        await self._db.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        await self._db.commit()
        return True, "已删除"

    async def mark_run(self, user_id: int, task_id: int) -> None:
        """执行一次后记录。"""
        await self._db.execute(
            """
            UPDATE tasks
            SET run_count = run_count + 1,
                last_run_at = CURRENT_TIMESTAMP
            WHERE id = ? AND (user_id = ? OR is_builtin = 1)
            """,
            (task_id, user_id),
        )
        await self._db.commit()

    # ---------- 内部 ----------
    @staticmethod
    def _to_dict(row) -> dict[str, Any]:
        d = dict(row)
        if d.get("tools"):
            try:
                d["tools"] = json.loads(d["tools"])
            except (json.JSONDecodeError, TypeError):
                d["tools"] = []
        else:
            d["tools"] = []
        return d
