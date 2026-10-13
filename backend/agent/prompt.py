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

### 记忆类（remember_preference / forget_preference）
调用时**必须选对 scope 和 category**：

**scope=user**（关于用户）：
- profile   —— 身份：名字、城市、职业、年龄
- work      —— 工作：项目、职位、同事
- lifestyle —— 生活：作息、饮食、运动、爱好
- tools     —— 工具：编辑器、笔记软件、设备
- goals     —— 目标：短期/长期目标
- routine   —— 习惯：每天固定动作

**scope=agent**（关于助手自己）：
- character   —— 人设：名字、性格、说话风格
- tone        —— 语气偏好
- constraints —— 行为约束

**lifetime**：
- long_term —— 稳定事实（"我是工程师"）
- mid_term  —— 几周/几个月（"这个月在做 X"）
- transient —— 临时状态（"最近有点忙"）

判断依据：
- "我是…""我住…""我喜欢…" → user
- "你叫…""以后叫我…""回答时…" → agent

### 笔记类（save_note）
- 按内容选 category：生活→lifestyle、工作→work、工具→tools、目标→goals
- 保存成功后只回一句"已保存笔记 #N"
- **一次对话只调一次** save_note

### 待办类（add_todo）
- category 按内容判断：工作→work、生活→lifestyle、学习→goals
- 默认 lifetime='mid_term'
### 任务类（create_task）
用户想创建任务时（"帮我建个任务""每天提醒我…"），调用 create_task：
- **name 和 prompt 都要给**：name 是任务名（20 字内），prompt 是完整的执行指令
- **判断触发方式**：
  · "帮我看看""今天做" → trigger_type='manual'
  · "每天""每周""早上 8 点" → trigger_type='scheduled'，把时间转成 cron
- **cron 格式**：分 时 日 月 周（5 段）
  · "每天 8:00" → '0 8 * * *'
  · "每周一 9:00" → '0 9 * * 1'
  · "每天 22:30" → '30 22 * * *'
- 创建后**只回一句确认**（如"已创建任务「XX」，每天 8:00 自动执行"）
- **不要复述 prompt 内容**，不要暴露 cron 表达式给用户


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
