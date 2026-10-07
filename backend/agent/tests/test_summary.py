"""
摘要压缩（Summary Memory）测试集。

覆盖：
1. 短对话不触发摘要
2. 长对话触发摘要
3. 摘要 prompt 正确
4. 摘要后保留 system 消息
5. 摘要后保留最近消息
6. 摘要失败时优雅降级
7. 摘要上下文溢出时截断重试
8. 摘要重试耗尽后依然能继续
"""

import pytest
from conftest import make_response


# ============ 辅助函数 ============
def _seed_history(agent, turns: int):
    """往 agent.messages 里塞 turns 轮 user/assistant 历史"""
    for i in range(turns):
        agent.messages.append({"role": "user", "content": f"用户第{i}轮"})
        agent.messages.append({"role": "assistant", "content": f"助手第{i}轮"})


def _is_summary_call(call) -> bool:
    """判断一次 API 调用是不是摘要调用（通过 max_tokens=500 区分）"""
    return call.kwargs.get("max_tokens") == 500


def _is_main_call(call) -> bool:
    return not _is_summary_call(call)


# ============ 1. 触发条件 ============
@pytest.mark.asyncio
async def test_no_summary_when_short(agent, mock_client):
    """Arrange: 消息数未超阈值 | Act: 对话一次 | Assert: 不应触发摘要"""
    mock_client.chat.completions.create.return_value = make_response(content="你好")

    await agent.chat("你好")

    # 只有 1 次调用（主对话），没有摘要调用
    assert mock_client.chat.completions.create.call_count == 1
    assert _is_main_call(mock_client.chat.completions.create.call_args_list[0])


@pytest.mark.asyncio
async def test_summary_triggered_when_long(agent, mock_client):
    """消息超阈值时应触发摘要"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)  # 10 条历史 + 1 条 system = 11 条

    mock_client.chat.completions.create.side_effect = [
        make_response(content="这是压缩后的摘要"),  # 摘要调用
        make_response(content="最终回复"),  # 主对话
    ]

    await agent.chat("继续聊")

    # 应该至少有 2 次调用：1 摘要 + 1 主对话
    assert mock_client.chat.completions.create.call_count >= 2
    # 第一次是摘要调用
    assert _is_summary_call(mock_client.chat.completions.create.call_args_list[0])


# ============ 2. 摘要调用的参数 ============
@pytest.mark.asyncio
async def test_summary_uses_correct_prompt(agent, mock_client):
    """摘要调用应使用专门的 prompt，且带上历史文本"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    mock_client.chat.completions.create.side_effect = [
        make_response(content="摘要内容"),
        make_response(content="好的"),
    ]

    await agent.chat("继续")

    summary_call = mock_client.chat.completions.create.call_args_list[0]
    sent = summary_call.kwargs["messages"]

    assert sent[0]["role"] == "system"
    assert "摘要" in sent[0]["content"]

    assert sent[1]["role"] == "user"
    assert "请摘要以下对话" in sent[1]["content"]
    # 历史文本应被拼进 user content
    assert "用户第0轮" in sent[1]["content"]


# ============ 3. 摘要后的结构 ============
@pytest.mark.asyncio
async def test_summary_keeps_system_message(agent, mock_client):
    """摘要后，原始 system prompt 必须保留"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    mock_client.chat.completions.create.side_effect = [
        make_response(content="摘要内容"),
        make_response(content="好的"),
    ]

    await agent.chat("继续")

    system_msgs = [m for m in agent.messages if m["role"] == "system"]
    # 至少有 2 条 system：原始 prompt + 摘要
    assert len(system_msgs) >= 2
    # 一条是摘要，一条是原始 prompt
    assert any("摘要" in m["content"] for m in system_msgs)
    assert any("聊天助手" in m["content"] for m in system_msgs)


@pytest.mark.asyncio
async def test_summary_keeps_recent_messages(agent, mock_client):
    """摘要后应保留最近几条原始消息（不能全丢）"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    mock_client.chat.completions.create.side_effect = [
        make_response(content="摘要内容"),
        make_response(content="好的"),
    ]

    await agent.chat("继续")

    # 刚 append 的 user 消息"继续"必须还在
    user_msgs = [m for m in agent.messages if m["role"] == "user"]
    assert any(m.get("content") == "继续" for m in user_msgs)


