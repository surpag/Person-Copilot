import os
import json
import asyncio
import datetime
import logging
import time
import httpx
import uuid
from typing import Any, Callable
from openai import AsyncOpenAI
from dotenv import load_dotenv
from tools import get_tool_definitions, invoke_tool
from session.store import SessionStore
from session.schema import connect
from session.trace import Tracer, Trace, TraceStep, trace_step
from prompt import build_system_prompt
from memory import PreferencesStore, TodosStore, NotesStore, init_tables
from tools import set_context

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。把历史对话压缩成结构化摘要，"
    "保留关键事实、用户目标、未完成事项、精确的文件名/函数名/报错信息。"
    "严禁编造内容，只压缩已有信息。"
)

INITIAL_SUMMARY_PROMPT = """请把以下对话压缩成结构化摘要：

<对话>
{history}
</对话>

严格使用如下结构，每节保持简洁：

## 目标
[用户想达成什么？可多条]

## 关键事实
- [用户提到的约束、偏好、重要数据]

## 已完成
- [x] ...

## 进行中
- [ ] ...

## 未完成 / 待办
- [ ] ...

## 下一步
1. ...

## 关键上下文
- [继续对话所需的重要信息]

总长度不超过 500 字。"""

UPDATE_SUMMARY_PROMPT = """下面是【上一份摘要】和【新对话内容】。
请把新内容合并进旧摘要，输出一份更新后的结构化摘要。

规则：
- 保留旧摘要里所有仍然有效的信息
- 把新内容里已完成的事项，从"进行中"移到"已完成"
- 更新"下一步"和"未完成"
- 保留精确的文件名、函数名、报错信息

<上一份摘要>
{previous}
</上一份摘要>

<新对话内容>
{new_history}
</新对话内容>

按同样的结构（目标 / 关键事实 / 已完成 / 进行中 / 未完成 / 下一步 / 关键上下文）输出。
总长度不超过 500 字。"""


SUMMARY_TAG = "[摘要]"

# 请求超时（秒）。SDK 默认 600s 且自动重试 2 次，一次挂起的请求能拖住十几分钟，这里显式收紧。
REQUEST_TIMEOUT_S = 120  # 主调用：实测最慢约 70s，留 ~1.7 倍余量
SUMMARY_TIMEOUT_S = 60  # 摘要调用：正常 8-25s，超时就当失败并降级


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


def tool_definitions() -> list[dict]:
    return [
        _tool(
            "get_current_time",
            "获取当前的系统时间。当用户询问现在几点、今天几号时使用。",
            None,
            None,
        ),
        # 新增天气调用工具
        _tool(
            "get_weather",
            "获取当前城市的天气",
            {"city": {"type": "string", "description": "城市名称"}},
            ["city"],
        ),
    ]


def get_current_time():
    """本地真正执行的函数"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_weather(city: str):
    weather_data = {"南京": "晴", "上海": "雨天", "北京": "多云"}
    # 根据传入的 city 查找，找不到就返回默认提示
    return weather_data.get(city, f"抱歉，暂时没有{city}的天气信息")


# 工具名到函数的映射字典，方便后面通过名字直接调用
available_tools = {"get_current_time": get_current_time, "get_weather": get_weather}


def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += len(content) // 2
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"])) // 4
    return total


# 新增
# 模块级缓存（工具定义在启动后基本不变）
# _TOOL_DEF_TOKENS_CACHE: int | None = None


# def _get_tool_def_tokens() -> int:
#     """计算工具定义占用的 token 数（带缓存）。"""
#     global _TOOL_DEF_TOKENS_CACHE
#     if _TOOL_DEF_TOKENS_CACHE is None:
#         tools = get_tool_definitions()
#         tools_json = json.dumps(tools, ensure_ascii=False)
#         # JSON Schema 结构密集，token 密度比普通文本高，用 /3 更接近实际
#         _TOOL_DEF_TOKENS_CACHE = len(tools_json) // 3
#     return _TOOL_DEF_TOKENS_CACHE


# def _estimate_tokens_full(messages: list[dict]) -> int:
#     """
#     完整 token 估算：messages + 工具定义 + 保守系数。

