"""
长期记忆相关工具。

运行前提：调用前必须先 set_context(preferences=..., todos=...)。

工具返回统一的 emoji + 结构化文本格式：
    ✅ 操作成功...
    ❌ 操作失败：原因
    📭 空结果
"""

from pydantic import BaseModel, Field

from .context import get_context, get_current_user_id
from .registry import tool

# ============================================================
# context 依赖获取
# ============================================================


def _get_prefs():
    prefs = get_context("preferences")
    if prefs is None:
        raise RuntimeError(
            "preferences store 未初始化。请先 set_context(preferences=...)"
        )
    return prefs


def _get_todos():
    todos = get_context("todos")
    if todos is None:
        raise RuntimeError("todos store 未初始化。请先 set_context(todos=...)")
    return todos


def _uid() -> int:
    return get_current_user_id()


# ============================================================
# 偏好：记住 / 忘记
# ============================================================


class RememberPreferenceArgs(BaseModel):
    key: str = Field(description="偏好的键，短英文标识符，如 city / job_title / nickname")
    value: str = Field(description="偏好的值，如 南京 / 开发工程师 / 小王")
    scope: str = Field(
        default="user",
        description=(
            "归属：'user'=关于用户，'agent'=关于助手自身。"
            "用户说'我是…''我住…'→ user；"
            "用户说'你叫…''以后叫我…'→ agent。"
        ),
    )
    category: str = Field(
        default="profile",
        description=(
            "具体维度。user 可填：profile(身份)/work(工作)/lifestyle(生活)/"
            "tools(工具)/goals(目标)/routine(习惯)；"
            "agent 可填：character(人设)/tone(语气)/constraints(约束)。"
        ),
    )
    lifetime: str = Field(
        default="long_term",
        description=(
            "时效性：'long_term'(稳定事实)/'mid_term'(几周几个月)/"
            "'transient'(临时状态)。"
        ),
    )


@tool(
    description=(
        "记住用户的一条偏好、事实或关于助手的设定。\n"
        "当用户说「记住…」「我是…」「我住…」「以后叫我…」时使用。\n"
        "**必须选对 scope 和 category**，否则记忆会归错类。"
    )
)
async def remember_preference(args: RememberPreferenceArgs) -> str:
    prefs = _get_prefs()
    try:
        created = await prefs.remember(
            _uid(),
            args.key,
            args.value,
            scope=args.scope,
            category=args.category,
            lifetime=args.lifetime,
        )
    except ValueError as e:
        return f"❌ 参数错误：{e}"

    verb = "已记住" if created else "已更新"
    return (
        f"✅ {verb}：{args.key} = {args.value}"
        f"（{args.scope}/{args.category}/{args.lifetime}）"
    )


class ForgetPreferenceArgs(BaseModel):
    key: str = Field(description="要删除的偏好键")
    scope: str = Field(default="user", description="user / agent")
    category: str = Field(default="profile", description="具体维度")


@tool(
    description=(
        "删除一条用户偏好或助手设定。"
        "当用户说「忘掉…」「不要再记…」时使用。"
    )
)
async def forget_preference(args: ForgetPreferenceArgs) -> str:
    prefs = _get_prefs()
    ok = await prefs.forget(_uid(), args.key, scope=args.scope, category=args.category)
    if not ok:
        return f"❌ 未找到 key={args.key} 的记录"
    return f"✅ 已忘记：{args.key}"


# ============================================================
# 待办：加 / 列 / 完成 / 删
# ============================================================


class AddTodoArgs(BaseModel):
    content: str = Field(description="待办内容，简洁的一两句话")
    due: str | None = Field(
        default=None,
        description=(
            "截止时间。ISO 8601 格式：YYYY-MM-DD 或 YYYY-MM-DDTHH:MM。"
            "不确定时传 null，不要编造。"
        ),
    )


@tool(
    description=(
        "添加一条待办事项。当用户说「记一下…」「提醒我…」「别忘了…」时使用。"
        "如果用户只说了模糊时间（如「下周三」），先和用户确认具体日期再调用。"
    )
)
async def add_todo(args: AddTodoArgs) -> str:
    todos = _get_todos()
    try:
        tid = await todos.add(_uid(), args.content, due=args.due)
    except ValueError as e:
        return f"❌ 参数错误：{e}"
    due_text = f"（截止：{args.due}）" if args.due else ""
    return f"✅ 已添加待办 #{tid}：{args.content}{due_text}"


class ListTodosArgs(BaseModel):
    status: str = Field(
        default="pending",
        description="过滤状态：pending（未完成）/ done（已完成）/ all（全部）",
    )


@tool(description=("列出待办事项。当用户问「我有哪些待办」「还有什么没做」时使用。"))
async def list_todos(args: ListTodosArgs) -> str:
    todos = _get_todos()
    status = None if args.status == "all" else args.status
    try:
        rows = await todos.list(_uid(), status=status)
    except ValueError as e:
        return f"❌ 参数错误：{e}"
    if not rows:
        return "📭 没有符合条件的待办"
    lines = [f"✅ 待办列表（{len(rows)} 条）："]
    for r in rows:
        due = f"（截止：{r['due']}）" if r["due"] else ""
        mark = "[done]" if r["status"] == "done" else "[pending]"
        lines.append(f"  #{r['id']} {mark} {r['content']}{due}")
    return "\n".join(lines)


class CompleteTodoArgs(BaseModel):
    id: int = Field(description="待办 id，从 add_todo 或 list_todos 的返回里获取")


@tool(
    description=(
        "把一条待办标记为完成。当用户说「完成了」「做完了」「搞定了」时使用。"
        "如果用户描述的是内容而非 id，先调用 list_todos 拿到 id。"
    )
)
async def complete_todo(args: CompleteTodoArgs) -> str:
    todos = _get_todos()
    ok = await todos.complete(_uid(), args.id)
    if not ok:
        return f"❌ 未找到 id={args.id} 的待办"
    return f"✅ 已完成待办 #{args.id}"


class DeleteTodoArgs(BaseModel):
    id: int = Field(description="要删除的待办 id")


@tool(description=("删除一条待办。当用户说「删掉」「取消」「不用了」时使用。"))
async def delete_todo(args: DeleteTodoArgs) -> str:
    todos = _get_todos()
    ok = await todos.delete(_uid(), args.id)
    if not ok:
        return f"❌ 未找到 id={args.id} 的待办"
    return f"✅ 已删除待办 #{args.id}"
