"""Agent 连接池：session_id → Agent 实例。

每个 Agent 独立 DB 连接，独立历史。同一 session 的请求串行。

多用户隔离：key 用 f"{user_id}:{session_id}" 复合，避免不同用户撞同 session_id。
"""
import os
import asyncio
import logging
from typing import Any

from run import Agent  # 项目根目录里的 run.py

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = "你是一个聊天助手，可以使用工具帮助用户解决问题。"
_data_dir = os.getenv("DATA_DIR", "")
DEFAULT_DB_PATH = (
    os.path.join(_data_dir, "agent_memory.db") if _data_dir else "agent_memory.db"
)


def _key(user_id: int, session_id: str) -> str:
    return f"{user_id}:{session_id}"


class AgentPool:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()  # 保护 _agents / _locks 的创建
        self._running_tasks: dict[str, asyncio.Task] = {}

    async def get(
        self, session_id: str, user_id: int = 0, llm_config: dict | None = None
    ) -> Agent:
        """获取或创建 session 对应的 Agent。llm_config 变化时热更新 client。"""
        key = _key(user_id, session_id)
        cfg = llm_config or {}
        async with self._guard:
            if key not in self._agents:
                logger.info(f"[pool] 创建新 Agent: user={user_id} session={session_id}")
                agent = Agent(
                    system_prompt=DEFAULT_SYSTEM_PROMPT,
                    session_id=session_id,
                    user_id=user_id,
                    llm_config=cfg,
                )
                await agent.init(db_path=DEFAULT_DB_PATH)
                self._agents[key] = agent
                self._locks[key] = asyncio.Lock()
            else:
                agent = self._agents[key]
                if cfg != agent._llm_config:
                    logger.info(f"[pool] 用户 {user_id} 模型配置变更，热更新 client")
                    agent.update_llm_config(cfg)
            return agent

    def lock(self, session_id: str, user_id: int = 0) -> asyncio.Lock:
        return self._locks[_key(user_id, session_id)]

    def register_task(self, session_id: str, task: asyncio.Task, user_id: int = 0) -> None:
        """注册当前 session 的运行中任务，供取消用。"""
        key = _key(user_id, session_id)
        old = self._running_tasks.get(key)
        if old and not old.done():
            old.cancel()
        self._running_tasks[key] = task

    def cancel_task(self, session_id: str, user_id: int = 0) -> bool:
        """取消当前 session 的运行中任务。返回是否真的取消了。"""
        key = _key(user_id, session_id)
        task = self._running_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def clear_task(self, session_id: str, user_id: int = 0) -> None:
        """任务完成后清掉注册（避免内存泄漏）。"""
        key = _key(user_id, session_id)
        self._running_tasks.pop(key, None)

    async def drop(self, session_id: str, user_id: int = 0) -> bool:
        """关闭并移除某个 session 的 Agent。返回是否真的存在。"""
        key = _key(user_id, session_id)
        agent = self._agents.pop(key, None)
        self._locks.pop(key, None)
        self._running_tasks.pop(key, None)
        if agent is None:
            return False
        await agent.close()
        return True

    async def close_all(self) -> None:
        for sid, task in list(self._running_tasks.items()):
            if not task.done():
                task.cancel()
        self._running_tasks.clear()
        for sid, agent in list(self._agents.items()):
            try:
                await agent.close()
                logger.info(f"[pool] 关闭 Agent: session={sid}")
            except Exception as e:
                logger.error(f"[pool] 关闭失败 {sid}: {e}")
        self._agents.clear()
        self._locks.clear()

    @property
    def active_sessions(self) -> int:
        return len(self._agents)


# 全局单例（FastAPI 生命周期内使用）
pool = AgentPool()
