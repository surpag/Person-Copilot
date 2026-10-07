"""PreferencesStore 单元测试。

覆盖：upsert 语义、空值校验、CRUD、批量读、并发。
"""

import asyncio
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import PreferencesStore, init_tables
from session.schema import connect as connect_db


@pytest_asyncio.fixture
async def prefs(tmp_path):
    db = await connect_db(str(tmp_path / "test.db"))
    await init_tables(db)
    yield PreferencesStore(db)
    await db.close()


# ============================================================
# 写路径
# ============================================================
class TestRemember:
    @pytest.mark.asyncio
    async def test_new_returns_true(self, prefs):
        created = await prefs.remember("city", "南京")
        assert created is True
        assert await prefs.get("city") == "南京"

    @pytest.mark.asyncio
    async def test_update_returns_false(self, prefs):
        await prefs.remember("city", "南京")
        created = await prefs.remember("city", "上海")
        assert created is False
        assert await prefs.get("city") == "上海"

    @pytest.mark.asyncio
    async def test_strips_whitespace(self, prefs):
        await prefs.remember("  city  ", "  南京  ")
        assert await prefs.get("city") == "南京"

    @pytest.mark.asyncio
    async def test_empty_key_raises(self, prefs):
        with pytest.raises(ValueError, match="key 不能为空"):
            await prefs.remember("", "南京")

    @pytest.mark.asyncio
    async def test_empty_value_raises(self, prefs):
        with pytest.raises(ValueError, match="value 不能为空"):
            await prefs.remember("city", "")

    @pytest.mark.asyncio
    async def test_invalid_source_raises(self, prefs):
        with pytest.raises(ValueError, match="source"):
            await prefs.remember("city", "南京", source="hacker")

    @pytest.mark.asyncio
    async def test_agent_inferred_source(self, prefs):
        await prefs.remember("weather_pref", "喜欢晴天", source="agent_inferred")
        rows = await prefs.list_all()
        assert rows[0]["source"] == "agent_inferred"


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_existing(self, prefs):
        await prefs.remember("city", "南京")
        assert await prefs.forget("city") is True
        assert await prefs.get("city") is None

    @pytest.mark.asyncio
    async def test_forget_nonexistent(self, prefs):
        assert await prefs.forget("nope") is False

    @pytest.mark.asyncio
    async def test_forget_empty_key_raises(self, prefs):
        with pytest.raises(ValueError):
            await prefs.forget("")


# ============================================================
# 读路径
# ============================================================
class TestRead:
    @pytest.mark.asyncio
    async def test_get_nonexistent(self, prefs):
        assert await prefs.get("nope") is None

    @pytest.mark.asyncio
    async def test_get_all(self, prefs):
        await prefs.remember("a", "1")
        await prefs.remember("b", "2")
        await prefs.remember("c", "3")
        assert await prefs.get_all() == {"a": "1", "b": "2", "c": "3"}

    @pytest.mark.asyncio
    async def test_get_all_empty(self, prefs):
        assert await prefs.get_all() == {}

    @pytest.mark.asyncio
    async def test_list_all_sorted_by_updated(self, prefs):
        await prefs.remember("a", "1")
        await prefs.remember("b", "2")
        await prefs.remember("a", "10")  # a 被更新，应该排最前

        rows = await prefs.list_all()
        assert rows[0]["key"] == "a"
        assert rows[0]["value"] == "10"

    @pytest.mark.asyncio
    async def test_count(self, prefs):
        assert await prefs.count() == 0
        await prefs.remember("a", "1")
        await prefs.remember("b", "2")
        assert await prefs.count() == 2


# ============================================================
# 并发 / 幂等
# ============================================================
class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_remember_same_key(self, prefs):
        """并发 upsert 同一 key，最终值合法，不报错。"""
        await asyncio.gather(*[prefs.remember("city", f"城市{i}") for i in range(10)])
        assert await prefs.count() == 1
        final = await prefs.get("city")
        assert final in {f"城市{i}" for i in range(10)}

    @pytest.mark.asyncio
    async def test_concurrent_remember_different_keys(self, prefs):
        await asyncio.gather(*[prefs.remember(f"k{i}", f"v{i}") for i in range(20)])
        assert await prefs.count() == 20
