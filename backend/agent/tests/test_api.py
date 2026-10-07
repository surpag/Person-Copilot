"""FastAPI 端点测试。用 httpx.AsyncClient + ASGITransport，不真起服务。"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest_asyncio.fixture
async def client(mocker):
    """构造一个 mock 了 Agent._chat 的测试客户端。"""
    os.environ["API_TOKEN"] = ""  # 测试时不鉴权

    # mock Agent 的 init 和 _chat
    async def fake_init(self, db_path="agent_memory.db"):
        self._db = MagicMock()
        self.store = MagicMock()
        self._prefs = MagicMock()
        self._todos = MagicMock()
        self._notes = MagicMock()

    async def fake_chat(self, user_input):
        return f"echo: {user_input}"

    mocker.patch("run.Agent.init", fake_init)
    mocker.patch("run.Agent._chat", fake_chat)
    mocker.patch("run.Agent.close", AsyncMock())

    from api.main import app
    from api.pool import pool

    # 每个测试清空 pool
    pool._agents.clear()
    pool._locks.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_chat(client):
    r = await client.post("/chat", json={"session_id": "s1", "message": "你好"})
    assert r.status_code == 200
    assert r.json()["reply"] == "echo: 你好"


@pytest.mark.asyncio
async def test_chat_validation(client):
    # 空 message 应该 422
    r = await client.post("/chat", json={"session_id": "s1", "message": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_stream(client):
    r = await client.post(
        "/chat/stream",
        json={"session_id": "s1", "message": "hi"},
    )
    assert r.status_code == 200
    body = r.text
    assert "done" in body
    assert "echo: hi" in body


@pytest.mark.asyncio
async def test_unknown_session_delete(client):
    r = await client.delete("/sessions/nonexistent")
    assert r.status_code == 404
