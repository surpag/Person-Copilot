"""摘要持久化 + 消息归档测试。

覆盖：
- upsert_summary 幂等
- archive_older_messages 的保留策略
- load() 的组装顺序
- 端到端：压缩 → 归档 → 重启 → 加载
"""

import json
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.schema import connect
from session.store import SUMMARY_TAG, SessionStore


@pytest_asyncio.fixture
async def store(tmp_path):
    db = await connect(str(tmp_path / "test.db"))
    yield SessionStore(db)
    await db.close()


# ============================================================
# 1. 摘要持久化
# ============================================================
class TestSummary:
    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        assert await store.get_summary("s1") is None

    @pytest.mark.asyncio
    async def test_upsert_then_get(self, store):
        await store.upsert_summary("s1", "第一次摘要")
        assert await store.get_summary("s1") == "第一次摘要"

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, store):
        await store.upsert_summary("s1", "旧摘要")
        await store.upsert_summary("s1", "新摘要")
        assert await store.get_summary("s1") == "新摘要"

    @pytest.mark.asyncio
    async def test_summary_is_per_session(self, store):
        await store.upsert_summary("s1", "摘要1")
        await store.upsert_summary("s2", "摘要2")
        assert await store.get_summary("s1") == "摘要1"
        assert await store.get_summary("s2") == "摘要2"


# ============================================================
# 2. 归档
# ============================================================
class TestArchive:
    @pytest.mark.asyncio
    async def test_archive_keeps_recent(self, store):
        await store.create_session("s1", "你是助手")
        for i in range(10):
            await store.append("s1", {"role": "user", "content": f"msg{i}"})

        archived = await store.archive_older_messages("s1", keep_recent=3)

        assert archived == 7
        # 活跃消息应该只剩 3 条
        history = await store.load("s1")
        active = [m for m in history if m["role"] != "system"]
        assert len(active) == 3
        assert [m["content"] for m in active] == ["msg7", "msg8", "msg9"]

    @pytest.mark.asyncio
    async def test_archive_zero_keeps_nothing(self, store):
        await store.create_session("s1", "p")
        for i in range(5):
            await store.append("s1", {"role": "user", "content": f"msg{i}"})

        archived = await store.archive_older_messages("s1", keep_recent=0)
        assert archived == 5

    @pytest.mark.asyncio
    async def test_archive_more_than_exist(self, store):
        await store.create_session("s1", "p")
        await store.append("s1", {"role": "user", "content": "only"})

        archived = await store.archive_older_messages("s1", keep_recent=10)
        assert archived == 0

    @pytest.mark.asyncio
    async def test_archive_skips_system_role(self, store):
        """历史遗留的 system 消息不应被 archive 再处理（migration 已归档）"""
        await store.create_session("s1", "p")
        await store.append("s1", {"role": "user", "content": "u1"})
        # 手动塞一条 system 消息
        await store._db.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'system', 'old')",
            ("s1",),
        )
        await store._db.commit()

        await store.archive_older_messages("s1", keep_recent=0)
        history = await store.load("s1")
        # system 消息不出现在 load 结果里（因为 role != 'system' 过滤）
        assert all(m["role"] != "system" or m["content"] == "p" for m in history)


# ============================================================
# 3. load 组装顺序
# ============================================================
class TestLoadOrder:
    @pytest.mark.asyncio
    async def test_load_empty(self, store):
        assert await store.load("nonexistent") == []

    @pytest.mark.asyncio
    async def test_persona_first(self, store):
        await store.create_session("s1", "你是助手")
        history = await store.load("s1")
        assert history[0]["role"] == "system"
        assert history[0]["content"] == "你是助手"

    @pytest.mark.asyncio
    async def test_summary_after_persona(self, store):
        await store.create_session("s1", "你是助手")
        await store.upsert_summary("s1", "历史摘要内容")

        history = await store.load("s1")
        assert len(history) == 2
        assert history[0]["content"] == "你是助手"
        assert history[1]["content"].startswith(SUMMARY_TAG)
        assert "历史摘要内容" in history[1]["content"]

    @pytest.mark.asyncio
    async def test_active_messages_after_summary(self, store):
        await store.create_session("s1", "你是助手")
        await store.upsert_summary("s1", "摘要")
        await store.append("s1", {"role": "user", "content": "u1"})
        await store.append("s1", {"role": "assistant", "content": "a1"})

        history = await store.load("s1")
        assert len(history) == 4
        assert history[0]["role"] == "system"  # persona
        assert history[1]["role"] == "system"  # 摘要
        assert history[2]["content"] == "u1"
        assert history[3]["content"] == "a1"

    @pytest.mark.asyncio
    async def test_archived_not_in_load(self, store):
        await store.create_session("s1", "p")
        for i in range(5):
            await store.append("s1", {"role": "user", "content": f"msg{i}"})

        await store.archive_older_messages("s1", keep_recent=2)

        history = await store.load("s1")
        contents = [m["content"] for m in history if m["role"] != "system"]
        assert contents == ["msg3", "msg4"]


# ============================================================
# 4. 端到端：压缩 → 归档 → 重启 → 加载
# ============================================================
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_simulated_restart(self, tmp_path):
        db_path = str(tmp_path / "e2e.db")

        # 第一次运行：写一堆历史 + 摘要
        db1 = await connect(db_path)
        store1 = SessionStore(db1)
        await store1.create_session("s1", "你是助手")
        for i in range(20):
            await store1.append("s1", {"role": "user", "content": f"旧消息{i}"})
        await store1.upsert_summary("s1", "用户聊了 20 轮，已完成任务 X")
        await store1.archive_older_messages("s1", keep_recent=3)
        await db1.close()

        # 模拟重启
        db2 = await connect(db_path)
        store2 = SessionStore(db2)
        history = await store2.load("s1")
        await db2.close()

        # 验证：persona + 摘要 + 3 条活跃
        assert len(history) == 5
        assert history[0]["content"] == "你是助手"
        assert "用户聊了 20 轮" in history[1]["content"]
        assert [m["content"] for m in history[2:]] == [
            "旧消息17",
            "旧消息18",
            "旧消息19",
        ]

    @pytest.mark.asyncio
    async def test_migration_from_old_db(self, tmp_path):
        """模拟旧 DB：messages 表没有 archived 字段，有旧的 system 消息"""
        import aiosqlite

        db_path = str(tmp_path / "old.db")

        # 手动建一个旧结构 DB
        db = await aiosqlite.connect(db_path)
        await db.execute("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                system_prompt TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "INSERT INTO sessions (session_id, system_prompt) VALUES ('s1', '你是助手')"
        )
        await db.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ('s1', 'system', 'old prompt')"
        )
        await db.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ('s1', 'user', 'hello')"
        )
        await db.commit()
        await db.close()

        # 用新 connect 打开 → 应该触发迁移
        db2 = await connect(db_path)
        store2 = SessionStore(db2)

        # 检查 archived 字段
        cursor = await db2.execute("PRAGMA table_info(messages)")
        cols = [r[1] for r in await cursor.fetchall()]
        await cursor.close()
        assert "archived" in cols

        # 旧的 system 消息应被归档
        history = await store2.load("s1")
        assert history[0]["content"] == "你是助手"
        assert history[1]["content"] == "hello"  # 只有 user，没有旧 system

        await db2.close()
