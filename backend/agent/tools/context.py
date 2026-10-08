"""
工具运行时上下文。

用于把 store 等依赖注入到通过 @tool 注册的模块级函数里。
工具函数本身不能接收 store 参数（会被 LLM 看到），所以通过 context 传递。

MVP 阶段用简单的 dict 实现，不做线程隔离（Agent 是单进程单线程逻辑）。
目前问题，会话并发执行，会覆盖笔记
"""

from contextvars import ContextVar
from typing import Any


# ============================================================
# store 容器（全局共享，因为所有 Agent 都指向同一 DB）
# ============================================================

_store_context: dict[str, Any] = {}


def set_context(**kwargs: Any) -> None:
    """注入或更新 store 容器（notes / preferences / todos）。"""
    _store_context.update(kwargs)


def get_context(key: str, default: Any = None) -> Any:
    return _store_context.get(key, default)


def clear_context() -> None:
    """清空 store 容器。测试里用。"""
    _store_context.clear()


# ============================================================
# 请求级数据（ContextVar 隔离，每个异步任务独立）
# ============================================================

_last_image_description: ContextVar[str | None] = ContextVar(
    "last_image_description", default=None
)

_last_user_content: ContextVar[str | None] = ContextVar(
    "last_user_content", default=None
)


def set_last_image_description(text: str | None) -> None:
    """记录最近一次图片识别的完整描述。"""
    _last_image_description.set(text)


def get_last_image_description() -> str | None:
    return _last_image_description.get()


def clear_last_image_description() -> None:
    _last_image_description.set(None)


def set_last_user_content(text: str | None) -> None:
    """记录最近一条用户消息的原始内容（不含图片注入）。"""
    _last_user_content.set(text)


def get_last_user_content() -> str | None:
    return _last_user_content.get()