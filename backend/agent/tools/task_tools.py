"""任务相关的工具。让 LLM 能通过对话创建 / 管理任务。

运行前提：set_context(tasks=TasksStore(...))。
"""
from pydantic import BaseModel, Field

from .context import get_context, get_current_user_id
from .registry import tool


def _get_tasks():
    tasks = get_context("tasks")
    if tasks is None:
        raise RuntimeError("tasks store 未初始化。请先 set_context(tasks=...)")
    return tasks


def _uid() -> int:
    return get_current_user_id()


# ============================================================
# 创建任务
# ============================================================

class CreateTaskArgs(BaseModel):
    name: str = Field(
        description="任务名称，一句话概括，20 字以内。例如：看今日日程 / 每周复盘 / 检查订阅"
    )
    prompt: str = Field(
        description=(
            "任务执行时发给 Agent 的完整指令。写成'你希望 Agent 做什么'。"
            "例如：请用 list_todos 查看今天所有未完成待办，按优先级挑出 3-5 件，用简洁列表呈现。"
        )
    )
    trigger_type: str = Field(
        default="manual",
        description=(
            "触发方式：'manual'=用户手动点击执行；'scheduled'=定时自动执行。"
            "只有用户明确说'每天''每周''定时''自动'时才用 scheduled。"
        ),
    )
    schedule: str | None = Field(
        default=None,
        description=(
            "cron 表达式（5 段：分 时 日 月 周）。仅 trigger_type='scheduled' 时必填。"
            "例子：'0 8 * * *'=每天 8:00；'0 9 * * 1'=每周一 9:00；'30 22 * * *'=每天 22:30。"
            "如果用户说的是自然语言（如'每天早上8点'），你要自己转成 cron。"
        ),
    )
    icon: str = Field(
        default="📋",
        description="一个 emoji 图标，用于展示。例如：🌅（早上）/ 🔁（复盘）/ ✉️（邮件）",
    )
    category: str = Field(
        default="general",
        description="分类：general（通用）/ work（工作）/ life（生活）/ learning（学习）/ health（健康）",
    )
    description: str = Field(
        default="",
        description="一句话描述这个任务做什么，15 字以内。",
    )


@tool(
    description=(
        "创建一个新任务。当用户说「帮我建个任务」「每天提醒我…」「每周帮我…」时使用。\n"
        "**必须同时给出 name 和 prompt**：name 是任务名，prompt 是实际的执行指令。\n"
        "**判断触发方式**：\n"
        "- 用户说「明天做」「帮我看看」→ trigger_type='manual'\n"
        "- 用户说「每天」「每周」「早上 8 点」「定时」→ trigger_type='scheduled'，并填 schedule（cron 格式）\n"
        "**创建后只回一句话确认**（如「已创建任务「XX」，每天 8:00 自动执行」），"
        "不要复述 prompt 内容。"
    )
)
async def create_task(args: CreateTaskArgs) -> str:
    tasks = _get_tasks()

    # 校验
    if args.trigger_type not in ("manual", "scheduled"):
        return "❌ trigger_type 必须是 manual 或 scheduled"
    if args.trigger_type == "scheduled" and not args.schedule:
        return "❌ trigger_type=scheduled 时必须提供 schedule（cron 表达式）"

    try:
        task_id = await tasks.create(
            _uid(),
            name=args.name,
            prompt=args.prompt,
            icon=args.icon or "📋",
            description=args.description or "",
            category=args.category or "general",
            tools=[],                          # 暂不限定工具白名单
            trigger_type=args.trigger_type,
            schedule=args.schedule,
            is_builtin=0,
        )
    except ValueError as e:
        return f"❌ 参数错误：{e}"

    # 友好确认
    if args.trigger_type == "scheduled":
        when = _humanize_cron(args.schedule)
        return f"✅ 已创建定时任务「{args.name}」（{when}）"
    else:
        return f"✅ 已创建任务「{args.name}」，可在「任务」页点击执行"


# ============================================================
# 完成 draft 任务（仅在任务创建会话里调用）
# ============================================================

