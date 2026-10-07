"""轻量级追踪模块。

- 一次用户输入 → 一个 Trace
- Trace 里记录若干 TraceStep（LLM 调用 / 工具执行 / DB 写入 / 摘要 等）
- 落盘到 traces/{session_id}/{turn_id}.json
"""

import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class TraceStep:
    type: str  # "llm_call" / "tool_call" / "memory" / "message"
    name: str  # "chat.completions.create" / "get_weather" ...
    started_at: str
    parent: str | None = None  # 父 step 的名字；None 表示顶层
    duration_ms: float = 0.0
    input: Any = None
    output: Any = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trace:
    session_id: str
    turn_id: str
    user_input: str
    started_at: str
    steps: list[TraceStep] = field(default_factory=list)
    finished_at: str | None = None
    final_output: str | None = None

    def add_step(self, step: TraceStep) -> None:
        self.steps.append(step)

    def total_duration_ms(self) -> float:
        """端到端墙钟耗时。

        step 之间是嵌套关系（如 summary.create 跑在 trim_messages 里），
        求和会把嵌套的时长重复计入，所以这里只认 started_at / finished_at。
        未 finish（仍在跑或手工构造）时退回顶层 step 之和。
        """
        if not self.finished_at:
            return self.self_time_ms()
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.fromisoformat(self.finished_at)
        return (finished - started).total_seconds() * 1000

    def self_time_ms(self) -> float:
        """顶层 step 耗时之和（嵌套子 step 不重复计入）。

        与 total_duration_ms 的差额 = 未打点时间（DB 写入、保存、循环间隙等）。
        """
        return sum(s.duration_ms for s in self.steps if s.parent is None)

    def total_tokens(self) -> int:
        return sum(s.metadata.get("total_tokens", 0) for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "user_input": self.user_input,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "final_output": self.final_output,
            "total_duration_ms": self.total_duration_ms(),
            "self_time_ms": self.self_time_ms(),
            "total_tokens": self.total_tokens(),
            "steps": [s.to_dict() for s in self.steps],
        }


class Tracer:
    """负责创建 Trace 和落盘/加载。"""

    def __init__(self, base_dir: str = "traces") -> None:
        self.base_dir = Path(base_dir)

    def new_trace(self, session_id: str, user_input: str) -> Trace:
        return Trace(
            session_id=session_id,
            turn_id=str(uuid.uuid4())[:8],
            user_input=user_input,
            started_at=datetime.now().isoformat(),
        )

    def save(self, trace: Trace) -> Path:
        trace.finished_at = datetime.now().isoformat()
        dir_path = self.base_dir / trace.session_id
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{trace.turn_id}.json"
        file_path.write_text(
            json.dumps(trace.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return file_path

    def load(self, session_id: str, turn_id: str) -> dict:
        file_path = self.base_dir / session_id / f"{turn_id}.json"
        return json.loads(file_path.read_text(encoding="utf-8"))

    def list_traces(self, session_id: str) -> list[str]:
        dir_path = self.base_dir / session_id
        if not dir_path.exists():
            return []
        return sorted(p.stem for p in dir_path.glob("*.json"))


# 当前正在执行的 step（同一 task 内嵌套调用时用于标记父子关系）
_current_step: ContextVar[TraceStep | None] = ContextVar("current_step", default=None)


@asynccontextmanager
async def trace_step(
    trace: Trace,
    step_type: str,
    name: str,
    input: Any = None,
    metadata: dict | None = None,
):
    """在 async with 里使用，自动记录耗时和异常。

    用法：
        async with trace_step(trace, "tool_call", "get_weather", input=args) as s:
            result = get_weather(**args)
            s.output = result
    """
    started = time.perf_counter()
    parent = _current_step.get()
    step = TraceStep(
        type=step_type,
        name=name,
        started_at=datetime.now().isoformat(),
        parent=parent.name if parent else None,
        input=input,
        metadata=metadata or {},
    )
    token = _current_step.set(step)
    try:
        yield step
    except Exception as e:
        step.error = f"{e.__class__.__name__}: {e}"
        raise
    finally:
        step.duration_ms = (time.perf_counter() - started) * 1000
        _current_step.reset(token)
        trace.add_step(step)
