"""fetch_url 工具测试。"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.registry import invoke_tool
from tools import fetch_url as fu_module


def _mock_httpx(mocker, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    mocker.patch("tools.fetch_url.httpx.AsyncClient", return_value=client)
    return client


# ============================================================
# URL 安全校验
# ============================================================


class TestUrlSafety:
    @pytest.mark.asyncio
    async def test_reject_localhost(self):
        result = await invoke_tool("fetch_url", '{"url": "http://localhost/x"}')
        assert "本地地址" in result

    @pytest.mark.asyncio
    async def test_reject_127(self):
        result = await invoke_tool("fetch_url", '{"url": "http://127.0.0.1/x"}')
        assert "本地地址" in result

    @pytest.mark.asyncio
    async def test_reject_private_ip(self):
        result = await invoke_tool("fetch_url", '{"url": "http://192.168.1.1/x"}')
        assert "内网" in result

    @pytest.mark.asyncio
    async def test_reject_ftp_scheme(self):
        result = await invoke_tool("fetch_url", '{"url": "ftp://example.com"}')
        assert "http" in result

    @pytest.mark.asyncio
    async def test_reject_empty_url(self):
        result = await invoke_tool("fetch_url", '{"url": "  "}')
        assert "不能为空" in result


# ============================================================
# 正常抓取
# ============================================================


class TestFetch:
    @pytest.mark.asyncio
    async def test_success(self, mocker):
        _mock_httpx(mocker, 200, "Title: 测试文章\n\n这是正文内容。")
        result = await invoke_tool(
            "fetch_url",
            '{"url": "https://www.cnblogs.com/cl193/p/22107535#commentform"}',
        )
        assert "测试文章" in result
        assert "这是正文内容" in result
        assert "https://www.cnblogs.com/cl193/p/22107535#commentform" in result

    @pytest.mark.asyncio
    async def test_truncation_when_too_long(self, mocker):
        long_text = "Title: 长文\n\n" + "x" * 10000
        _mock_httpx(mocker, 200, long_text)
        result = await invoke_tool("fetch_url", '{"url": "https://example.com/long"}')
        assert "已截断" in result
        # 内容长度应该被限住
        assert len(result) < 5000

    @pytest.mark.asyncio
    async def test_no_truncation_when_short(self, mocker):
        _mock_httpx(mocker, 200, "Title: 短文\n\n短内容")
        result = await invoke_tool("fetch_url", '{"url": "https://example.com/short"}')
        assert "已截断" not in result


# ============================================================
# 错误处理
# ============================================================


class TestErrors:
    @pytest.mark.asyncio
    async def test_timeout(self, mocker):
        import httpx

        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mocker.patch("tools.fetch_url.httpx.AsyncClient", return_value=client)

        result = await invoke_tool("fetch_url", '{"url": "https://example.com"}')
        assert "超时" in result

    @pytest.mark.asyncio
    async def test_404(self, mocker):
        _mock_httpx(mocker, 404, "")
        result = await invoke_tool("fetch_url", '{"url": "https://example.com"}')
        assert "404" in result

    @pytest.mark.asyncio
    async def test_429(self, mocker):
        _mock_httpx(mocker, 429, "")
        result = await invoke_tool("fetch_url", '{"url": "https://example.com"}')
        assert "限流" in result

    @pytest.mark.asyncio
    async def test_empty_content(self, mocker):
        _mock_httpx(mocker, 200, "")
        result = await invoke_tool("fetch_url", '{"url": "https://example.com"}')
        assert "空内容" in result
