"""Web 搜索工具测试（mock Tavily 客户端）。"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.registry import invoke_tool
from tools import web_search as ws_module  # 触发注册

# ============================================================
# fake 响应
# ============================================================

_FAKE_OK = {
    "answer": "2026 年 AI Agent 的主流方向是多模态和长上下文。",
    "results": [
        {
            "title": "AI Agent 2026 综述",
            "url": "https://example.com/a",
            "content": "本文综述了 Agent 的核心组件：规划、记忆、工具调用……",
        },
        {
            "title": "Agent Memory 最新进展",
            "url": "https://example.com/b",
            "content": "向量检索 + 摘要压缩是目前主流方案。",
        },
    ],
}

_FAKE_EMPTY = {"answer": None, "results": []}


# ============================================================
# fixtures
# ============================================================


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fake")


def _mock_client(mocker, response):
    """mock AsyncTavilyClient.search 返回给定响应。"""
    fake = MagicMock()
    fake.search = AsyncMock(return_value=response)
    mocker.patch("tools.web_search._get_client", return_value=fake)
    return fake


# ============================================================
# 格式化（纯函数）
# ============================================================


class TestFormat:
    def test_full_result(self):
        out = ws_module._format_results("q", _FAKE_OK)
        assert "💡 摘要" in out
        assert "多模态" in out
        assert "AI Agent 2026 综述" in out
        assert "https://example.com/a" in out

    def test_empty_results(self):
        out = ws_module._format_results("q", _FAKE_EMPTY)
        assert "📭" in out

    def test_truncates_long_content(self):
        resp = {
            "answer": None,
            "results": [
                {
                    "title": "T",
                    "url": "u",
                    "content": "x" * 1000,
                }
            ],
        }
        out = ws_module._format_results("q", resp)
        assert "…" in out  # 被截断
        assert "x" * 1000 not in out


# ============================================================
# 工具调用
# ============================================================


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_success(self, mocker):
        _mock_client(mocker, _FAKE_OK)
        result = await invoke_tool("web_search", '{"query": "AI Agent"}')
        assert "💡 摘要" in result
        assert "AI Agent 2026 综述" in result

    @pytest.mark.asyncio
    async def test_empty_query(self, mocker):
        _mock_client(mocker, _FAKE_OK)
        result = await invoke_tool("web_search", '{"query": "   "}')
        assert "不能为空" in result

    @pytest.mark.asyncio
    async def test_empty_results(self, mocker):
        _mock_client(mocker, _FAKE_EMPTY)
        result = await invoke_tool("web_search", '{"query": "asdfghjkl"}')
        assert "📭" in result

    @pytest.mark.asyncio
    async def test_api_error(self, mocker):
        fake = MagicMock()
        fake.search = AsyncMock(side_effect=Exception("rate limited"))
        mocker.patch("tools.web_search._get_client", return_value=fake)
        result = await invoke_tool("web_search", '{"query": "test"}')
        assert "❌" in result
        assert "搜索失败" in result

    @pytest.mark.asyncio
    async def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        result = await invoke_tool("web_search", '{"query": "test"}')
        assert "未配置" in result

    @pytest.mark.asyncio
    async def test_max_results_capped(self, mocker):
        client = _mock_client(mocker, _FAKE_OK)
        await invoke_tool("web_search", '{"query": "q", "max_results": 100}')
        # 应该被 cap 到 10
        call_kwargs = client.search.call_args.kwargs
        assert call_kwargs["max_results"] == 10

    @pytest.mark.asyncio
    async def test_max_results_floor(self, mocker):
        client = _mock_client(mocker, _FAKE_OK)
        await invoke_tool("web_search", '{"query": "q", "max_results": 0}')
        call_kwargs = client.search.call_args.kwargs
        assert call_kwargs["max_results"] == 1