#     为什么需要保守系数：
#     - _estimate_tokens 用 len(content) // 2 估算中文，
#       但实际 tokenizer 对中文约 1.5 字符/token，会低估
#     - system prompt 里的结构化内容（列表、emoji）token 密度也高于普通文本
#     """
#     msg_tokens = _estimate_tokens(messages)
#     tool_tokens = _get_tool_def_tokens()
#     return int((msg_tokens + tool_tokens) * 1.5)


def _split_into_turns(messages: list[dict]) -> list[list[dict]]:
    """
    把消息拆成 turn 列表。
    一个 turn 从 user 或 assistant 消息开始，包含后面紧跟的所有 tool 消息。
    —— 这保证了 assistant(tool_calls) 和它的 tool 结果永远在同一个 turn 里。
    """
    turns: list[list[dict]] = []
    current: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role in ("user", "assistant"):
            if current:
                turns.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        turns.append(current)
    return turns


# 从尾部往前找，凑足最近N轮“完整turn”
def _recent_msgs(
    messages: list[dict], keep_tokens: int, max_turns: int | None = None
) -> list[dict]:
    """
     从尾部往前保留 keep_turns 个完整 turn。
    一个 turn 是 user/assistant 开头的消息，后跟任意条 tool 消息。

    keep_tokens 和 max_turns 两个上限同时生效，取先到的那个；
    无论如何至少保留最后一个 turn，否则当前这轮对话就没了。
    """
    if not messages:
        return []
    turns = _split_into_turns(messages)
    if not turns:
        return []
    kept: list[list[dict]] = []
    total = 0
    for turn in reversed(turns):
        if max_turns is not None and len(kept) >= max_turns:
            break
        turn_tokens = _estimate_tokens(turn)
        if kept and total + turn_tokens > keep_tokens:
            break
        kept.insert(0, turn)
        total += turn_tokens
    return [m for turn in kept for m in turn]


