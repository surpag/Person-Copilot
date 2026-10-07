"""TodosStore 单元测试。

覆盖：加/列/完成/删除、due 校验、排序、状态过滤、边界。
"""

import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import TodosStore, init_tables
from session.schema import connect as connect_db


@pytest_asyncio.fixture
async def todos(tmp_path):
    db = await connect_db(str(tmp_path / "test.db"))
    await init_tables(db)
    yield TodosStore(db)
    await db.close()


# ============================================================
# 添加
# ============================================================
class TestAdd:
    @pytest.mark.asyncio
    async def test_add_basic(self, todos):
        tid = await todos.add("买牛奶")
        assert tid >= 1
        row = await todos.get(tid)
        assert row["content"] == "买牛奶"
        assert row["status"] == "pending"
        assert row["due"] is None
        assert row["completed_at"] is None

    @pytest.mark.asyncio
    async def test_add_with_due_date_only(self, todos):
        tid = await todos.add("开会", due="2026-09-20")
        row = await todos.get(tid)
        assert row["due"] == "2026-09-20"

    @pytest.mark.asyncio
    async def test_add_with_due_datetime(self, todos):
        tid = await todos.add("开会", due="2026-09-20T15:30")
        row = await todos.get(tid)
        assert row["due"] == "2026-09-20T15:30"

    @pytest.mark.asyncio
    async def test_add_with_session_id(self, todos):
        tid = await todos.add("买牛奶", session_id="sess-1")
        row = await todos.get(tid)
        assert row["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_add_empty_content_raises(self, todos):
        with pytest.raises(ValueError, match="content 不能为空"):
            await todos.add("")

    @pytest.mark.asyncio
    async def test_add_whitespace_content_raises(self, todos):
        with pytest.raises(ValueError):
            await todos.add("   ")

    @pytest.mark.asyncio
    async def test_add_invalid_due_raises(self, todos):
        with pytest.raises(ValueError, match="due 格式错误"):
            await todos.add("开会", due="明天下午3点")

    @pytest.mark.asyncio
    async def test_add_empty_due_string_treated_as_none(self, todos):
        tid = await todos.add("开会", due="   ")
        assert (await todos.get(tid))["due"] is None

    @pytest.mark.asyncio
    async def test_add_strips_content(self, todos):
        tid = await todos.add("  买牛奶  ")
        assert (await todos.get(tid))["content"] == "买牛奶"


# ============================================================
# 完成
# ============================================================
class TestComplete:
    @pytest.mark.asyncio
    async def test_complete(self, todos):
        tid = await todos.add("买牛奶")
        assert await todos.complete(tid) is True
        row = await todos.get(tid)
        assert row["status"] == "done"
        assert row["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_nonexistent(self, todos):
        assert await todos.complete(9999) is False

    @pytest.mark.asyncio
    async def test_complete_is_idempotent(self, todos):
        tid = await todos.add("买牛奶")
        assert await todos.complete(tid) is True
        # 再 complete 一次：状态已是 done，仍返回 True（不报错）
        assert await todos.complete(tid) is True

    @pytest.mark.asyncio
    async def test_complete_does_not_touch_content(self, todos):
        tid = await todos.add("买牛奶", due="2026-09-20")
        await todos.complete(tid)
        row = await todos.get(tid)
        assert row["content"] == "买牛奶"
        assert row["due"] == "2026-09-20"


# ============================================================
# 删除
# ============================================================
class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, todos):
        tid = await todos.add("买牛奶")
        assert await todos.delete(tid) is True
        assert await todos.get(tid) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, todos):
        assert await todos.delete(9999) is False

    @pytest.mark.asyncio
    async def test_delete_completed(self, todos):
        tid = await todos.add("买牛奶")
        await todos.complete(tid)
        assert await todos.delete(tid) is True


# ============================================================
# 列表 / 过滤 / 排序
# ============================================================
class TestList:
    @pytest.mark.asyncio
    async def test_list_default_pending(self, todos):
        a = await todos.add("A")
        b = await todos.add("B")
        c = await todos.add("C")
        await todos.complete(b)

        rows = await todos.list()
        ids = [r["id"] for r in rows]
        assert set(ids) == {a, c}

    @pytest.mark.asyncio
    async def test_list_done(self, todos):
        a = await todos.add("A")
        b = await todos.add("B")
        await todos.complete(b)

        rows = await todos.list(status="done")
        assert [r["id"] for r in rows] == [b]

    @pytest.mark.asyncio
    async def test_list_all(self, todos):
        a = await todos.add("A")
        b = await todos.add("B")
        await todos.complete(b)

        rows = await todos.list(status=None)
        assert {r["id"] for r in rows} == {a, b}

    @pytest.mark.asyncio
    async def test_list_invalid_status_raises(self, todos):
        with pytest.raises(ValueError, match="无效状态"):
            await todos.list(status="invalid")

    @pytest.mark.asyncio
    async def test_list_sorted_by_due_then_id(self, todos):
        # 创建顺序：无 due、晚 due、早 due
        no_due = await todos.add("没有截止日")
        late = await todos.add("晚", due="2026-12-31")
        early = await todos.add("早", due="2026-09-20")

        rows = await todos.list()
        ids = [r["id"] for r in rows]
        # 有 due 的按 due 升序，没有 due 的排最后
        assert ids == [early, late, no_due]

    @pytest.mark.asyncio
    async def test_list_empty(self, todos):
        assert await todos.list() == []


# ============================================================
# 计数
# ============================================================
class TestCount:
    @pytest.mark.asyncio
    async def test_count_total(self, todos):
        await todos.add("A")
        await todos.add("B")
        assert await todos.count() == 2

    @pytest.mark.asyncio
    async def test_count_pending(self, todos):
        a = await todos.add("A")
        b = await todos.add("B")
        await todos.complete(b)
        assert await todos.count(status="pending") == 1

    @pytest.mark.asyncio
    async def test_count_done(self, todos):
        a = await todos.add("A")
        b = await todos.add("B")
        await todos.complete(b)
        assert await todos.count(status="done") == 1

    @pytest.mark.asyncio
    async def test_count_invalid_status_raises(self, todos):
        with pytest.raises(ValueError):
            await todos.count(status="bad")
