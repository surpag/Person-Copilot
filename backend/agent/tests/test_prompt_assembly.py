"""Prompt 组装测试。

覆盖：
- 纯函数 build_system_prompt 各分支
- 截断策略
- Agent 集成：init 后 messages[0] 包含偏好和待办
- 刷新后 system prompt 更新
"""

import os
import sys

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prompt import DEFAULT_TOOL_RULES, build_system_prompt

# ============================================================
# 1. 纯函数：build_system_prompt
# ============================================================


class TestBuildSystemPrompt:
    def test_persona_only(self):
        result = build_system_prompt("你是助手")
        assert result.startswith("你是助手")
        assert DEFAULT_TOOL_RULES in result
        assert "已知用户偏好" not in result
        assert "当前待办" not in result

    def test_with_preferences(self):
        result = build_system_prompt(
            "你是助手",
            preferences={"city": "南京", "name": "小王"},
        )
        assert "## 已知用户偏好" in result
        assert "- city: 南京" in result
        assert "- name: 小王" in result

    def test_with_todos(self):
        result = build_system_prompt(
            "你是助手",
            todos=[
                {"id": 1, "content": "买牛奶", "due": None},
                {"id": 2, "content": "开会", "due": "2026-09-20"},
            ],
        )
        assert "## 当前待办（2 条未完成）" in result
        assert "- #1 买牛奶" in result
        assert "- #2 开会（截止：2026-09-20）" in result

    def test_with_both(self):
        result = build_system_prompt(
            "你是助手",
            preferences={"city": "南京"},
            todos=[{"id": 1, "content": "买牛奶", "due": None}],
        )
        # 顺序：persona → 偏好 → 待办 → 规则
        assert result.index("你是助手") < result.index("已知用户偏好")
        assert result.index("已知用户偏好") < result.index("当前待办")
        assert result.index("当前待办") < result.index("工具使用规则")

    def test_empty_preferences_dict_no_section(self):
        result = build_system_prompt("你是助手", preferences={})
        assert "已知用户偏好" not in result

    def test_empty_todos_list_no_section(self):
        result = build_system_prompt("你是助手", todos=[])
        assert "当前待办" not in result

    def test_truncates_preferences(self):
        prefs = {f"k{i}": f"v{i}" for i in range(60)}
        result = build_system_prompt("你是助手", preferences=prefs, max_prefs=10)
        # 只有 10 条列出，最后提示还有 50 条
        assert "k0" in result
        assert "k9" in result
        assert "k10" not in result
        assert "还有 50 条未显示" in result

    def test_truncates_todos(self):
        todos = [{"id": i, "content": f"任务{i}", "due": None} for i in range(30)]
        result = build_system_prompt("你是助手", todos=todos, max_todos=5)
        assert "#0" in result
        assert "#4" in result
        assert "#5" not in result
        assert "还有 25 条未列出" in result

    def test_custom_tool_rules(self):
        result = build_system_prompt("你是助手", tool_rules="## 自定义规则\n- 测试")
        assert "自定义规则" in result
        assert DEFAULT_TOOL_RULES not in result

    def test_empty_tool_rules_no_section(self):
        result = build_system_prompt("你是助手", tool_rules="")
        assert "工具使用规则" not in result

    def test_persona_stripped(self):
        result = build_system_prompt("  你是助手  ")
        assert result.startswith("你是助手")

    def test_no_duplicate_blank_lines(self):
        result = build_system_prompt(
            "你是助手",
            preferences={"a": "1"},
            todos=[{"id": 1, "content": "x", "due": None}],
        )
        # 不应出现连续 3 个以上换行
        assert "\n\n\n" not in result


# ============================================================
# 2. Agent 集成：init 后 messages[0] 被组装
# ============================================================


@pytest_asyncio.fixture
async def initialized_agent(mock_client, tmp_path):
    """构造一个完整初始化的 Agent（使用 tmp DB）"""
    from run import Agent

    agent = Agent("你是一个个人助理", "test-session")
    await agent.init(db_path=str(tmp_path / "test.db"))
    yield agent


class TestAgentIntegration:
    @pytest.mark.asyncio
    async def test_init_assembles_system_prompt(self, initialized_agent):
        agent = initialized_agent
        assert agent.messages[0]["role"] == "system"
        content = agent.messages[0]["content"]
        assert "你是一个个人助理" in content
        assert "工具使用规则" in content

    @pytest.mark.asyncio
    async def test_preferences_injected(self, initialized_agent):
        agent = initialized_agent
        await agent._prefs.remember("city", "南京")
        await agent._refresh_system_prompt()

        content = agent.messages[0]["content"]
        assert "city: 南京" in content

    @pytest.mark.asyncio
    async def test_todos_injected(self, initialized_agent):
        agent = initialized_agent
        await agent._todos.add("买牛奶")
        await agent._refresh_system_prompt()

        content = agent.messages[0]["content"]
        assert "买牛奶" in content
        assert "1 条未完成" in content

    @pytest.mark.asyncio
    async def test_refresh_returns_stats(self, initialized_agent):
        agent = initialized_agent
        await agent._prefs.remember("a", "1")
        await agent._prefs.remember("b", "2")
        await agent._todos.add("任务")

        stats = await agent._refresh_system_prompt()
        assert stats["skipped"] is False
        assert stats["pref_count"] == 2
        assert stats["todo_count"] == 1

    @pytest.mark.asyncio
    async def test_refresh_before_db_returns_skipped(self, mock_client):
        """没 init 就刷新，应该静默跳过"""
        from run import Agent

        agent = Agent("你是助手", "s1")
        stats = await agent._refresh_system_prompt()
        assert stats["skipped"] is True

    @pytest.mark.asyncio
    async def test_prefs_and_todos_updated_between_turns(self, initialized_agent):
        """模拟：用户先记住，再刷新，system prompt 应该包含"""
        agent = initialized_agent

        # 第一次刷新，没偏好
        await agent._refresh_system_prompt()
        content1 = agent.messages[0]["content"]
        assert "已知用户偏好" not in content1

        # 用户记住一条
        await agent._prefs.remember("name", "小王")

        # 第二次刷新
        await agent._refresh_system_prompt()
        content2 = agent.messages[0]["content"]
        assert "name: 小王" in content2
