"""流式主调用 / 压缩失败降级 的单元测试。

这两个路径都是"别让用户干等"的防线，用假 client 覆盖，不依赖网络。
"""

import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import Agent  # noqa: E402
from session.trace import Tracer, TraceStep  # noqa: E402


def _chunk(content=None, reasoning=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls
    )
    choices = [] if usage is not None and content is None and not tool_calls else [
        SimpleNamespace(delta=delta)
    ]
    return SimpleNamespace(choices=choices, usage=usage)


def _tc(index, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def _gen(self):
        for c in self._chunks:
            yield c

    def __aiter__(self):
        return self._gen()


def _client(create):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


@pytest.mark.asyncio
async def test_stream_main_call_assembles_content_and_tool_calls():
    """流式分片要能拼回正文、reasoning 计数、tool_calls 和 usage。"""

    async def create(**kwargs):
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        return _FakeStream(
            [
                _chunk(reasoning="先想一下"),
                _chunk(
                    tool_calls=[_tc(0, id="call_1", name="get_weather", arguments='{"city": ')]
                ),
                _chunk(tool_calls=[_tc(0, arguments='"南京"}')]),
                _chunk(content="南京", usage=None),
                _chunk(content="晴"),
                _chunk(
                    usage=SimpleNamespace(
                        total_tokens=42, prompt_tokens=30, completion_tokens=12
                    )
                ),
            ]
        )

    agent = Agent("测试", "s1")
    agent.client = _client(create)
    seen = []
    agent.on_token = lambda text, kind: seen.append((kind, text))
    step = TraceStep(type="llm_call", name="chat.completions.create", started_at="now")

    content, tool_calls = await agent._stream_main_call(step)

    assert content == "南京晴"
    assert tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "南京"}'},
        }
    ]
    assert step.metadata["total_tokens"] == 42
    assert step.metadata["prompt_tokens"] == 30
    assert step.metadata["completion_tokens"] == 12
    assert step.metadata["reasoning_chars"] == len("先想一下")
    assert step.metadata["ttft_ms"] >= 0
    assert [s for s in seen if s[0] == "content"] == [
        ("content", "南京"),
        ("content", "晴"),
    ]


@pytest.mark.asyncio
async def test_compaction_failure_falls_back_to_hard_trim():
    """摘要超时不能让整轮卡死：降级为硬截断，保留 system + 最近窗口。"""

    async def create(**kwargs):
        raise APITimeoutError(request=httpx.Request("POST", "https://example.com"))

    agent = Agent("你是一个助手", "s2")
    agent.client = _client(create)
    agent.messages = [{"role": "system", "content": "你是一个助手"}]
    for i in range(20):
        agent.messages.append({"role": "user", "content": f"第{i}个问题" + "补" * 400})
        agent.messages.append({"role": "assistant", "content": f"第{i}个回答" + "答" * 400})

    trace = Tracer(base_dir="unused").new_trace("s2", "触发压缩")
    before = list(agent.messages)

    await agent._trim_messages(trace)

    assert len(agent.messages) < len(before)
    assert agent.messages[0]["content"] == "你是一个助手"
    assert agent.messages[1:] == before[-len(agent.messages) + 1 :]
    summary_step = next(s for s in trace.steps if s.name == "summary.create")
    assert "APITimeoutError" in summary_step.error
