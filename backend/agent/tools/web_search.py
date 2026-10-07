"""Web 搜索工具（Tavily）。

Tavily 专为 LLM 设计，返回：
- answer: 对查询的直接答案（Tavily 的 AI 摘要）
- results: top N 结果（标题 + URL + 正文片段）

运行前提：.env 里有 TAVILY_API_KEY。
"""

import logging
import os

from pydantic import BaseModel, Field
from tavily import AsyncTavilyClient

from .registry import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10
SNIPPET_LEN = 300


def _get_client() -> AsyncTavilyClient:
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        raise RuntimeError("Web 搜索未配置：缺少 TAVILY_API_KEY")
    return AsyncTavilyClient(api_key=api_key)


class WebSearchArgs(BaseModel):
    query: str = Field(
        description="搜索查询词，尽量具体。例如：'2026 年 AI Agent 最新进展'"
    )
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS,
        description=f"返回结果数量，1-{MAX_RESULTS_LIMIT}，默认 {DEFAULT_MAX_RESULTS}",
    )


@tool(
    description=(
        "联网搜索最新信息。当用户问「最新的」「最近的」「帮我查一下」「搜索」时使用。"
        "返回：AI 摘要 + 若干条结果（标题/URL/正文片段）。"
        "适用于新闻、实时信息、不确定的事实查询。"
    )
)
async def web_search(args: WebSearchArgs) -> str:
    query = (args.query or "").strip()
    if not query:
        return "❌ 搜索词不能为空"

    max_results = max(1, min(args.max_results, MAX_RESULTS_LIMIT))

    try:
        client = _get_client()
    except RuntimeError as e:
        return f"❌ {e}"

    try:
        response = await client.search(
            query=query,
            max_results=max_results,
            include_answer=True,  # 让 Tavily 给 AI 摘要
            search_depth="basic",  # basic 快，advanced 慢但准
        )
    except Exception as e:
        logger.exception("[web_search] 搜索失败")
        return f"❌ 搜索失败：{type(e).__name__}: {e}"

    return _format_results(query, response)


def _format_results(query: str, response: dict) -> str:
    """把 Tavily 的原始响应精简成结构化文本。"""
    lines: list[str] = []

    # 1. AI 摘要
    answer = response.get("answer")
    if answer:
        lines.append(f"💡 摘要：{answer.strip()}")
        lines.append("")

    # 2. 结果列表
    results = response.get("results") or []
    if not results:
        return "\n".join(lines) + "📭 没有找到相关结果"

    lines.append(f"📚 找到 {len(results)} 条结果：")
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip().replace("\n", " ")
        if len(content) > SNIPPET_LEN:
            content = content[:SNIPPET_LEN] + "…"

        lines.append(f"\n{i}. {title}")
        if url:
            lines.append(f"   🔗 {url}")
        if content:
            lines.append(f"   {content}")

    return "\n".join(lines)
