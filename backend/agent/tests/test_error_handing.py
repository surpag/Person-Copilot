import pytest
from unittest.mock import AsyncMock, MagicMock
from conftest import make_tool_call, make_response


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_to_llm(agent, mock_client):
    """LLM 返回了不存在的工具名，应该把错误喂回去而不是崩溃"""
    bad_call = make_tool_call("call_bad", "nonexistent_tool", "{}")
    mock_client.chat.completions.create.side_effect = [
        make_response(content=None, tool_calls=[bad_call]),
        make_response(content="抱歉，我无法完成这个操作。"),
    ]

    reply = await agent.chat("帮我做点什么")

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "找不到工具" in tool_msg["content"] or "错误" in tool_msg["content"]
    assert reply == "抱歉，我无法完成这个操作。"


@pytest.mark.asyncio
async def test_tool_exception_is_caught(agent, mock_client, mocker):
    """工具内部抛异常时应该被捕获，并作为结果返回给 LLM"""
    broken_tool = mocker.patch.dict(
        "run.available_tools",
        {"get_weather": MagicMock(side_effect=RuntimeError("boom"))},
    )
    call = make_tool_call("call_1", "get_weather", '{"city": "北京"}')
    mock_client.chat.completions.create.side_effect = [
        make_response(content=None, tool_calls=[call]),
        make_response(content="刚才查询失败了。"),
    ]

    reply = await agent.chat("北京天气")
    assert reply == "刚才查询失败了。"

    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "工具执行失败" in tool_msg["content"]
    assert "boom" in tool_msg["content"]


@pytest.mark.asyncio
async def test_malformed_json_arguments(agent, mock_client):
    """arguments 是非法 JSON 时不应崩溃（当前实现会抛错，用来标记待修复点）"""
    bad_call = make_tool_call("call_1", "get_weather", "not-a-json")
    mock_client.chat.completions.create.return_value = make_response(
        content=None, tool_calls=[bad_call]
    )

    # 当前实现会抛 JSONDecodeError —— 这就是要修的地方
    with pytest.raises(Exception):
        await agent.chat("北京天气")


@pytest.mark.asyncio
async def test_max_steps_limit(agent, mock_client):
    """LLM 无限调用工具时，应该在 max_steps 后停下"""
    call = make_tool_call("call_x", "get_current_time", "{}")
    # 让 API 一直返回 tool_calls
    mock_client.chat.completions.create.return_value = make_response(
        content=None, tool_calls=[call]
    )

    reply = await agent.chat("永远调用工具")

    # 循环次数不应该超过 max_steps
    assert mock_client.chat.completions.create.call_count <= agent.max_steps + 1
    # 兜底回复
    assert reply is None or "超时" in (reply or "") or reply == ""
