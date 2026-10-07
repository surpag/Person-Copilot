"""工具注册中心测试。

覆盖：装饰器、schema 生成、invoke 全路径（成功/失败/异步）。
"""

import os
import sys
import pytest
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.registry import (
    _TOOLS,
    _restore,
    _snapshot,
    get_tool_definitions,
    invoke_tool,
    tool,
)

# ============================================================
# Fixture：隔离 registry 状态
# ============================================================


@pytest.fixture
def isolated_registry():
    """
    测试用干净 registry：
    - 进入时清空（保证测试里注册的工具不跟 builtin 混在一起）
    - 退出时恢复（不影响后续测试）
    """
    backup = _snapshot()
    _TOOLS.clear()
    yield
    _restore(backup)


# ============================================================
# 1. 装饰器与 schema
# ============================================================


class TestDecorator:
    def test_registers_tool(self, isolated_registry):
        @tool(description="测试工具")
        def my_tool() -> str:
            return "ok"

        assert "my_tool" in _TOOLS
        assert _TOOLS["my_tool"].description == "测试工具"

    def test_custom_name(self, isolated_registry):
        @tool(name="renamed", description="x")
        def original() -> str:
            return "ok"

        assert "renamed" in _TOOLS
        assert "original" not in _TOOLS

    def test_schema_shape_no_args(self, isolated_registry):
        @tool(description="无参数工具")
        def ping() -> str:
            return "pong"

        defs = get_tool_definitions()
        assert len(defs) == 1
        d = defs[0]
        assert d["type"] == "function"
        assert d["function"]["name"] == "ping"
        assert d["function"]["parameters"]["type"] == "object"
        assert d["function"]["parameters"]["properties"] == {}

    def test_schema_with_pydantic_args(self, isolated_registry):
        class EchoArgs(BaseModel):
            text: str = Field(description="要回显的文本")

        @tool(description="回显")
        def echo(args: EchoArgs) -> str:
            return args.text

        d = get_tool_definitions()[0]
        params = d["function"]["parameters"]
        assert "text" in params["properties"]
        assert params["properties"]["text"]["description"] == "要回显的文本"
        assert params["properties"]["text"]["type"] == "string"
        assert params["required"] == ["text"]
        # title 应被清理
        assert "title" not in params
        assert "title" not in params["properties"]["text"]

    def test_rejects_non_pydantic_param(self, isolated_registry):
        with pytest.raises(TypeError, match="Pydantic"):

            @tool(description="x")
            def bad(a: int) -> str:
                return str(a)

    def test_rejects_too_many_params(self, isolated_registry):
        class A(BaseModel):
            x: int = 1

        with pytest.raises(TypeError, match="0 或 1 个参数"):

            @tool(description="x")
            def bad(a: A, b: A) -> str:
                return "ok"


# ============================================================
# 2. invoke 正常路径
# ============================================================


class TestInvokeSuccess:
    @pytest.mark.asyncio
    async def test_no_args_tool(self, isolated_registry):
        @tool(description="ping")
        def ping() -> str:
            return "pong"

        assert await invoke_tool("ping", "") == "pong"
        assert await invoke_tool("ping", "{}") == "pong"

    @pytest.mark.asyncio
    async def test_with_args(self, isolated_registry):
        class AddArgs(BaseModel):
            a: int
            b: int

        @tool(description="加法")
        def add(args: AddArgs) -> int:
            return args.a + args.b

        result = await invoke_tool("add", '{"a": 3, "b": 4}')
        assert result == "7"

    @pytest.mark.asyncio
    async def test_async_tool(self, isolated_registry):
        import asyncio

        class SlowArgs(BaseModel):
            ms: int = 10

        @tool(description="慢工具")
        async def slow(args: SlowArgs) -> str:
            await asyncio.sleep(args.ms / 1000)
            return "done"

        assert await invoke_tool("slow", '{"ms": 5}') == "done"

    @pytest.mark.asyncio
    async def test_return_non_string(self, isolated_registry):
        @tool(description="返回数字")
        def num() -> int:
            return 42

        assert await invoke_tool("num", "") == "42"


# ============================================================
# 3. invoke 异常路径
# ============================================================


class TestInvokeErrors:
    @pytest.mark.asyncio
    async def test_tool_not_found(self, isolated_registry):
        result = await invoke_tool("nonexistent", "{}")
        assert "找不到工具" in result
        assert "nonexistent" in result

    @pytest.mark.asyncio
    async def test_bad_json(self, isolated_registry):
        @tool(description="x")
        def ping() -> str:
            return "pong"

        result = await invoke_tool("ping", "not-json")
        assert "参数解析失败" in result
        assert "not-json" in result

    @pytest.mark.asyncio
    async def test_json_not_object(self, isolated_registry):
        @tool(description="x")
        def ping() -> str:
            return "pong"

        result = await invoke_tool("ping", "[1, 2, 3]")
        assert "必须是 JSON 对象" in result

    @pytest.mark.asyncio
    async def test_validation_error(self, isolated_registry):
        class AddArgs(BaseModel):
            a: int
            b: int

        @tool(description="加法")
        def add(args: AddArgs) -> int:
            return args.a + args.b

        result = await invoke_tool("add", '{"a": "not-a-number", "b": 4}')
        assert "参数校验失败" in result

    @pytest.mark.asyncio
    async def test_internal_exception(self, isolated_registry):
        @tool(description="会炸的")
        def boom() -> str:
            raise ValueError("我炸了")

        result = await invoke_tool("boom", "")
        assert "工具执行失败" in result
        assert "ValueError" in result
        assert "我炸了" in result


# ============================================================
# 4. 内置工具能正常工作
# ============================================================


class TestBuiltinTools:
    # 用一个不清空的 fixture，只备份/恢复
    @pytest.fixture(autouse=True)
    def _keep_builtin(self):
        from tools import builtin  # noqa: F401  —— 确保 builtin 已导入

        backup = _snapshot()
        yield
        _restore(backup)

    def test_builtin_registered(self):
        from tools import list_tool_names

        names = list_tool_names()
        assert "get_weather" in names
        assert "get_current_time" in names

    @pytest.mark.asyncio
    async def test_builtin_weather(self):
        result = await invoke_tool("get_weather", '{"city": "北京"}')
        assert "多云" in result

    @pytest.mark.asyncio
    async def test_builtin_weather_unknown_city(self):
        result = await invoke_tool("get_weather", '{"city": "火星"}')
        assert "火星" in result

    @pytest.mark.asyncio
    async def test_builtin_time(self):
        import re

        result = await invoke_tool("get_current_time", "")
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result)
