"""记忆工具测试。

覆盖：
- 工具已注册
- 注入 context 后各工具行为
- 参数校验路径
- 空结果 / 未初始化路径
"""

import importlib
import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import PreferencesStore, TodosStore, init_tables
from session.schema import connect as connect_db
from tools.context import clear_context, get_context, set_context
from tools.registry import _TOOLS, invoke_tool

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(autouse=True)
def _clean_registry():
    """
    每个测试前：清空 registry，重载 builtin + memory_tools。
    保证工具注册状态确定。
    """
    from tools import builtin, memory_tools

    _TOOLS.clear()
    importlib.reload(builtin)
    importlib.reload(memory_tools)

    yield

    _TOOLS.clear()
    clear_context()
    importlib.reload(builtin)
    importlib.reload(memory_tools)


@pytest_asyncio.fixture
async def memory_ctx(tmp_path):
    """构造内存 DB + 两个 store，注入 context。"""
    db = await connect_db(str(tmp_path / "test.db"))
    await init_tables(db)
    prefs = PreferencesStore(db)
    todos = TodosStore(db)
    set_context(preferences=prefs, todos=todos)
    yield prefs, todos
    await db.close()


@pytest_asyncio.fixture
async def no_ctx():
    """不注入 context，用于测试未初始化路径。"""
    clear_context()
    yield


# ============================================================
# 1. 注册检查
# ============================================================


class TestRegistration:
    def test_all_memory_tools_registered(self):
        expected = {
            "remember_preference",
            "forget_preference",
            "add_todo",
            "list_todos",
            "complete_todo",
            "delete_todo",
        }
        assert expected.issubset(set(_TOOLS.keys()))

    def test_schemas_generated(self):
        for name in ("remember_preference", "add_todo", "complete_todo"):
            entry = _TOOLS[name]
            schema = entry.schema
            assert schema["type"] == "function"
            assert schema["function"]["name"] == name
            assert schema["function"]["parameters"]["type"] == "object"


# ============================================================
# 2. remember_preference
# ============================================================


class TestRememberPreference:
    @pytest.mark.asyncio
    async def test_success(self, memory_ctx):
        prefs, _ = memory_ctx
        result = await invoke_tool(
            "remember_preference", '{"key": "city", "value": "南京"}'
        )
        assert "已记住" in result
        assert "city" in result
        assert "南京" in result
        assert await prefs.get("city") == "南京"

    @pytest.mark.asyncio
    async def test_update_message(self, memory_ctx):
        prefs, _ = memory_ctx
        await invoke_tool("remember_preference", '{"key": "city", "value": "南京"}')
        result = await invoke_tool(
            "remember_preference", '{"key": "city", "value": "上海"}'
        )
        assert "已更新" in result
        assert await prefs.get("city") == "上海"

    @pytest.mark.asyncio
    async def test_validation_error(self, memory_ctx):
        result = await invoke_tool("remember_preference", '{"key": "city"}')  # 缺 value
        assert "参数校验失败" in result

    @pytest.mark.asyncio
    async def test_empty_value_returns_error(self, memory_ctx):
        result = await invoke_tool(
            "remember_preference", '{"key": "city", "value": "   "}'
        )
        assert "❌" in result
        assert "value 不能为空" in result

    @pytest.mark.asyncio
    async def test_no_context_returns_error(self, no_ctx):
        result = await invoke_tool(
            "remember_preference", '{"key": "city", "value": "南京"}'
        )
        # invoke_tool 会把 RuntimeError 包成 "工具执行失败"
        assert "工具执行失败" in result
        assert "preferences store 未初始化" in result


# ============================================================
# 3. forget_preference
# ============================================================


class TestForgetPreference:
    @pytest.mark.asyncio
    async def test_success(self, memory_ctx):
        prefs, _ = memory_ctx
        await prefs.remember("city", "南京")
        result = await invoke_tool("forget_preference", '{"key": "city"}')
        assert "已忘记" in result
        assert await prefs.get("city") is None

    @pytest.mark.asyncio
    async def test_not_found(self, memory_ctx):
        result = await invoke_tool("forget_preference", '{"key": "nonexistent"}')
        assert "❌" in result
        assert "未找到" in result


# ============================================================
# 4. add_todo
# ============================================================


