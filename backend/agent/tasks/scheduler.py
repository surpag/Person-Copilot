"""定时任务调度器。

基于 APScheduler 的 AsyncIOScheduler：
- 启动时从 DB 加载所有 scheduled + enabled 任务
- 创建/更新/删除时同步调度器
- 到点触发 Agent 执行，结果写入 task_runs
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


# 全局单例
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return _scheduler


def start() -> None:
    """启动调度器（幂等）。"""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        logger.info("[scheduler] 已启动")


def shutdown() -> None:
    """优雅关闭。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] 已关闭")
    _scheduler = None


# ============================================================
# 任务注册/取消
# ============================================================

def _job_id(task_id: int) -> str:
    return f"task-{task_id}"


def register_task(task: dict[str, Any]) -> None:
    """
    给单个任务注册 cron 触发器。已存在则先移除。
    只处理 scheduled + enabled + status=active。
    """
    sched = get_scheduler()
    job_id = _job_id(task["id"])

    # 先清旧的
    try:
        sched.remove_job(job_id)
    except Exception:
        pass

    if task.get("trigger_type") != "scheduled":
        return
    if not task.get("enabled"):
        return
    if task.get("status") != "active":
        return

    cron_expr = task.get("schedule")
    if not cron_expr:
        logger.warning(f"[scheduler] 任务 #{task['id']} 是 scheduled 但没有 schedule")
        return

    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai")
    except Exception as e:
        logger.error(f"[scheduler] cron 解析失败 '{cron_expr}': {e}")
        return

    sched.add_job(
        _run_task_job,
        trigger=trigger,
        id=job_id,
        args=[task["id"]],
        replace_existing=True,
        misfire_grace_time=300,   # 错过 5 分钟内仍执行
    )
    logger.info(f"[scheduler] 注册任务 #{task['id']}「{task['name']}」({cron_expr})")


def unregister_task(task_id: int) -> None:
    """移除任务的调度。"""
    sched = get_scheduler()
    try:
        sched.remove_job(_job_id(task_id))
        logger.info(f"[scheduler] 已移除任务 #{task_id}")
    except Exception:
        pass


# ============================================================
# 执行逻辑（scheduled job 触发时调）
# ============================================================

async def _run_task_job(task_id: int) -> None:
    """定时触发的执行逻辑。"""
    logger.info(f"[scheduler] 触发任务 #{task_id}")

    # 延迟导入，避免循环依赖
    from memory import TasksStore, TaskRunsStore
    from session.schema import connect as schema_connect
    from memory import init_tables

    db = await schema_connect("agent_memory.db")
    await init_tables(db)

    try:
        tasks_store = TasksStore(db)
        runs_store = TaskRunsStore(db)

        task = await tasks_store.get(task_id)
        if not task or task["status"] != "active" or not task["enabled"]:
            logger.info(f"[scheduler] 任务 #{task_id} 已禁用/暂停，跳过")
            return

        # 创建执行记录
        session_id = f"task-{task_id}"
        run_id = await runs_store.start(task_id, session_id=session_id)
        await tasks_store.mark_run(task_id)
    finally:
        await db.close()

    # 执行 Agent
    started = datetime.now()
    success = False
    output = ""
    error = None

    try:
        from api.pool import pool
        agent = await pool.get(session_id)
        async with pool.lock(session_id):
            output = await agent._chat(task["prompt"]) or ""
            success = True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.exception(f"[scheduler] 任务 #{task_id} 执行失败")

    duration_ms = int((datetime.now() - started).total_seconds() * 1000)

    # 写结果
    db2 = await schema_connect("agent_memory.db")
    try:
        runs_store2 = TaskRunsStore(db2)
        await runs_store2.finish(
            run_id,
            success=success,
            output=output,
            error=error,
            duration_ms=duration_ms,
        )
    finally:
        await db2.close()

    logger.info(f"[scheduler] 任务 #{task_id} 完成: success={success}, {duration_ms}ms")


# ============================================================
# 启动时全量加载
# ============================================================

async def load_all_tasks() -> None:
    """从 DB 加载所有 scheduled 任务并注册。启动时调用一次。"""
    from memory import TasksStore, init_tables
    from session.schema import connect as schema_connect

    db = await schema_connect("agent_memory.db")
    await init_tables(db)
    try:
        store = TasksStore(db)
        tasks = await store.list_all(trigger_type="scheduled", status="active")
        logger.info(f"[scheduler] 加载 {len(tasks)} 个定时任务")
        for t in tasks:
            register_task(t)
    finally:
        await db.close()