"""web_search：联网搜索（一个工具吃所有搜索服务）。

Exa / Tavily 的差异藏在 providers/search.py 后面，用哪家看 config 的 search 段
（auto = 有钥的优先）。工具面就一个 web_search——服务不涨工具。
读网页正文（web_read）下一步再加。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..config import SearchConfig
from ..log import green, red
from ..providers.search import SearchError, SearchResult, make_searcher
from .format import ToolOutput, error
from .registry import ToolRegistry

SNIPPET_MAX = 300   # 每条摘要给模型的字符上限（30K 总预算留给更多条目）
COUNT_MAX = 20


def _snippet(text: str) -> str:
    """摘要压成一行再截断（原文换行多，直进结果太占地方）。"""
    s = " ".join((text or "").split())
    return s if len(s) <= SNIPPET_MAX else s[:SNIPPET_MAX] + "…"


def render_results(results: List[SearchResult]) -> str:
    """紧凑列表：`N. 标题 — url（日期）` + 一行摘要。"""
    lines: List[str] = []
    for i, r in enumerate(results, 1):
        head = f"{i}. {r.title or r.url} — {r.url}"
        if r.published:
            head += f"（{r.published}）"
        lines.append(head)
        snip = _snippet(r.snippet)
        if snip:
            lines.append(f"   {snip}")
    return "\n".join(lines)


def register_search_tool(registry: ToolRegistry, cfg: SearchConfig) -> None:
    default_count = max(1, min(COUNT_MAX, int(cfg.max_results)))

    async def web_search(args: Dict[str, Any]) -> ToolOutput:
        query = str(args.get("query") or "").strip()
        if not query:
            return error("bad_param", "query 必填（想搜什么）")

        raw_count = args.get("count", default_count)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return error("bad_param", f"count 要是数字：{raw_count!r}")
        if not 1 <= count <= COUNT_MAX:
            return error("bad_param", f"count 需在 1..{COUNT_MAX}")

        try:
            searcher = make_searcher(cfg.provider, cfg.exa_api_key, cfg.tavily_api_key)
            results = await searcher.search(query, count)
        except SearchError as e:
            return ToolOutput(text=f"搜索失败：{e}",
                              stat=red("搜索失败"), fields=[("err", "search")])

        if not results:
            return ToolOutput(text="没有搜到结果：换个说法或换个关键词再试",
                              stat="0 条结果", fields=[("delta", "+0")])
        return ToolOutput(
            text=render_results(results),
            stat=f"{green('+' + str(len(results)))} 条结果（{searcher.name}）",
            fields=[("delta", f"+{len(results)}")],
        )

    registry.register(
        "web_search",
        "联网搜索网页（服务商 Exa/Tavily 由配置决定）。返回标题、链接和内容摘要片段，"
        "查资料、核事实、追新闻用它；搜索词用自然语言也行。",
        {"type": "object",
         "properties": {
             "query": {"type": "string", "description": "搜索词（关键词或自然语言问题）"},
             "count": {"type": "integer",
                       "description": f"返回条数，1..{COUNT_MAX}，默认 {default_count}"},
         },
         "required": ["query"]},
        web_search,
    )