class FinalizeTaskArgs(BaseModel):
    task_id: int = Field(
        description="要完成的任务 ID（从会话 ID 里获取：task-123 → 123）"
    )
    name: str = Field(description="任务名称，20 字以内")
    prompt: str = Field(
        description="任务执行时发给 Agent 的完整指令（你希望 Agent 做什么）"
    )
    trigger_type: str = Field(
        default="manual",
        description="manual（手动执行）/ scheduled（定时自动执行）",
    )
    schedule: str | None = Field(
        default=None,
        description=(
            "cron 表达式（5 段：分 时 日 月 周）。"
            "仅 trigger_type='scheduled' 时必填。"
            "例：'0 8 * * *'=每天 8:00；'0 9 * * 1'=每周一 9:00。"
        ),
    )
    icon: str = Field(default="📋", description="一个 emoji 图标")
    category: str = Field(
        default="general",
        description="general / work / life / learning / health",
    )
    description: str = Field(default="", description="一句话描述，15 字以内")


@tool(
    description=(
        "【仅任务创建会话使用】把正在讨论的任务最终定型并激活。\n"
        "**调用时机**：用户在任务创建会话里表达了足够信息，你已经明确了任务需求。\n"
        "**不要提前调用**：如果用户还在描述需求、你还没确定 key 字段，先问清楚。\n"
        "**必填参数**：task_id（从当前会话 ID 获取）、name、prompt。\n"
        "**调用成功后只回一句话确认**，不要复述 prompt 内容。"
    )
)
async def finalize_task(args: FinalizeTaskArgs) -> str:
    tasks = _get_tasks()

    task = await tasks.get(_uid(), args.task_id)
    if not task:
        return f"❌ 任务 #{args.task_id} 不存在"
    if task.get("status") != "draft":
        return (
            f"❌ 任务 #{args.task_id} 状态是 {task.get('status')}，不是 draft。"
            f"只能定型 draft 状态的任务。"
        )

    if args.trigger_type not in ("manual", "scheduled"):
        return "❌ trigger_type 必须是 manual 或 scheduled"
    if args.trigger_type == "scheduled" and not args.schedule:
        return "❌ scheduled 任务必须提供 schedule（cron 表达式）"

    # 更新字段
    try:
        await tasks.update(
            _uid(),
            args.task_id,
            name=args.name,
            prompt=args.prompt,
            icon=args.icon or "📋",
            description=args.description or "",
            category=args.category or "general",
            trigger_type=args.trigger_type,
            schedule=args.schedule,
        )
    except ValueError as e:
        return f"❌ 参数错误：{e}"

    # 激活
    ok = await tasks.activate(_uid(), args.task_id)
    if not ok:
        return "❌ 激活失败（可能状态不是 draft）"

    # 同步调度器
    try:
        from tasks import scheduler as sched
        task = await tasks.get(_uid(), args.task_id)
        if task:
            sched.register_task(task)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"调度器同步失败: {e}")

    if args.trigger_type == "scheduled":
        when = _humanize_cron(args.schedule)
        return f"✅ 已创建定时任务「{args.name}」（{when}），可在「任务」页查看"
    return f"✅ 已创建任务「{args.name}」，可在「任务」页点击执行"



def _humanize_cron(expr: str | None) -> str:
    """把常见 cron 表达式翻译成人话（覆盖 90% 场景）。"""
    if not expr:
        return "定时"

    parts = expr.split()
    if len(parts) != 5:
        return f"cron: {expr}"

    minute, hour, dom, month, dow = parts

    # 每天固定时间
    if dom == "*" and month == "*" and dow == "*":
        if minute.isdigit() and hour.isdigit():
            return f"每天 {int(hour):02d}:{int(minute):02d}"

    # 每周固定时间
    if dom == "*" and month == "*" and dow.isdigit():
        wd = ["日", "一", "二", "三", "四", "五", "六"][int(dow)]
        if minute.isdigit() and hour.isdigit():
            return f"每周{wd} {int(hour):02d}:{int(minute):02d}"

    # 每月固定时间
    if dom.isdigit() and month == "*" and dow == "*":
        if minute.isdigit() and hour.isdigit():
            return f"每月 {int(dom)} 号 {int(hour):02d}:{int(minute):02d}"

    return f"cron: {expr}"