import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

# 让测试能 import 到 run.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入 run 之前，先把环境变量塞好，避免 AsyncOpenAI 初始化失败
os.environ.setdefault("LLM_API_KEY", "sk-233efcae3efc4d7ab98e0441dc7feac7")
os.environ.setdefault(
    "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
os.environ.setdefault("LLM_MODEL_ID", "kimi-k2.7-code")


def make_tool_call(call_id: str, name: str, arguments: str):
    """构造一个和 OpenAI SDK 结构相同的 tool_call 对象"""
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def make_response(content: str = None, tool_calls=None):
    """构造一个和 OpenAI ChatCompletion 结构相同的响应对象"""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.fixture
def mock_client(mocker):
    """
    替换 Agent 内部的 AsyncOpenAI 客户端，
    返回一个可以自定义响应的 mock client。
    """
    from run import Agent  # noqa

    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    mocker.patch("run.AsyncOpenAI", return_value=client)
    return client


@pytest.fixture
def agent(mock_client):
    """返回一个使用 mock client 的 Agent 实例"""
    from run import Agent

    return Agent("你是一个聊天助手，可以使用工具帮助用户解决问题。")


@pytest.fixture
def mock_client_factory(mocker):
    """允许测试自己构造 mock client 并注入"""
    from run import Agent  # noqa

    def _build():
        client = MagicMock()
        client.chat.completions.create = AsyncMock()
        mocker.patch("run.AsyncOpenAI", return_value=client)
        return client

    return _build