@pytest.mark.asyncio
async def test_summary_reduces_message_count(agent, mock_client):
    """摘要后的消息数应明显减少"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    original_count = len(agent.messages)
    assert original_count > 10  # 确认一开始就超阈值

    mock_client.chat.completions.create.side_effect = [
        make_response(content="摘要内容"),
        make_response(content="好的"),
    ]

    await agent.chat("继续")

    # 压缩后：system + 摘要 system + 最近 2 条 + 本次 user/assistant
    # 应该远小于原始数量
    assert len(agent.messages) < original_count


# ============ 4. 异常路径 ============
@pytest.mark.asyncio
async def test_summary_failure_falls_back(agent, mock_client):
    """摘要调用抛普通异常时，不应崩溃，应降级"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    def side_effect(*args, **kwargs):
        if kwargs.get("max_tokens") == 500:
            raise RuntimeError("摘要 API 挂了")
        return make_response(content="降级后的回复")

    mock_client.chat.completions.create.side_effect = side_effect

    reply = await agent.chat("继续")

    assert reply == "降级后的回复"
    # 原始 system prompt 依然存在
    assert any(
        "聊天助手" in m["content"] for m in agent.messages if m["role"] == "system"
    )


@pytest.mark.asyncio
async def test_summary_retries_on_context_overflow(agent, mock_client):
    """摘要本身超长时，应截断重试"""
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    summary_call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        if kwargs.get("max_tokens") == 500:
            summary_call_count["n"] += 1
            if summary_call_count["n"] == 1:
                # 第一次模拟上下文溢出
                raise Exception("maximum context length exceeded")
            # 第二次成功
            return make_response(content="截断后的摘要")
        return make_response(content="主对话回复")

    mock_client.chat.completions.create.side_effect = side_effect

    reply = await agent.chat("继续")

    # 摘要至少被调用了 2 次（第一次失败 + 第二次成功）
    assert summary_call_count["n"] >= 2
    assert reply == "主对话回复"


@pytest.mark.asyncio
async def test_summary_gives_up_after_max_retries(agent, mock_client):
    """摘要重试耗尽后，应该继续完成主对话（不卡死）"""
    agent.MAX_MESSAGES = 4
    agent.SUMMARY_MAX_RETRIES = 2
    _seed_history(agent, 5)

    summary_calls = {"n": 0}

    def side_effect(*args, **kwargs):
        if kwargs.get("max_tokens") == 500:
            summary_calls["n"] += 1
            raise Exception("maximum context length exceeded")
        return make_response(content="仍然能回复")

    mock_client.chat.completions.create.side_effect = side_effect

    reply = await agent.chat("继续")

    # 摘要尝试次数不超过 SUMMARY_MAX_RETRIES
    assert summary_calls["n"] <= agent.SUMMARY_MAX_RETRIES
    # 主对话仍然完成了
    assert reply == "仍然能回复"


# ============ 5. 回归测试：await 遗漏 ============
@pytest.mark.asyncio
async def test_trim_messages_must_await_api(agent, mock_client):
    """
    回归测试：_trim_messages 里的 API 调用必须是 await 的。

    如果忘了写 await，create() 返回的是 coroutine，不会被真正执行，
    AsyncMock 就不会记录这次调用（call_args_list 里看不到摘要调用）。
    """
    agent.MAX_MESSAGES = 4
    _seed_history(agent, 5)

    mock_client.chat.completions.create.side_effect = [
        make_response(content="摘要"),
        make_response(content="主对话"),
    ]

    await agent.chat("继续")

    summary_calls = [
        c
        for c in mock_client.chat.completions.create.call_args_list
        if c.kwargs.get("max_tokens") == 500
    ]
    assert (
        len(summary_calls) >= 1
    ), "摘要 API 没有被真正 await 执行 —— 请检查 _trim_messages 里是否漏了 await"
