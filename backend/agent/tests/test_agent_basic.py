import pytest
from conftest import make_tool_call, make_response


@pytest.mark.asyncio
async def test_simple_reply_no_tool(agent, mock_client):
    """LLM 直接回答，不调用工具"""
    mock_client.chat.completions.create.return_value = make_response(
        content="你好，我是助手。"
    )

    reply = await agent.chat("你好")

    assert reply == "你好，我是助手。"
    # messages 里应该有 system / user / assistant 三条
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_single_tool_call(agent, mock_client):
    """LLM 调用一次 get_weather，然后给出最终回复"""
    call = make_tool_call("call_1", "get_weather", '{"city": "北京"}')

    mock_client.chat.completions.create.side_effect = [
        make_response(content=None, tool_calls=[call]),
        make_response(content="北京今天多云。"),
    ]

    reply = await agent.chat("北京天气怎么样？")

    assert reply == "北京今天多云。"
    # 验证工具消息被写回
    roles = [m["role"] for m in agent.messages]
    assert "tool" in roles
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    assert "多云" in tool_msg["content"]


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_turn(agent, mock_client):
    """LLM 一次调用多个工具"""
    call_time = make_tool_call("call_t", "get_current_time", "{}")
    call_weather = make_tool_call("call_w", "get_weather", '{"city": "上海"}')

    mock_client.chat.completions.create.side_effect = [
        make_response(content=None, tool_calls=[call_time, call_weather]),
        make_response(content="现在时间和上海天气都查到了。"),
    ]

    reply = await agent.chat("现在几点？上海天气？")

    assert "时间和上海" in reply
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    ids = {m["tool_call_id"] for m in tool_msgs}
    assert ids == {"call_t", "call_w"}