class Agent:
    def __init__(
        self,
        system_prompt: str,
        session_id: str | None = None,
        on_token: Callable[[str, str], None] | None = None,
    ):
        # system_prompt = "你是一个聊天助手，可以使用工具帮助用户解决问题。"
        self.client = AsyncOpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            timeout=REQUEST_TIMEOUT_S,
            max_retries=0,  # 不要让 SDK 自动重试放大等待时间
        )
        # self.system_prompt = system_prompt
        self.persona = system_prompt
        self.model = os.getenv("LLM_MODEL_ID")
        self.messages = [{"role": "system", "content": system_prompt}]
        self.max_steps = 5
        self.SUMMARY_MAX_RETRIES = 3
        self.KEEP_RECENT_TOKENS = 1200  # 保留最近多少 token 不压缩
        self.KEEP_RECENT_TURNS = 10  # 保留最近多少个完整 turn 不压缩
        self.SUMMARY_MAX_TOKENS = 500  # 摘要本身最大输出
        self.KEEP_RECENT_TOKENS = 1200
        self.MAX_ACTIVE_TOKENS = 3000
        self.session_id = session_id or str(uuid.uuid4())
        self.store: SessionStore | None = None  # 等 init() 里赋
        self._prefs: PreferencesStore | None = None  # ← 新增
        self._todos: TodosStore | None = None  # ← 新增
        self._notes: NotesStore | None = None
        self._db = None
        self.tracer = Tracer("traces")
        # 压缩触发阈值：只管"可压缩的部分"（活跃消息 + 轮数），
        # 不含工具定义/persona/摘要 —— 那些压缩也缩不掉，算进来会让目标态不可达。
        self.MAX_ACTIVE_TOKENS = 3000
        self.MAX_TURNS = 30
        # 上下文硬上限（含工具定义的估算口径）：纯粹的安全网，超了强制收缩
        self.MAX_CONTEXT_TOKENS = 24000
        self.summary_model = "qwen3.8-27b"
        # 流式回调：(text, kind)，kind 为 "content"（正文）或 "reasoning"（思考）
        self.on_token = on_token
        self._ephemeral_turn_ids = set()  # 临时轮次 ID

    async def init(self, db_path: str = "agent_memory.db"):
        """打开 DB、创建会话（幂等）、恢复历史。"""
        db = await connect(db_path)
        self._db = db
        await init_tables(db)
        self.store = SessionStore(db)
        self._prefs = PreferencesStore(db)
        self._todos = TodosStore(db)
        self._notes = NotesStore(db)
        # 注入 context，让 @tool 装饰的函数能访问 store
        set_context(preferences=self._prefs, todos=self._todos, notes=self._notes)
        # 幂等：session 已存在不会重复写
        await self.store.create_session(self.session_id, self.persona)

        # 恢复历史（如果 DB 里已经有该 session 的消息）
        history = await self.store.load(self.session_id)
        if history:
            self.messages = history
            logger.info(f"[session={self.session_id}] 已恢复 {len(history)} 条历史消息")
        else:
            # 空历史时，把 system prompt 也写进 DB
            await self._refresh_system_prompt()
            # await self.store.append(self.session_id, self.messages[0])

    async def _refresh_system_prompt(self) -> dict:
        """
        从 store 读取最新偏好和待办，重新组装 system prompt，
        并替换 messages 里的第一条非摘要 system 消息。
        """
        if self._prefs is None or self._todos is None:
            return {"skipped": True, "pref_count": 0, "todo_count": 0}

        prefs = await self._prefs.get_all()
        todos = await self._todos.list(status="pending")

        content = build_system_prompt(self.persona, prefs, todos)

        for i, m in enumerate(self.messages):
            if m["role"] == "system" and not (m.get("content") or "").startswith(
                SUMMARY_TAG
            ):
                # 隐形问题，直接替换可能会丢掉原始system内容
                self.messages[i] = {"role": "system", "content": content}
                break

        return {
            "skipped": False,
            "pref_count": len(prefs),
            "todo_count": len(todos),
        }

    @staticmethod
    def _is_context_overflow(exc: Exception) -> bool:
        """判断是否上下文溢出错误（摘要防死锁触发条件）。

        兼容 OpenAI SDK 的 BadRequestError（code=context_length_exceeded），
        以及其他供应商/网关返回的文本形式错误。
        """
        if isinstance(exc, Exception):
            if getattr(exc, "code", None) == "context_length_exceeded":
                return True
            body = getattr(exc, "body", None)
            message = str(body) if body is not None else str(exc)
            return "maximum context length" in message.lower()
        message = str(exc).lower()
        return (
            "context_length_exceeded" in message or "maximum context length" in message
        )

    # async def _trim_messages(self, trace: Trace):
    #     # if len(self.messages) <= self.SUMMARY_TRIGGER:
    #     #     return
    #     if self._estimate_tokens(self.messages) < self.MAX_TOKENS:
    #         return

    #     system_msgs = [m for m in self.messages if m["role"] == "system"]
    #     other_msgs = [m for m in self.messages if m["role"] != "system"]

    #     history_text = "\n".join(
    #         f"{m['role']}: {m.get('content','')}" for m in other_msgs
    #     )
    #     summary = history_text

    #     prompt_messages = [
    #         {
    #             "role": "system",
    #             "content": (
    #                 "你是对话摘要助手，请把用户与助手的对话压缩成简洁的要点摘要，"
    #                 "保留关键事实、用户目标与未完成事项。"
    #                 + (
    #                     "请尽量精简，只保留最关键的事实与未完成事项，总长度不超过500字。"
    #                 )
    #             ),
    #         },
    #         {"role": "user", "content": f"请摘要以下对话：\n\n{history_text}"},
    #     ]

    #     for attempt in range(self.SUMMARY_MAX_RETRIES):
    #         prompt_messages = [
    #             {
    #                 "role": "system",
    #                 "content": (
    #                     "你是对话摘要助手，请把用户与助手的对话压缩成简洁的要点摘要，"
    #                     "保留关键事实、用户目标与未完成事项。"
    #                     + (
    #                         "请尽量精简，只保留最关键的事实与未完成事项，总长度不超过500字。"
    #                     )
    #                 ),
    #             },
    #             {"role": "user", "content": f"请摘要以下对话：\n\n{history_text}"},
    #         ]

    #         # 摘要压缩使用小模型
    #         try:
    #             # 新增trace
    #             async with trace_step(
    #                 trace,
    #                 "llm_call",
    #                 "summary.create",
    #                 input={"attempt": attempt, "history_len": len(history_text)},
    #             ) as s:
    #                 response = await self.client.chat.completions.create(
    #                     model=self.model,
    #                     messages=prompt_messages,
    #                     max_tokens=500,
    #                 )
    #                 summary = response.choices[0].message.content
    #                 usage = getattr(response, "usage", None)
    #                 if usage:
    #                     s.metadata["total_tokens"] = getattr(usage, "total_tokens", 0)
    #                 s.output = summary[:200]
    #                 break
    #         except Exception as e:
    #             if self._is_context_overflow(e) and len(history_text) > 1:
    #                 history_text = history_text[len(history_text) // 2 :]
    #                 prompt_messages[1][
    #                     "content"
    #                 ] = f"请摘要以下对话：\n\n{history_text}"
    #             else:
    #                 logger.error(f"摘要失败：{e}")
    #                 break
    #             #
    #     recent_msgs = other_msgs[-2:] if len(other_msgs) > 2 else []
    #     self.messages = (
    #         system_msgs
    #         + [{"role": "system", "content": f"以下是之前对话的摘要：\n{summary}"}]
    #         + recent_msgs
    #     )

    def _plan_compaction(self) -> dict | None:
        """
         纯计算：判断是否需要压缩，返回压缩计划。不调 LLM。
        返回 None 表示不需要压缩。
        """

        # 1. 分类消息
        system_msgs: list[dict] = []
        summary_msgs: list[dict] = []
        other_msgs: list[dict] = []

        for m in self.messages:
            if m["role"] == "system":
                if (m.get("content") or "").startswith(SUMMARY_TAG):
                    summary_msgs.append(m)
                else:
                    system_msgs.append(m)
            else:
                other_msgs.append(m)

        turn_count = len(_split_into_turns(other_msgs))

        # 2. 只估算活跃对话消息（不含工具定义 / persona / 摘要）
        active_tokens = _estimate_tokens(other_msgs)  # ← 改这里
        if active_tokens <= self.MAX_ACTIVE_TOKENS and turn_count <= self.MAX_TURNS:
            return None

        # 3. 按 turn 对齐保留最近的消息
        recent = _recent_msgs(other_msgs, self.KEEP_RECENT_TOKENS)
        if not recent:
            return None

        # 4. 需要摘要的历史
        n_to_sum = len(other_msgs) - len(recent)
        if n_to_sum <= 0:
            return None
        to_summarize = other_msgs[:n_to_sum]

        # 5. 增量摘要
        previous_summary: str | None = None
        if summary_msgs:
            last_content = summary_msgs[-1]["content"]
            previous_summary = last_content[len(SUMMARY_TAG) :].strip()

        return {
            "system_msgs": system_msgs,
            "to_summarize": to_summarize,
            "recent": recent,
            "previous_summary": previous_summary,
            "tokens_before": active_tokens,  # ← 改成活跃消息 token
            "turn_count": turn_count,
        }

    async def _compact(self, plan: dict, trace: Trace) -> str | None:
        """调 LLM 生成摘要（首次用 INITIAL，之后用 UPDATE 增量更新）。

        失败（超时 / 接口报错）返回 None，由调用方降级处理：
        压缩只是优化手段，不该让整轮对话卡死或失败。
        """
        history_text = "\n".join(
            f"{m['role']}: {m.get('content') or ''}" for m in plan["to_summarize"]
        )
        # 截断摘要
        MAX_HISTORY_CHARS = 8000
        if len(history_text) > MAX_HISTORY_CHARS:
            logger.warning(
                f"[摘要] history 过长 ({len(history_text)} chars)，截断到 {MAX_HISTORY_CHARS}"
            )
        history_text = history_text[:MAX_HISTORY_CHARS] + "\n...（已截断）"

        if plan["previous_summary"]:
            user_prompt = UPDATE_SUMMARY_PROMPT.format(
                previous=plan["previous_summary"],
                new_history=history_text,
            )
        else:
            user_prompt = INITIAL_SUMMARY_PROMPT.format(history=history_text)

        logger.info(
            f"[摘要] model={self.summary_model} "
            f"history_chars={len(history_text)} "
            f"prompt_chars={len(user_prompt)}"
        )

        async with trace_step(trace, "llm_call", "summary.create") as s:
            s.metadata["model"] = self.summary_model
            try:
                response = await self.client.chat.completions.create(
                    model=self.summary_model,  # ← 用便宜的小模型
                    messages=[
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=self.SUMMARY_MAX_TOKENS,
                    extra_body={"enable_thinking": False},
                    timeout=SUMMARY_TIMEOUT_S,
                )
            except Exception as e:
                logger.warning(f"[摘要] 调用失败：{type(e).__name__}: {e}")
                s.error = f"{type(e).__name__}: {e}"
                return None
            summary = response.choices[0].message.content or ""
            # ← 新增：持久化摘要 + 归档历史消息
            await self.store.upsert_summary(self.session_id, summary)
            keep_count = len(plan["recent"])
            archived_count = await self.store.archive_older_messages(
                self.session_id, keep_recent=keep_count
            )
            logger.info(
                f"[摘要] 持久化完成：摘要已存，归档 {archived_count} 条历史消息"
            )

            s.output = summary[:200]
            usage = getattr(response, "usage", None)
            if usage:
                s.metadata["total_tokens"] = getattr(usage, "total_tokens", 0)
        return summary

    def _rebuild_messages(self, plan: dict, summary: str) -> list[dict]:
        """压缩后的新 messages 结构：原始 system + 摘要 system + 最近消息。"""
        return (
            plan["system_msgs"]
            + [{"role": "system", "content": f"{SUMMARY_TAG} {summary}"}]
            + plan["recent"]
        )

    @staticmethod
    def _hard_trim(plan: dict) -> list[dict]:
        """压缩失败时的兜底结构：原始 system + 旧摘要 + 最近窗口。

        丢的是待压缩的中段历史，保留的信息不至于断档；不写库，
        所以下次启动仍能从 DB 恢复完整历史。
        """
        msgs = list(plan["system_msgs"])
        if plan["previous_summary"]:
            msgs.append(
                {
                    "role": "system",
                    "content": f"{SUMMARY_TAG} {plan['previous_summary']}",
                }
            )
        return msgs + plan["recent"]

    def _shrink_for_overflow(self) -> None:
        """上下文溢出的兜底：按更小的窗口硬截断，system（含摘要）不动，不写库。

        估算器对真实 tokenizer 有偏差，所以除了阈值预判，还要留一条
        以接口真实报错为准的路。
        """
        system_msgs = [m for m in self.messages if m["role"] == "system"]
        other = [m for m in self.messages if m["role"] != "system"]
        kept = _recent_msgs(other, max(1, self.KEEP_RECENT_TOKENS // 2))
        self.messages = system_msgs + kept
        logger.warning(f"[溢出] 已收缩到 {len(self.messages)} 条消息")

    async def _trim_messages(self, trace: Trace) -> None:
        """对话前检查一次，必要时压缩。"""
        plan = self._plan_compaction()
        if plan is None:
            return

        logger.info(
            f"[摘要] 触发压缩: {plan['tokens_before']} tok → "
            f"压缩 {len(plan['to_summarize'])} 条 + 保留 {len(plan['recent'])} 条"
            + ("（增量）" if plan["previous_summary"] else "（首次）")
        )

        summary = await self._compact(plan, trace)
        if summary is None:
            self.messages = self._hard_trim(plan)
            logger.warning(
                f"[摘要] 压缩失败，已降级硬截断到 {len(self.messages)} 条消息"
            )
            return

        self.messages = self._rebuild_messages(plan, summary)

        logger.info(f"[摘要] 压缩完成，现在 {len(self.messages)} 条消息")

    async def _stream_main_call(self, step: TraceStep) -> tuple[str, list[dict] | None]:
        """流式调用主模型，边收边回调 on_token，返回 (正文, tool_calls)。

        用流式是为了把首字延迟暴露出来：思考模式下正文要等思考结束，
        期间只有 reasoning 增量，回调出去才能让调用方给出进度反馈。
        """
        content_parts: list[str] = []
        tool_parts: dict[int, dict] = {}
        usage = None
        ttft_ms: float | None = None
        reasoning_chars = 0
        started = time.perf_counter()

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=get_tool_definitions(),
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
            # extra_body={"enable_thinking": False},
        )
        async for chunk in stream:
            # ─── 临时诊断 ───
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # reasoning_field = getattr(delta, "reasoning_content", None)
            # if reasoning_field and len(reasoning_field) > 0:
            #     logger.warning(
            #         f"[DIAG] 发现 reasoning 字段！长度={len(reasoning_field)}"
            #     )
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_chars += len(reasoning)
                if self.on_token:
                    self.on_token(reasoning, "reasoning")

            if delta.content:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - started) * 1000
                content_parts.append(delta.content)
                if self.on_token:
                    self.on_token(delta.content, "content")

            for tc in delta.tool_calls or []:
                slot = tool_parts.setdefault(
                    tc.index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["function"]["name"] = tc.function.name
                    if tc.function.arguments:
                        slot["function"]["arguments"] += tc.function.arguments

        content = "".join(content_parts)
        tool_calls = [tool_parts[i] for i in sorted(tool_parts)] or None

        # ─── 临时诊断：抓 LLM 响应构成 ───
        logger.warning(
            f"[DIAG] LLM#step{step} "
            f"content={len(content)} 字 "
            f"reasoning={reasoning_chars} 字 "
            f"tool_calls={len(tool_calls) if tool_calls else 0} 个"
        )
        if tool_calls:
            for tc in tool_calls:
                args_preview = tc["function"]["arguments"][:300]
                logger.warning(
                    f"[DIAG]   tool={tc['function']['name']} "
                    f"args_len={len(tc['function']['arguments'])} "
                    f"args={args_preview}"
                )
        if content:
            logger.warning(f"[DIAG]   content_preview={content[:200]}")
        # ─── 诊断结束 ───

        if usage:
            step.metadata["total_tokens"] = usage.total_tokens
            step.metadata["prompt_tokens"] = usage.prompt_tokens
            step.metadata["completion_tokens"] = usage.completion_tokens
        if ttft_ms is not None:
            step.metadata["ttft_ms"] = round(ttft_ms)
        step.metadata["reasoning_chars"] = reasoning_chars
        step.output = {"has_tool_calls": bool(tool_calls), "content_len": len(content)}
        return content, tool_calls

    async def _append_message(self, msg: dict):
        """
        统一入口：append 到内存 + 持久化到 DB。

        注意：user 消息里的图片描述（标记：'【用户上传的图片内容】'）
        只保留在内存给 LLM 看，写入 DB 时剥离掉，避免污染历史。
        """
        self.messages.append(msg)

        # 写 DB 前剥离图片描述
        db_msg = dict(msg)
        content = db_msg.get("content") or ""
        marker = "【用户上传的图片内容】"
        if marker in content:
            db_msg["content"] = content.split(marker)[0].rstrip()

        await self.store.append(self.session_id, db_msg)

    async def _chat(self, user_input: str, image_path: str | None = None):
        trace = self.tracer.new_trace(self.session_id, user_input)
        try:
            result = await self.chat(user_input, trace, image_path=image_path)
            trace.final_output = result
            return result
        except Exception as e:
            logger.error(f"[chat] 异常: {type(e).__name__}: {e}")
            trace.final_output = f"[错误] {type(e).__name__}: {e}"
            return f"抱歉，本次对话出错了：{type(e).__name__}"
        finally:
            try:
                slow_steps = [s for s in trace.steps if s.duration_ms > 10000]
                if slow_steps:
                    logger.warning(
                        f"[SLOW] {len(slow_steps)} 个步骤 >10s: "
                        + ", ".join(
                            f"{s.name}={s.duration_ms/1000:.1f}s" for s in slow_steps
                        )
                    )
                path = self.tracer.save(trace)
                logger.info(f"[trace] {path}")
            except Exception as e:
                logger.error(f"[trace] 保存失败: {e}")

    async def chat(self, user_input: str, trace: Trace, image_path: str | None = None):

        async with trace_step(trace, "memory", "refresh_system_prompt") as s:
            stats = await self._refresh_system_prompt()
            s.metadata.update(stats)
            s.output = stats

        async with trace_step(trace, "message", "user_input", input=user_input) as s:
            user_msg = {"role": "user", "content": user_input}
            if image_path:
                user_msg["image_path"] = image_path
            await self._append_message(user_msg)
            s.output = "ok"
        # 2. 摘要/压缩打点
        async with trace_step(trace, "memory", "trim_messages") as s:
            before = len(self.messages)
            await self._trim_messages(trace)
            s.metadata["message_count"] = len(self.messages)
            s.output = {"before": before, "after": len(self.messages)}
        step = 0
        overflow_retried = False
        while step < self.max_steps:
            step += 1

            # 3. 主 LLM 调用打点（流式：正文边收边回调，思考过程只回调进度）
            try:
                async with trace_step(
                    trace,
                    "llm_call",
                    "chat.completions.create",
                    input={"step": step, "message_count": len(self.messages)},
                ) as s:
                    content, tool_calls = await self._stream_main_call(s)
            except Exception as e:
                if overflow_retried or not self._is_context_overflow(e):
                    raise
                # 估算器可能有偏差，这里用接口的真实报错兜底：收缩后重试一次，
                # 不消耗步数，也不改变"这一轮要跑完"的语义。
                overflow_retried = True
                logger.warning(f"[溢出] {type(e).__name__}：收缩上下文后重试本轮")
                self._shrink_for_overflow()
                step -= 1
                continue

            assistant_msg = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            await self._append_message(assistant_msg)

            if not tool_calls:
                return content

            for tool_call in tool_calls:
                func_name = tool_call["function"]["name"]
                raw_args = tool_call["function"]["arguments"]
                call_id = tool_call["id"]
                async with trace_step(
                    trace,
                    "tool_call",
                    func_name,
                    input=raw_args,
                ) as s:
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError as e:
                        s.error = f"JSONDecodeError: {e}"
                        args = {}
                        await self._append_message(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": f"参数解析失败，请重新生成合法的 JSON。原始内容: {raw_args}",
                            }
                        )
                        continue
                    result = await invoke_tool(func_name, raw_args)
                    s.output = result[:200]
                    if result.startswith(("错误", "参数", "工具执行失败")):
                        s.error = result[:200]

                    s.output = str(result)[:200]
                    await self._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": str(result),
                        }
                    )

        logger.info("Agent: 达到最大循环步数，已强制停止。")

    async def close(self) -> None:
        """关闭数据库连接，触发 aiosqlite 后台线程退出。"""
        if self._db is not None:
            try:
                await self._db.close()
                logger.info("[agent] DB 已关闭")
            except Exception as e:
                logger.error(f"[agent] DB 关闭失败: {e}")
            finally:
                self._db = None


