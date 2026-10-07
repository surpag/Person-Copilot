"""NotesStore 单测。

覆盖：save / update / get / list / delete / count / 边界 / 标签 / 关键词。
"""

import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import NotesStore, init_tables
from memory.notes import MAX_CONTENT_LEN, MAX_TITLE_LEN
from session.schema import connect as connect_db


@pytest_asyncio.fixture
async def notes(tmp_path):
    db = await connect_db(str(tmp_path / "test.db"))
    await init_tables(db)
    yield NotesStore(db)
    await db.close()


# ============================================================
# save
# ============================================================
class TestSave:
    @pytest.mark.asyncio
    async def test_basic(self, notes):
        nid = await notes.save("标题", "正文")
        assert nid >= 1
        row = await notes.get(nid)
        assert row["title"] == "标题"
        assert row["content"] == "正文"
        assert row["tags"] == []

    @pytest.mark.asyncio
    async def test_with_tags(self, notes):
        nid = await notes.save("标题", "正文", tags=["工作", "重要"])
        row = await notes.get(nid)
        assert row["tags"] == ["工作", "重要"]

    @pytest.mark.asyncio
    async def test_strips_whitespace(self, notes):
        nid = await notes.save("  标题  ", "  正文  ")
        row = await notes.get(nid)
        assert row["title"] == "标题"
        assert row["content"] == "正文"

    @pytest.mark.asyncio
    async def test_empty_title_raises(self, notes):
        with pytest.raises(ValueError, match="title 不能为空"):
            await notes.save("", "正文")

    @pytest.mark.asyncio
    async def test_empty_content_raises(self, notes):
        with pytest.raises(ValueError, match="content 不能为空"):
            await notes.save("标题", "")

    @pytest.mark.asyncio
    async def test_title_too_long(self, notes):
        with pytest.raises(ValueError, match="title 过长"):
            await notes.save("x" * (MAX_TITLE_LEN + 1), "正文")

    @pytest.mark.asyncio
    async def test_content_too_long(self, notes):
        with pytest.raises(ValueError, match="content 过长"):
            await notes.save("标题", "x" * (MAX_CONTENT_LEN + 1))


# ============================================================
# update
# ============================================================
class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_title_only(self, notes):
        nid = await notes.save("旧标题", "正文")
        assert await notes.update(nid, title="新标题") is True
        row = await notes.get(nid)
        assert row["title"] == "新标题"
        assert row["content"] == "正文"

    @pytest.mark.asyncio
    async def test_update_content_only(self, notes):
        nid = await notes.save("标题", "旧正文")
        await notes.update(nid, content="新正文")
        assert (await notes.get(nid))["content"] == "新正文"

    @pytest.mark.asyncio
    async def test_update_tags(self, notes):
        nid = await notes.save("标题", "正文", tags=["a"])
        await notes.update(nid, tags=["b", "c"])
        assert (await notes.get(nid))["tags"] == ["b", "c"]

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, notes):
        assert await notes.update(9999, title="x") is False

    @pytest.mark.asyncio
    async def test_update_no_fields_returns_false(self, notes):
        nid = await notes.save("标题", "正文")
        assert await notes.update(nid) is False


# ============================================================
# list / 过滤
# ============================================================
class TestList:
    @pytest.mark.asyncio
    async def test_list_empty(self, notes):
        assert await notes.list() == []

    @pytest.mark.asyncio
    async def test_list_all(self, notes):
        await notes.save("A", "内容A")
        await notes.save("B", "内容B")
        rows = await notes.list()
        assert len(rows) == 2
        # 只返回预览，不含全文
        assert "content" not in rows[0]
        assert "preview" in rows[0]

    @pytest.mark.asyncio
    async def test_filter_by_tag(self, notes):
        await notes.save("A", "内容A", tags=["工作"])
        await notes.save("B", "内容B", tags=["生活"])
        rows = await notes.list(tag="工作")
        assert len(rows) == 1
        assert rows[0]["title"] == "A"

    @pytest.mark.asyncio
    async def test_filter_by_keyword_in_title(self, notes):
        await notes.save("会议记录", "内容1")
        await notes.save("读书笔记", "内容2")
        rows = await notes.list(keyword="会议")
        assert len(rows) == 1
        assert rows[0]["title"] == "会议记录"

    @pytest.mark.asyncio
    async def test_filter_by_keyword_in_content(self, notes):
        await notes.save("A", "这是关于 Python 的笔记")
        await notes.save("B", "这是关于 Java 的笔记")
        rows = await notes.list(keyword="Python")
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_limit(self, notes):
        for i in range(5):
            await notes.save(f"标题{i}", "内容")
        rows = await notes.list(limit=2)
        assert len(rows) == 2


# ============================================================
# delete / count
# ============================================================
class TestDeleteCount:
    @pytest.mark.asyncio
    async def test_delete(self, notes):
        nid = await notes.save("标题", "正文")
        assert await notes.delete(nid) is True
        assert await notes.get(nid) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, notes):
        assert await notes.delete(9999) is False

    @pytest.mark.asyncio
    async def test_count(self, notes):
        assert await notes.count() == 0
        await notes.save("A", "1")
        await notes.save("B", "2")
        assert await notes.count() == 2
