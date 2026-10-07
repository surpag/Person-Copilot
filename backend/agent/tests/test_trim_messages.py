"""上下文压缩（Compaction）测试。

覆盖：
1. Token 估算
2. Turn 切分
3. 保留策略（保证 tool 配对）
4. 压缩触发条件
5. 压缩后结构
6. 增量摘要
"""

import pytest
from conftest import make_response

# 从 run.py 引入纯函数
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run import (
    _estimate_tokens,
    _estimate_tokens_full,
    _split_into_turns,
    _recent_msgs,
    SUMMARY_TAG,
)


# ============ 1. Token 估算 ============
class TestEstimateTokens:
    def test_empty(self):
        assert _estimate_tokens([]) == 0

    def test_short(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert _estimate_tokens(msgs) == 1

    def test_tool_calls_counted(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": '{"a": 1}'},
                    }
                ],
            }
        ]
        assert _estimate_tokens(msgs) > 0

    def test_multiple_messages(self):
        msgs = [
            {"role": "user", "content": "abcd"},  # 4 // 2 = 2
            {"role": "assistant", "content": "12345678"},  # 8 // 2 = 4
        ]
        assert _estimate_tokens(msgs) == 6


# ============ 2. Turn 切分 ============
class TestSplitIntoTurns:
    def test_no_tool(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        turns = _split_into_turns(msgs)
        assert len(turns) == 3
        assert [t[0]["content"] for t in turns] == ["a", "b", "c"]

    def test_tool_joined_with_assistant(self):
        msgs = [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "多云"},
            {"role": "assistant", "content": "北京多云"},
        ]
        turns = _split_into_turns(msgs)
        assert len(turns) == 3
        # 中间 turn 应该是 assistant + tool 两条
        assert len(turns[1]) == 2
        assert turns[1][0]["role"] == "assistant"
        assert turns[1][1]["role"] == "tool"

    def test_multiple_tools_in_one_turn(self):
        msgs = [
            {"role": "user", "content": "天气+时间"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1"}, {"id": "c2"}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "多云"},
            {"role": "tool", "tool_call_id": "c2", "content": "10:00"},
            {"role": "assistant", "content": "结果"},
        ]
        turns = _split_into_turns(msgs)
        assert len(turns) == 3
        assert len(turns[1]) == 3  # assistant + 2 个 tool


# ============ 3. 保留策略 ============
class TestSafeRecentMsgs:
    def test_empty(self):
        assert _recent_msgs([], 100) == []

    def test_keeps_at_least_last_turn(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        recent = _recent_msgs(msgs, keep_tokens=0)
        # 预算为 0，也要保留最后一个 turn
        assert len(recent) >= 1
        assert recent[-1]["content"] == "b"

    def test_never_breaks_tool_pair(self):
        """关键测试：tool 消息不会孤立出现在 recent 头部"""
        msgs = [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "多云"},
            {"role": "assistant", "content": "北京多云"},
            {"role": "user", "content": "上海呢"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "雨天"},
        ]
        recent = _recent_msgs(msgs, keep_tokens=30)
        # 检查：任何 tool 消息前面必须紧跟 assistant 或 tool
        for i, m in enumerate(recent):
            if m["role"] == "tool":
                assert i > 0, "recent 里出现了孤立的 tool 消息"
                assert recent[i - 1]["role"] in ("assistant", "tool")

    def test_budget_respected(self):
        """预算够大时应该保留更多"""
        msgs = []
        for i in range(10):
            msgs.append({"role": "user", "content": f"用户消息{i}" * 10})
            msgs.append({"role": "assistant", "content": f"回复{i}" * 10})

        small = _recent_msgs(msgs, keep_tokens=20)
        big = _recent_msgs(msgs, keep_tokens=200)

        assert len(big) > len(small)


# ============ 4. 压缩触发条件 ============
class TestPlanCompaction:
    def test_no_compaction_when_short(self, agent):
        agent.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "你好"},
        ]
        assert agent._plan_compaction() is None

    def test_triggered_when_over_budget(self, agent):
        agent.MAX_ACTIVE_TOKENS = 100
        agent.KEEP_RECENT_TOKENS = 20
        agent.messages = [{"role": "system", "content": "sys"}]
        for i in range(20):
            agent.messages.append({"role": "user", "content": f"用户{i}" * 20})
            agent.messages.append({"role": "assistant", "content": f"回复{i}"})

        plan = agent._plan_compaction()
        assert plan is not None
        assert len(plan["to_summarize"]) > 0
        assert len(plan["recent"]) > 0
        assert plan["tokens_before"] > agent.MAX_ACTIVE_TOKENS
        assert plan["previous_summary"] is None

    def test_fixed_overhead_does_not_trigger_compaction(self, agent):
        """工具定义 / persona / 摘要属于压缩缩不掉的固定开销，不该参与触发判断。

        回归用例：这类开销曾把"全量口径"顶过阈值，而压缩又缩不掉它们，
        于是压缩完仍然超标 → 每轮都触发压缩。
        """
        agent.MAX_ACTIVE_TOKENS = 1000
        agent.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "system", "content": f"{SUMMARY_TAG} 旧摘要"},
            {"role": "user", "content": "现在几点"},
            {"role": "assistant", "content": "现在是 10 点"},
        ]
        # 全量口径（含工具定义）已经超过预算——老实现就是在这里误判的
        assert _estimate_tokens_full(agent.messages) > agent.MAX_ACTIVE_TOKENS
        # 但真正可压缩的活跃部分远没超过，不该压缩
        assert agent._plan_compaction() is None

    def test_previous_summary_detected(self, agent):
        agent.MAX_ACTIVE_TOKENS = 50
        agent.KEEP_RECENT_TOKENS = 10
        agent.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "system", "content": f"{SUMMARY_TAG} 旧摘要内容"},
        ] + [{"role": "user", "content": f"消息{i}" * 20} for i in range(10)]

        plan = agent._plan_compaction()
        assert plan is not None
        assert plan["previous_summary"] == "旧摘要内容"
        # 摘要消息本身不应该出现在 to_summarize 里
        for m in plan["to_summarize"]:
            assert not (m["role"] == "system" and m["content"].startswith(SUMMARY_TAG))