async def main():
    streamed: list[str] = []
    state = {"printing": False, "reasoning_chars": 0}

    def on_token(text: str, kind: str) -> None:
        """把流式增量打到终端；思考阶段只显示字数，不暴露思考内容。"""
        if kind == "reasoning":
            if not state["printing"]:
                state["reasoning_chars"] += len(text)
                print(
                    f"\rAgent: 思考中… {state['reasoning_chars']} 字",
                    end="",
                    flush=True,
                )
            return
        if not state["printing"]:
            print()  # 结束思考进度行
            print("Agent: ", end="", flush=True)
            state["printing"] = True
        streamed.append(text)
        print(text, end="", flush=True)

    agent = Agent(
        "你是一个聊天助手，可以使用工具帮助用户解决问题。",
        "demo-session-1",
        on_token=on_token,
    )
    await agent.init()
    print(f"[DEBUG] QWEATHER_API_HOST = {os.getenv('QWEATHER_API_HOST')!r}")
    print(f"[DEBUG] QWEATHER_PROJECT_ID = {os.getenv('QWEATHER_PROJECT_ID')!r}")
    print(f"[DEBUG] QWEATHER_KEY_ID = {os.getenv('QWEATHER_KEY_ID')!r}")
    print(
        f"[DEBUG] QWEATHER_PRIVATE_KEY_PATH = {os.getenv('QWEATHER_PRIVATE_KEY_PATH')!r}"
    )
    try:
        while True:
            try:
                user_input = input("你：")
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            if user_input.strip().lower() in {"exit", "quit"}:
                print("再见！")
                break

            try:
                response = await agent._chat(user_input)
            except Exception as e:
                logger.error(f"[main] 未捕获异常: {type(e).__name__}: {e}")
                print(f"⚠️ 出错了，但程序继续运行：{e}")
                response = None

            # 一轮里可能调多次模型，最终答案只是最后一轮的正文，
            # 所以判断"是否已经流式打完了"要看后缀而不是全等。
            if response and "".join(streamed).endswith(response):
                print()  # 流式内容已覆盖完整答案，收尾换行即可
            elif response:
                print(f"Agent: {response}")

            streamed.clear()
            state["printing"] = False
            state["reasoning_chars"] = 0
    finally:
        await agent.close()  # ← 关键：无论怎么退出，都关 DB


if __name__ == "__main__":
    asyncio.run(main())