class TestAddTodo:
    @pytest.mark.asyncio
    async def test_success_no_due(self, memory_ctx):
        _, todos = memory_ctx
        result = await invoke_tool("add_todo", '{"content": "买牛奶"}')
        assert "✅" in result
        assert "#1" in result
        assert "买牛奶" in result
        assert await todos.count(status="pending") == 1

    @pytest.mark.asyncio
    async def test_success_with_due(self, memory_ctx):
        _, todos = memory_ctx
        result = await invoke_tool(
            "add_todo", '{"content": "开会", "due": "2026-09-20"}'
        )
        assert "截止：2026-09-20" in result
        rows = await todos.list()
        assert rows[0]["due"] == "2026-09-20"

    @pytest.mark.asyncio
    async def test_invalid_due_returns_error(self, memory_ctx):
        result = await invoke_tool("add_todo", '{"content": "开会", "due": "下周三"}')
        assert "❌" in result
        assert "due 格式错误" in result


# ============================================================
# 5. list_todos
# ============================================================


class TestListTodos:
    @pytest.mark.asyncio
    async def test_empty(self, memory_ctx):
        result = await invoke_tool("list_todos", "{}")
        assert "📭" in result

    @pytest.mark.asyncio
    async def test_default_pending(self, memory_ctx):
        _, todos = memory_ctx
        await todos.add("A")
        t2 = await todos.add("B")
        await todos.complete(t2)

        result = await invoke_tool("list_todos", "{}")
        assert "A" in result
        assert "B" not in result

    @pytest.mark.asyncio
    async def test_all(self, memory_ctx):
        _, todos = memory_ctx
        await todos.add("A")
        t2 = await todos.add("B")
        await todos.complete(t2)

        result = await invoke_tool("list_todos", '{"status": "all"}')
        assert "A" in result
        assert "B" in result
        assert "[done]" in result

    @pytest.mark.asyncio
    async def test_format_contains_id_and_status(self, memory_ctx):
        _, todos = memory_ctx
        tid = await todos.add("买牛奶")

        result = await invoke_tool("list_todos", "{}")
        assert f"#{tid}" in result
        assert "[pending]" in result


# ============================================================
# 6. complete_todo
# ============================================================


class TestCompleteTodo:
    @pytest.mark.asyncio
    async def test_success(self, memory_ctx):
        _, todos = memory_ctx
        tid = await todos.add("买牛奶")
        result = await invoke_tool("complete_todo", f'{{"id": {tid}}}')
        assert "✅" in result
        assert f"#{tid}" in result
        assert (await todos.get(tid))["status"] == "done"

    @pytest.mark.asyncio
    async def test_not_found(self, memory_ctx):
        result = await invoke_tool("complete_todo", '{"id": 9999}')
        assert "❌" in result
        assert "未找到" in result


# ============================================================
# 7. delete_todo
# ============================================================


class TestDeleteTodo:
    @pytest.mark.asyncio
    async def test_success(self, memory_ctx):
        _, todos = memory_ctx
        tid = await todos.add("买牛奶")
        result = await invoke_tool("delete_todo", f'{{"id": {tid}}}')
        assert "✅" in result
        assert await todos.get(tid) is None

    @pytest.mark.asyncio
    async def test_not_found(self, memory_ctx):
        result = await invoke_tool("delete_todo", '{"id": 9999}')
        assert "❌" in result


# ============================================================
# 8. 多工具协作（模拟 Agent 连续调用）
# ============================================================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_todo_lifecycle(self, memory_ctx):
        """加 -> 列 -> 完成 -> 列"""
        r1 = await invoke_tool("add_todo", '{"content": "写周报"}')
        assert "#1" in r1

        r2 = await invoke_tool("list_todos", "{}")
        assert "写周报" in r2
        assert "[pending]" in r2

        r3 = await invoke_tool("complete_todo", '{"id": 1}')
        assert "✅" in r3

        r4 = await invoke_tool("list_todos", "{}")
        assert "📭" in r4  # 没有 pending 了

    @pytest.mark.asyncio
    async def test_preference_persists_in_context(self, memory_ctx):
        """偏好写入后能通过 store 直接读到（验证 context 是同一个实例）"""
        prefs, _ = memory_ctx
        await invoke_tool("remember_preference", '{"key": "name", "value": "小王"}')
        assert await prefs.get("name") == "小王"
