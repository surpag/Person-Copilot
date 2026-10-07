"""
工具运行时上下文。

用于把 store 等依赖注入到通过 @tool 注册的模块级函数里。
工具函数本身不能接收 store 参数（会被 LLM 看到），所以通过 context 传递。

MVP 阶段用简单的 dict 实现，不做线程隔离（Agent 是单进程单线程逻辑）。
"""

from typing import Any

_context: dict[str, Any] = {}


def set_context(**kwargs: Any) -> None:
    """注入或更新 context 的若干 key。"""
    _context.update(kwargs)


def get_context(key: str, default: Any = None) -> Any:
    """读取 context 中的一个 key。"""
    return _context.get(key, default)


def clear_context() -> None:
    """清空所有 context。测试里用。"""
    _context.clear()


# ============================================================
# 会话级上下文（供工具按引用取内容）
# ============================================================


def set_last_image_description(text: str | None) -> None:
    """记录最近一次图片识别的完整描述。"""
    _context["last_image_description"] = text


def get_last_image_description() -> str | None:
    return _context.get("last_image_description")


def set_last_user_content(text: str | None) -> None:
    """记录最近一条用户消息的原始内容（不含图片注入）。"""
    _context["last_user_content"] = text


def get_last_user_content() -> str | None:
    return _context.get("last_user_content")


def clear_last_image_description() -> None:
    _context.pop("last_image_description", None)
