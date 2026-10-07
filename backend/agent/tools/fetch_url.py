"""网页抓取工具（Jina Reader）。

原理：Jina Reader 是一个代理服务，把任意 URL 的网页转成 markdown。
用法：https://r.jina.ai/{目标URL}

免费、无需 key。处理了 JS 渲染、正文提取、反爬。
"""

import ipaddress
import logging
import os
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from .registry import tool

logger = logging.getLogger(__name__)

JINA_BASE = "https://r.jina.ai"
FETCH_TIMEOUT_S = 15.0
MAX_CONTENT_CHARS = 4000  # 正文截断长度
MAX_URL_LEN = 2000


def _is_safe_url(url: str) -> tuple[bool, str]:
    """基础 SSRF 防护：拒绝本地/内网地址。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL 格式错误"

    if parsed.scheme not in ("http", "https"):
        return False, "只支持 http/https 链接"

    host = parsed.hostname
    if not host:
        return False, "URL 缺少域名"

    # 常见本地地址
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False, "不允许访问本地地址"

    # 内网 IP 段
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False, "不允许访问内网地址"
    except ValueError:
        # host 是域名不是 IP，放行
        pass

    return True, ""


class FetchUrlArgs(BaseModel):
    url: str = Field(description="要抓取的网页 URL，必须以 http:// 或 https:// 开头")


@tool(
    description=(
        "抓取一个网页的正文内容。当用户发来一个链接（文章、新闻、博客）"
        "并希望你阅读/总结时使用。返回 markdown 格式的正文（超长会截断）。"
        "注意：这是「读一个已知链接」，不是「搜索」。要搜索请用 web_search。"
    )
)
async def fetch_url(args: FetchUrlArgs) -> str:
    url = (args.url or "").strip()

    if not url:
        return "❌ URL 不能为空"
    if len(url) > MAX_URL_LEN:
        return f"❌ URL 过长（>{MAX_URL_LEN} 字符）"

    ok, reason = _is_safe_url(url)
    if not ok:
        return f"❌ {reason}"

    jina_url = f"{JINA_BASE}/{url}"

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
            resp = await client.get(
                jina_url,
                headers={"Accept": "text/plain"},
            )
    except httpx.TimeoutException:
        return f"❌ 抓取超时（>{int(FETCH_TIMEOUT_S)}秒），链接可能不可访问"
    except httpx.RequestError as e:
        return f"❌ 网络错误：{type(e).__name__}"

    if resp.status_code == 429:
        return "❌ 抓取服务限流，请稍后重试"
    if resp.status_code == 404:
        return f"❌ 链接不存在（404）：{url}"
    if resp.status_code != 200:
        return f"❌ 抓取失败（HTTP {resp.status_code}）"

    content = resp.text.strip()
    if not content:
        return "❌ 抓取到空内容，网页可能是纯 JS 渲染或需要登录"

    return _format_content(url, content)


def _format_content(url: str, content: str) -> str:
    """把 Jina 返回的 markdown 精简并截断。"""
    # 首行通常是 "Title: xxx"
    lines = content.split("\n")
    title = ""
    if lines and lines[0].startswith("Title:"):
        title = lines[0][len("Title:") :].strip()
        lines = lines[1:]
    body = "\n".join(lines).strip()

    # 智能截断：只在超长时加提示
    truncated = False
    if len(body) > MAX_CONTENT_CHARS:
        body = body[:MAX_CONTENT_CHARS]
        truncated = True

    parts = []
    if title:
        parts.append(f"📄 标题：{title}")
    parts.append(f"🔗 URL：{url}")
    parts.append("")
    parts.append("正文内容：")
    parts.append(body)
    if truncated:
        parts.append("")
        parts.append(f"…（内容已截断，原文约 {len(content)} 字）")

    return "\n".join(parts)