# ============ 5. 压缩后结构 ============
class TestTrimMessagesEndToEnd:
    @pytest.mark.asyncio
    async def test_full_flow(self, agent, mock_client):
        """端到端：触发压缩 → 生成摘要 → 重建 messages"""
        agent.MAX_ACTIVE_TOKENS = 100
        agent.KEEP_RECENT_TOKENS = 30

        agent.messages = [{"role": "system", "content": "你是助手"}]
        for i in range(20):
            agent.messages.append({"role": "user", "content": f"用户消息{i}" * 10})
            agent.messages.append({"role": "assistant", "content": f"回复{i}"})

        # mock LLM 返回摘要
        mock_client.chat.completions.create.return_value = make_response(
            content="## 目标\n完成用户任务\n## 已完成\n- [x] 处理了很多消息"
        )

        from session.trace import Tracer

        trace = Tracer("unused").new_trace("s1", "test")
        await agent._trim_messages(trace)

        # 结构：system(原始) + system(摘要) + recent...
        assert agent.messages[0]["role"] == "system"
        assert agent.messages[0]["content"] == "你是助手"
        assert agent.messages[1]["role"] == "system"
        assert agent.messages[1]["content"].startswith(SUMMARY_TAG)
        assert "完成用户任务" in agent.messages[1]["content"]

    @pytest.mark.asyncio
    async def test_incremental_summary_uses_update_prompt(self, agent, mock_client):
        """有上次摘要时，应该用 UPDATE_PROMPT 而不是 INITIAL_PROMPT"""
        agent.MAX_ACTIVE_TOKENS = 50
        agent.KEEP_RECENT_TOKENS = 10
        agent.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "system", "content": f"{SUMMARY_TAG} 旧摘要"},
        ] + [{"role": "user", "content": f"消息{i}" * 20} for i in range(10)]

        captured_prompt: list[str] = []

        def side_effect(*args, **kwargs):
            captured_prompt.append(kwargs["messages"][1]["content"])
            return make_response(content="新摘要")

        mock_client.chat.completions.create.side_effect = side_effect

        from session.trace import Tracer

        trace = Tracer("unused").new_trace("s1", "test")
        await agent._trim_messages(trace)

        # 校验用的是 update prompt（包含"上一份摘要"）
        assert len(captured_prompt) == 1
        assert "上一份摘要" in captured_prompt[0]
        assert "旧摘要" in captured_prompt[0]
