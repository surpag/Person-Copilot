"""笔记摘要 + 上下文取内容 测试。"""

import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import NotesStore, init_tables
from session.schema import connect as connect_db
from tools.context import (
    clear_context,
    get_last_long_input,
    set_context,
    set_last_long_input,
)
from tools.registry import invoke_tool
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
# Store 层：summary 字段
# ============================================================


class TestStoreSummary:
    @pytest.mark.asyncio
    async def test_save_with_summary(self, notes_ctx):
        nid = await notes_ctx.save("标题", "很长的正文内容", summary="这是摘要")
        row = await notes_ctx.get(nid)
        assert row["summary"] == "这是摘要"
        assert row["content"] == "很长的正文内容"

    @pytest.mark.asyncio
    async def test_save_without_summary(self, notes_ctx):
        nid = await notes_ctx.save("标题", "正文")
        row = await notes_ctx.get(nid)
        assert row["summary"] is None

    @pytest.mark.asyncio
    async def test_list_includes_summary(self, notes_ctx):
        await notes_ctx.save("A", "很长很长", summary="摘要A")
        rows = await notes_ctx.list()
        assert rows[0]["summary"] == "摘要A"


# ============================================================
# 上下文：last_long_input
# ============================================================


class TestContext:
    def test_set_and_get(self):
        set_last_long_input("模拟图片描述")
        assert get_last_long_input() == "模拟图片描述"

    def test_clear(self):
        set_last_long_input(None)
        assert get_last_long_input() is None


# ============================================================
# 工具层：save_note
# ============================================================


class TestSaveNote:
    @pytest.mark.asyncio
    async def test_uses_last_long_input(self, notes_ctx):
        """不传 content 时，自动取 last_long_input"""
        set_last_long_input("这是图片识别出来的原文")

        result = await invoke_tool(
            "save_note",
            '{"title": "测试标题", "tags": ["测试"]}',
        )
        assert "✅" in result
        assert "#1" in result

        row = await notes_ctx.get(1)
        assert row["content"] == "这是图片识别出来的原文"
        assert row["title"] == "测试标题"
        assert row["tags"] == ["测试"]
        assert row["summary"] is None

    @pytest.mark.asyncio
    async def test_clears_context_after_save(self, notes_ctx):
        """保存成功后 last_long_input 应该被清空"""
        set_last_long_input("内容")
        await invoke_tool("save_note", '{"title": "T"}')
        assert get_last_long_input() is None

    @pytest.mark.asyncio
    async def test_no_content_returns_error(self, notes_ctx):
        """没有 last_long_input 也没传 content → 返回错误"""
        set_last_long_input(None)
        result = await invoke_tool("save_note", '{"title": "T"}')
        assert "❌" in result
        assert "没有可保存的内容" in result

    @pytest.mark.asyncio
    async def test_explicit_content_wins(self, notes_ctx):
        """显式传 content 时优先用 content"""
        set_last_long_input("图片的原文")
        result = await invoke_tool(
            "save_note",
            '{"title": "T", "content": "用户显式传的内容"}',
        )
        assert "✅" in result
        row = await notes_ctx.get(1)
        assert row["content"] == "用户显式传的内容"

    @pytest.mark.asyncio
    async def test_with_summary(self, notes_ctx):
        """用户要摘要时，同时存原文和摘要"""
        set_last_long_input("很长很长的原文...")
        result = await invoke_tool(
            "save_note",
            '{"title": "T", "summary": "这是要点摘要"}',
        )
        assert "✅" in result
        assert "含摘要" in result

        row = await notes_ctx.get(1)
        assert row["content"] == "很长很长的原文..."
        assert row["summary"] == "这是要点摘要"

    @pytest.mark.asyncio
    async def test_multiple_images_uses_latest(self, notes_ctx):
        """连续传图，只用最新的"""
        set_last_long_input("第一张图")
        await invoke_tool("save_note", '{"title": "T1"}')

        set_last_long_input("第二张图")
        await invoke_tool("save_note", '{"title": "T2"}')

        assert (await notes_ctx.get(1))["content"] == "第一张图"
        assert (await notes_ctx.get(2))["content"] == "第二张图"
