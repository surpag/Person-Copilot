"""
SessionStore 持久化测试。

覆盖：
1. 创建会话 + 追加消息 + 加载顺序
2. 会话隔离（不同 session_id 不串）
3. 加载不存在的 session 返回空列表
4. tool_calls / tool_call_id 往返完整
5. 空 content（assistant 只调工具时）
6. 关闭连接再打开，数据仍在
7. 删除会话
8. count 计数准确
"""

import os
import sys
import pytest
import pytest_asyncio

# 确保能 import 到 session 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.schema import connect
from session.store import SessionStore


@pytest_asyncio.fixture
async def store(tmp_path):
    """每个测试用独立的临时 DB，互不干扰"""
    db_path = str(tmp_path / "test_memory.db")
    db = await connect(db_path)
    yield SessionStore(db)
    await db.close()


# ---------- 1. 基本 CRUD ----------
@pytest.mark.asyncio
async def test_create_and_load_in_order(store):
    await store.create_session("s1", "你是助手")
    await store.append("s1", {"role": "system", "content": "你是助手"})
    await store.append("s1", {"role": "user", "content": "你好"})
    await store.append("s1", {"role": "assistant", "content": "你也好"})

    history = await store.load("s1")

    assert len(history) == 3
    assert [m["role"] for m in history] == ["system", "user", "assistant"]
    assert [m["content"] for m in history] == ["你是助手", "你好", "你也好"]


# ---------- 2. 会话隔离 ----------
@pytest.mark.asyncio
async def test_session_isolation(store):
    await store.create_session("s1", "p1")
    await store.create_session("s2", "p2")

    await store.append("s1", {"role": "user", "content": "s1 的消息"})
    await store.append("s2", {"role": "user", "content": "s2 的消息"})

    s1 = await store.load("s1")
    s2 = await store.load("s2")

    assert len(s1) == 1 and s1[0]["content"] == "s1 的消息"
    assert len(s2) == 1 and s2[0]["content"] == "s2 的消息"


# ---------- 3. 加载不存在的会话 ----------
@pytest.mark.asyncio
async def test_load_nonexistent_returns_empty(store):
    history = await store.load("never-created")
    assert history == []


# ---------- 4. tool_calls 往返 ----------
@pytest.mark.asyncio
async def test_tool_calls_round_trip(store):
    await store.create_session("s1", "p")
    tool_calls = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
        }
    ]
    await store.append(
        "s1",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        },
    )
    await store.append(
        "s1",
        {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "多云",
        },
    )

    history = await store.load("s1")

    assert history[0]["tool_calls"] == tool_calls
    assert history[1]["tool_call_id"] == "call_abc"
    assert history[1]["content"] == "多云"


# ---------- 5. 空 content ----------
@pytest.mark.asyncio
async def test_empty_content_ok(store):
    await store.create_session("s1", "p")
    # assistant 只调工具时 content 为空
    await store.append(
        "s1",
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                }
            ],
        },
    )

    history = await store.load("s1")
    assert history[0]["content"] == ""


# ---------- 6. 重启后数据仍在 ----------
@pytest.mark.asyncio
async def test_db_reopen_persists(tmp_path):
    db_path = str(tmp_path / "persist.db")

    # 第一次打开
    db1 = await connect(db_path)
    store1 = SessionStore(db1)
    await store1.create_session("s1", "p")
    await store1.append("s1", {"role": "user", "content": "重启前的消息"})
    await db1.close()

    # 第二次打开
    db2 = await connect(db_path)
    store2 = SessionStore(db2)
    history = await store2.load("s1")
    await db2.close()

    assert len(history) == 1
    assert history[0]["content"] == "重启前的消息"


# ---------- 7. 删除会话 ----------
@pytest.mark.asyncio
async def test_delete_session(store):
    await store.create_session("s1", "p")
    await store.append("s1", {"role": "user", "content": "hi"})
    assert await store.count("s1") == 1

    await store.delete_session("s1")

    assert await store.load("s1") == []
    assert await store.count("s1") == 0


# ---------- 8. count 准确 ----------
@pytest.mark.asyncio
async def test_count(store):
    await store.create_session("s1", "p")
    for i in range(5):
        await store.append("s1", {"role": "user", "content": f"msg{i}"})

    assert await store.count("s1") == 5
    assert await store.count("nonexistent") == 0


# ---------- 9. 幂等创建 ----------
@pytest.mark.asyncio
async def test_create_session_idempotent(store):
    """重复 create 同一个 session_id 不应报错，也不应重复插入"""
    await store.create_session("s1", "p1")
    await store.create_session("s1", "p2")  # 第二次用不同 prompt

    sessions = await store.list_sessions()
    ids = [s["session_id"] for s in sessions]
    assert ids.count("s1") == 1
