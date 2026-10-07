"""Agent 连接池：session_id → Agent 实例。

每个 Agent 独立 DB 连接，独立历史。同一 session 的请求串行。
"""

import asyncio
import logging
from typing import Any

from run import Agent  # 项目根目录里的 run.py

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = "你是一个聊天助手，可以使用工具帮助用户解决问题。"
DEFAULT_DB_PATH = "agent_memory.db"


class AgentPool:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()  # 保护 _agents / _locks 的创建

    async def get(self, session_id: str) -> Agent:
        """获取或创建 session 对应的 Agent。"""
        async with self._guard:
            if session_id not in self._agents:
                logger.info(f"[pool] 创建新 Agent: session={session_id}")
                agent = Agent(
                    system_prompt=DEFAULT_SYSTEM_PROMPT,
                    session_id=session_id,
                )
                await agent.init(db_path=DEFAULT_DB_PATH)
                self._agents[session_id] = agent
                self._locks[session_id] = asyncio.Lock()
            return self._agents[session_id]

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks[session_id]

    async def close_all(self) -> None:
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
