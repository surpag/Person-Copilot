import pytest
from conftest import make_tool_call, make_response


@pytest.mark.asyncio
async def test_multi_turn_memory(agent, mock_client):
    """多轮对话应保留上下文"""
    mock_client.chat.completions.create.side_effect = [
        make_response(content="北京多云。"),
        make_response(content="上海雨天。"),
        make_response(content="你刚才问了北京和上海的天气。"),
    ]

    await agent.chat("北京天气")
    await agent.chat("上海天气")
    reply = await agent.chat("我刚才问了哪些城市？")

    assert "北京" in reply and "上海" in reply
    # 应该有 1 个 system + 3 个 user + 3 个 assistant
    roles = [m["role"] for m in agent.messages]
    assert roles.count("user") == 3
    assert roles.count("assistant") == 3


@pytest.mark.asyncio
async def test_history_passed_to_llm(agent, mock_client):
    """第二次请求时，应该带上完整历史"""
    mock_client.chat.completions.create.side_effect = [
        make_response(content="好的。"),
        make_response(content="好的，再记一次。"),
    ]

    await agent.chat("我喜欢喝美式")
    await agent.chat("记住这件事")

    # 检查第二次 API 调用时 messages 里有没有 "我喜欢喝美式"
    second_call = mock_client.chat.completions.create.call_args_list[1]
    sent_messages = second_call.kwargs["messages"]
    contents = " ".join(str(m.get("content", "")) for m in sent_messages)
    assert "美式" in contents


@pytest.mark.asyncio
async def test_unknown_city_returns_fallback(agent, mock_client):
    """工具返回 fallback 时，LLM 应该看到这条信息"""
    call = make_tool_call("call_1", "get_weather", '{"city": "火星"}')
    mock_client.chat.completions.create.side_effect = [
        make_response(content=None, tool_calls=[call]),
        make_response(content="抱歉，查不到火星的天气。"),
    ]

    reply = await agent.chat("火星天气")
    assert "抱歉" in reply or "查不到" in reply

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "火星" in tool_msg["content"]
