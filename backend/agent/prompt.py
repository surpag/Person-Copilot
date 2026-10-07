"""
System Prompt 组装。

设计原则：
- 纯函数，好测试
- 偏好 / 待办超长时截断，不无限注入
- 不同 section 顺序固定，便于 diff / 调试

输出结构：
    [基础人设]

    ## 已知用户偏好
    - key: value

    ## 当前待办（N 条未完成）
    - #id content（截止：YYYY-MM-DD）

    ## 工具使用规则
    - ...
"""

from typing import Any, Final

# ============================================================
# 默认工具使用规则
# ============================================================
DEFAULT_TOOL_RULES: Final[str] = """## 工具使用规则

### 记忆类
- 用户明确说「记住…」「我是…」「我喜欢…」时，调用 remember_preference
- 用户说「忘掉…」「不要再记…」时，调用 forget_preference

### 待办类
- 用户说「记一下…」「提醒我…」时，调用 add_todo
- 用户问「有哪些待办」时，调用 list_todos
- 用户说「完成了」「做完了」时，先确认是哪条待办，再调用 complete_todo
- 用户提到模糊时间（如「下周三」）时，先和用户确认具体日期，再调用工具

### 笔记类
保存图片或长文本时：
- **必须选一个 content_ref**：
  · 用户发图 + 「记一下」「存下来」→ content_ref="last_image"
  · 用户说「存我刚才说的话」→ content_ref="last_message"
  · 用户提供了具体文字 → content_ref="explicit" + content_explicit
- **不要生成正文**：正文会自动从指定来源取
- **一次对话只调一次 save_note**
- **保存成功后立即停止**：只回复一句话（如「已保存笔记 #N」）
- 用户说「总结一下再存」→ 额外传 summary（仍不传 content）

例外：
- 用户明确说「总结一下再存」→ 额外传 summary（200-500 字的要点），仍不传 content
- 用户明确说「存我刚才说的话」「存这段文字」→ 才显式传 content

### 通用
- 不要在回复里直接暴露工具名，用自然语言表达
- 一次对话里**同一个工具只调一次**（除非用户明确要求多次操作）
"""


# ============================================================
# 组装
# ============================================================


def build_system_prompt(
    persona: str,
    preferences: dict[str, str] | None = None,
    todos: list[dict[str, Any]] | None = None,
    *,
    max_prefs: int = 20,
    max_todos: int = 10,
    tool_rules: str = DEFAULT_TOOL_RULES,
) -> str:
    """
    组装完整的 system prompt。

    Args:
        persona: 基础人设（如"你是一个个人助理"）
        preferences: {key: value}，可选
        todos: 待办列表，每项至少包含 id / content / due 字段
        max_prefs: 偏好最多注入几条
        max_todos: 待办最多注入几条
        tool_rules: 工具规则文本，默认使用 DEFAULT_TOOL_RULES

    Returns:
        拼接后的完整字符串
    """
    parts: list[str] = [persona.strip()]

    # ---------- 偏好 ----------
    if preferences:
        items = list(preferences.items())
        shown = items[:max_prefs]
        lines = ["## 已知用户偏好"]
        for key, value in shown:
            lines.append(f"- {key}: {value}")
        if len(items) > max_prefs:
            lines.append(f"- （还有 {len(items) - max_prefs} 条未显示）")
        parts.append("\n".join(lines))

    # ---------- 待办 ----------
    if todos:
        shown = todos[:max_todos]
        lines = [f"## 当前待办（{len(todos)} 条未完成）"]
        for t in shown:
            due = t.get("due")
            due_text = f"（截止：{due}）" if due else ""
            lines.append(f"- #{t['id']} {t['content']}{due_text}")
        if len(todos) > max_todos:
            lines.append(f"- （还有 {len(todos) - max_todos} 条未列出）")
        parts.append("\n".join(lines))

    # ---------- 工具规则 ----------
    if tool_rules:
        parts.append(tool_rules.strip())

    return "\n\n".join(parts)
