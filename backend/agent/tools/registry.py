"""
工具注册中心。

用法：
    from pydantic import BaseModel, Field
    from tools.registry import tool

    class MyArgs(BaseModel):
        city: str = Field(description="城市名")

    @tool(description="获取天气")
    def get_weather(args: MyArgs) -> str:
        return "多云"

特性：
- 自动生成 OpenAI function calling 的 JSON Schema
- 自动注册到全局 registry
- 支持 sync / async 工具
- 统一 invoke 入口（含 JSON 解析、Pydantic 校验、异常兜底）
"""

import copy
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


@dataclass
class ToolEntry:
    name: str
    description: str
    args_model: type[BaseModel] | None
    func: Callable
    is_async: bool
    schema: dict


_TOOLS: dict[str, ToolEntry] = {}


# ============================================================
# Schema 清理
# ============================================================


def _clean_schema(node: Any) -> None:
    """递归删除 Pydantic 自动加上的 title 字段（OpenAI 不需要）。"""
    if isinstance(node, dict):
        node.pop("title", None)
        for v in node.values():
            _clean_schema(v)
    elif isinstance(node, list):
        for item in node:
            _clean_schema(item)


def _build_schema(
    name: str, description: str, args_model: type[BaseModel] | None
) -> dict:
    if args_model is None:
        parameters = {"type": "object", "properties": {}}
    else:
        raw = args_model.model_json_schema()
        _clean_schema(raw)
        raw.pop("$defs", None)  # 简单模型不会产生；嵌套模型可后续扩展
        parameters = raw

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


# ============================================================
# 装饰器
# ============================================================


def tool(*, name: str | None = None, description: str):
    """
    把一个函数注册为工具。

    函数签名必须是以下两种之一：
        def fn() -> str                    # 无参数
        def fn(args: MyArgsModel) -> str   # 参数用 Pydantic BaseModel
        async def fn(...) -> str           # 支持异步
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        sig = inspect.signature(func, eval_str=True)
        params = list(sig.parameters.values())

        if len(params) == 0:
            args_model = None
        elif len(params) == 1:
            ann = params[0].annotation
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                args_model = ann
            else:
                raise TypeError(
                    f"工具 {tool_name} 的唯一参数必须是 Pydantic BaseModel 子类，"
                    f"实际是 {ann!r}"
                )
        else:
            raise TypeError(
                f"工具 {tool_name} 只能有 0 或 1 个参数，实际有 {len(params)} 个"
            )

        _TOOLS[tool_name] = ToolEntry(
            name=tool_name,
            description=description,
            args_model=args_model,
            func=func,
            is_async=inspect.iscoroutinefunction(func),
            schema=_build_schema(tool_name, description, args_model),
        )
        return func

    return decorator


# ============================================================
# 对外接口
# ============================================================


def get_tool_definitions() -> list[dict]:
    """返回给 LLM 的工具列表（OpenAI 格式）。"""
    return [entry.schema for entry in _TOOLS.values()]


def list_tool_names() -> list[str]:
    return list(_TOOLS.keys())


async def invoke_tool(name: str, raw_arguments: str) -> str:
    """
    统一工具调用入口。

    - JSON 解析失败 → 返回错误字符串（不抛异常）
    - Pydantic 校验失败 → 返回错误字符串
    - 工具内部异常 → 返回错误字符串
    - 返回值统一转 str，方便喂回 LLM
    """
    entry = _TOOLS.get(name)
    if entry is None:
        return f"错误：找不到工具 {name}"

    # 1. 解析 JSON
    if raw_arguments:
        try:
            raw = json.loads(raw_arguments)
        except json.JSONDecodeError as e:
            return (
                f"参数解析失败: {e}。原始内容: {raw_arguments}。"
                f"请重新生成合法的 JSON。"
            )
        if not isinstance(raw, dict):
            return f"参数必须是 JSON 对象，实际是 {type(raw).__name__}"
    else:
        raw = {}

    # 2. Pydantic 校验
    if entry.args_model is not None:
        try:
            args = entry.args_model(**raw)
        except ValidationError as e:
            return f"参数校验失败: {e}"
    else:
        args = None

    # 3. 执行
    try:
        if entry.args_model is not None:
            coro_or_val = entry.func(args)
        else:
            coro_or_val = entry.func()
        result = await coro_or_val if entry.is_async else coro_or_val
    except Exception as e:
        return f"工具执行失败: {type(e).__name__}: {e}"

    return str(result)


# 测试用：备份/恢复 registry
def _snapshot() -> dict[str, ToolEntry]:
    return dict(_TOOLS)


def _restore(snapshot: dict[str, ToolEntry]) -> None:
    _TOOLS.clear()
    _TOOLS.update(snapshot)
