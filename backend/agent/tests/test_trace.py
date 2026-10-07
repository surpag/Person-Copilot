import json
import os
import sys
import asyncio
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session.trace import Tracer, Trace, TraceStep, trace_step


# ---------- 1. 数据结构 ----------
def test_trace_step_serialization():
    s = TraceStep(
        type="tool_call",
        name="get_weather",
        started_at="2026-01-01T00:00:00",
        duration_ms=123.4,
        input={"city": "北京"},
        output="多云",
    )
    d = s.to_dict()
    assert d["type"] == "tool_call"
    assert d["duration_ms"] == 123.4
    assert d["input"]["city"] == "北京"


def test_trace_totals():
    t = Trace(session_id="s1", turn_id="t1", user_input="hi", started_at="now")
    t.add_step(
        TraceStep(
            type="llm_call",
            name="x",
            started_at="a",
            duration_ms=100,
            metadata={"total_tokens": 50},
        )
    )
    t.add_step(
        TraceStep(
            type="tool_call",
            name="y",
            started_at="b",
            duration_ms=200,
            metadata={"total_tokens": 30},
        )
    )
    assert t.total_duration_ms() == 300
    assert t.total_tokens() == 80


def test_total_duration_uses_wall_clock_not_step_sum():
    """总耗时取墙钟，嵌套 step 不重复计入。"""
    t = Trace(
        session_id="s1",
        turn_id="t1",
        user_input="hi",
        started_at="2026-01-01T00:00:00",
        finished_at="2026-01-01T00:00:10",
    )
    t.add_step(TraceStep(type="memory", name="outer", started_at="a", duration_ms=9000))
    t.add_step(
        TraceStep(
            type="llm_call",
            name="inner",
            started_at="b",
            parent="outer",
            duration_ms=8900,
        )
    )
    assert t.total_duration_ms() == 10000
    assert t.self_time_ms() == 9000
    assert t.to_dict()["self_time_ms"] == 9000


# ---------- 2. 落盘 / 加载 ----------
def test_tracer_save_and_load(tmp_path):
    tracer = Tracer(base_dir=str(tmp_path))
    t = tracer.new_trace("s1", "北京天气")
    t.add_step(
        TraceStep(
            type="tool_call",
            name="get_weather",
            started_at="now",
            duration_ms=42,
            output="多云",
        )
    )
    t.final_output = "多云"

    path = tracer.save(t)
    assert path.exists()

    loaded = tracer.load("s1", t.turn_id)
    assert loaded["user_input"] == "北京天气"
    assert loaded["final_output"] == "多云"
    assert len(loaded["steps"]) == 1
    assert loaded["steps"][0]["name"] == "get_weather"


def test_tracer_list_traces(tmp_path):
    tracer = Tracer(base_dir=str(tmp_path))
    t1 = tracer.new_trace("s1", "u1")
    tracer.save(t1)
    t2 = tracer.new_trace("s1", "u2")
    tracer.save(t2)
    t3 = tracer.new_trace("s2", "u3")
    tracer.save(t3)

    assert len(tracer.list_traces("s1")) == 2
    assert len(tracer.list_traces("s2")) == 1
    assert tracer.list_traces("nope") == []


# ---------- 3. trace_step 行为 ----------
@pytest.mark.asyncio
async def test_trace_step_records_duration():
    tracer = Tracer(base_dir="unused")
    t = tracer.new_trace("s1", "hi")

    async with trace_step(t, "llm_call", "test") as s:
        await asyncio.sleep(0.05)
        s.output = "done"

    assert len(t.steps) == 1
    assert t.steps[0].duration_ms >= 50
    assert t.steps[0].output == "done"
    assert t.steps[0].error is None


@pytest.mark.asyncio
async def test_trace_step_records_error():
    tracer = Tracer(base_dir="unused")
    t = tracer.new_trace("s1", "hi")

    with pytest.raises(ValueError):
        async with trace_step(t, "tool_call", "boom") as s:
            raise ValueError("出错了")

    assert len(t.steps) == 1
    assert "ValueError" in t.steps[0].error
    assert "出错了" in t.steps[0].error


@pytest.mark.asyncio
async def test_trace_step_metadata():
    tracer = Tracer(base_dir="unused")
    t = tracer.new_trace("s1", "hi")

    async with trace_step(t, "llm_call", "x", metadata={"total_tokens": 42}) as s:
        pass

    assert t.steps[0].metadata["total_tokens"] == 42


@pytest.mark.asyncio
async def test_nested_trace_step_records_parent():
    """嵌套的 step 记下父步骤名字，顶层为 None。"""
    tracer = Tracer(base_dir="unused")
    t = tracer.new_trace("s1", "hi")

    async with trace_step(t, "memory", "outer"):
        async with trace_step(t, "llm_call", "inner"):
            pass

    assert {s.name: s.parent for s in t.steps} == {"outer": None, "inner": "outer"}
