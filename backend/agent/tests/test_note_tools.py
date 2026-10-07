"""笔记工具测试。"""

import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import NotesStore, init_tables
from session.schema import connect as connect_db
from tools.context import clear_context, set_context
from tools.registry import invoke_tool

# 触发注册
from tools import note_tools  # noqa: F401


@pytest_asyncio.fixture
async def notes_ctx(tmp_path):
    db = await connect_db(str(tmp_path / "test.db"))
    await init_tables(db)
    notes = NotesStore(db)
    set_context(notes=notes)
    yield notes
    clear_context()
    await db.close()


# ============================================================
# save_note
# ============================================================
class TestSaveNote:
    @pytest.mark.asyncio
    async def test_basic(self, notes_ctx):
        result = await invoke_tool(
            "save_note",
            '{"title": "会议记录", "content": "讨论了 X 和 Y"}',
        )
        assert "✅" in result
        assert "#1" in result
        assert "会议记录" in result

    @pytest.mark.asyncio
    async def test_with_tags(self, notes_ctx):
        result = await invoke_tool(
            "save_note",
            '{"title": "T", "content": "C", "tags": ["工作", "重要"]}',
        )
        assert "工作" in result
        assert "重要" in result

    @pytest.mark.asyncio
    async def test_empty_title(self, notes_ctx):
        result = await invoke_tool("save_note", '{"title": "  ", "content": "C"}')
        assert "❌" in result


# ============================================================
# list_notes
# ============================================================
class TestListNotes:
    @pytest.mark.asyncio
    async def test_empty(self, notes_ctx):
        result = await invoke_tool("list_notes", "{}")
        assert "📭" in result

    @pytest.mark.asyncio
    async def test_with_data(self, notes_ctx):
        await notes_ctx.save("会议记录", "内容", tags=["工作"])
        result = await invoke_tool("list_notes", "{}")
        assert "会议记录" in result
        assert "#1" in result

    @pytest.mark.asyncio
    async def test_filter_by_tag(self, notes_ctx):
        await notes_ctx.save("A", "1", tags=["工作"])
        await notes_ctx.save("B", "2", tags=["生活"])
        result = await invoke_tool("list_notes", '{"tag": "工作"}')
        assert "A" in result
        assert "B" not in result


# ============================================================
# read_note
# ============================================================
class TestReadNote:
    @pytest.mark.asyncio
    async def test_success(self, notes_ctx):
        nid = await notes_ctx.save("标题", "这是完整正文内容", tags=["测试"])
        result = await invoke_tool("read_note", f'{{"id": {nid}}}')
        assert "标题" in result
        assert "这是完整正文内容" in result
        assert "测试" in result

    @pytest.mark.asyncio
    async def test_not_found(self, notes_ctx):
        result = await invoke_tool("read_note", '{"id": 9999}')
        assert "❌" in result


# ============================================================
# delete_note
# ============================================================
class TestDeleteNote:
    @pytest.mark.asyncio
    async def test_success(self, notes_ctx):
        nid = await notes_ctx.save("标题", "正文")
        result = await invoke_tool("delete_note", f'{{"id": {nid}}}')
        assert "✅" in result
        assert await notes_ctx.get(nid) is None

    @pytest.mark.asyncio
    async def test_not_found(self, notes_ctx):
        result = await invoke_tool("delete_note", '{"id": 9999}')
        assert "❌" in result


# ============================================================
# 端到端
# ============================================================
class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_save_list_read_delete(self, notes_ctx):
        # 存
        r1 = await invoke_tool(
            "save_note", '{"title": "TODO想法", "content": "实现搜索功能"}'
        )
        assert "#1" in r1

        # 列
        r2 = await invoke_tool("list_notes", "{}")
        assert "TODO想法" in r2

        # 读
        r3 = await invoke_tool("read_note", '{"id": 1}')
        assert "实现搜索功能" in r3

        # 删
        r4 = await invoke_tool("delete_note", '{"id": 1}')
        assert "✅" in r4

        # 再列应该空
        r5 = await invoke_tool("list_notes", "{}")
        assert "📭" in r5
