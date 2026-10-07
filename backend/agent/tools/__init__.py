"""
导入本包即完成所有内置工具的注册。

用法：
    from tools import get_tool_definitions, invoke_tool, tool
"""

from .registry import (
    ToolEntry,
    get_tool_definitions,
    invoke_tool,
    list_tool_names,
    tool,
)
from .context import clear_context, get_context, set_context

from . import builtin  # noqa: F401  —— 时间、天气
from . import memory_tools  # noqa: F401  —— 偏好、待办
from . import note_tools  # 笔记
from . import web_search
from . import fetch_url

__all__ = [
    "ToolEntry",
    "get_tool_definitions",
    "invoke_tool",
    "list_tool_names",
    "tool",
    "set_context",
    "get_context",
    "clear_context",
]
