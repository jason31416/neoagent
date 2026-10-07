"""联网搜索：WebSearcher 抽象 + Exa / Tavily 适配（可拆换）。

协议统一：search(query, count) -> list[SearchResult(title, url, snippet, published)]。
换供应商 = 新实现一个 WebSearcher + config 的 search.provider，零改动核心。
失败一律抛 SearchError（一句话说清原因：缺钥 / HTTP / 超时 / 解析），
由 web_search 工具归一成结果文本——不让 HTTP/JSON 的异常形状漏进 agent 循环。

协议事实：
- Exa（https://exa.ai/docs/reference/search）：POST https://api.exa.ai/search，
  header x-api-key，body {query, numResults, type: "auto", text: true}，
  返回 results[].title/url/text/publishedDate。
- Tavily（https://docs.tavily.com/documentation/api-reference/endpoint/search）：
  POST https://api.tavily.com/search，header Authorization: Bearer，
  body {query, max_results}，返回 results[].title/url/content。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Protocol

import aiohttp

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 20.0   # 一次搜索的总超时（秒）


class SearchError(Exception):
    """搜索失败：可读一句话，给工具层直接展示。"""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    published: str = ""   # 发布日期（YYYY-MM-DD，服务没给就空）


class WebSearcher(Protocol):
    name: str

    async def search(self, query: str, count: int) -> List[SearchResult]: ...


class _BaseSearcher:
    """共用的 POST JSON 信封：网络/HTTP/解析失败统一抛 SearchError。"""

    name = ""

    def __init__(self, api_key: str, timeout: float = SEARCH_TIMEOUT) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def _post(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> dict:
        try:
            async with aiohttp.ClientSession(
                    trust_env=True,
                    timeout=aiohttp.ClientTimeout(total=self._timeout)) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        raise SearchError(f"{self.name} HTTP {resp.status}：{raw[:200]}")
                    return json.loads(raw)
        except SearchError:
            raise
        except asyncio.TimeoutError:
            raise SearchError(f"{self.name} 搜索超时（{self._timeout:.0f}s）") from None
        except aiohttp.ClientError as e:
            raise SearchError(f"{self.name} 网络错误：{e}") from None
        except (json.JSONDecodeError, TypeError):
            raise SearchError(f"{self.name} 返回不是合法 JSON") from None


class ExaSearch(_BaseSearcher):
    """Exa：神经/混合检索，text=true 顺带返回正文片段。"""

    name = "exa"

    async def search(self, query: str, count: int) -> List[SearchResult]:
        body = await self._post(
            "https://api.exa.ai/search",
            {"x-api-key": self._api_key},
            {"query": query, "numResults": count, "type": "auto", "text": True},
        )
        rows = body.get("results") or []
        return [SearchResult(
            title=str(r.get("title") or "").strip(),
            url=str(r.get("url") or "").strip(),
            snippet=str(r.get("text") or "").strip(),
            published=str(r.get("publishedDate") or "").strip()[:10],
        ) for r in rows]


class TavilySearch(_BaseSearcher):
    """Tavily：为 LLM 设计的搜索，返回精炼内容片段。"""

    name = "tavily"

    async def search(self, query: str, count: int) -> List[SearchResult]:
        body = await self._post(
            "https://api.tavily.com/search",
            {"Authorization": f"Bearer {self._api_key}"},
            {"query": query, "max_results": count},
        )
        rows = body.get("results") or []
        return [SearchResult(
            title=str(r.get("title") or "").strip(),
            url=str(r.get("url") or "").strip(),
            snippet=str(r.get("content") or "").strip(),
        ) for r in rows]


def make_searcher(provider: str, exa_key: str, tavily_key: str) -> _BaseSearcher:
    """按 config 选服务商：auto = exa→tavily 取第一个有钥的；缺钥/未知名抛 SearchError。"""
    keys = {"exa": (exa_key or "").strip(), "tavily": (tavily_key or "").strip()}
    provider = (provider or "auto").strip().lower()
    if provider == "auto":
        for name in ("exa", "tavily"):
            if keys[name]:
                return ExaSearch(keys[name]) if name == "exa" else TavilySearch(keys[name])
        raise SearchError(
            "未配置搜索密钥：补 config.yml search.exa_api_key 或 search.tavily_api_key"
            "（或环境变量 NEOAGENT_SEARCH_EXA_API_KEY / NEOAGENT_SEARCH_TAVILY_API_KEY）")
    if provider not in keys:
        raise SearchError(f"未知 search.provider：{provider}（可选 auto / exa / tavily）")
    if not keys[provider]:
        raise SearchError(
            f"search.provider={provider} 但没配密钥：补 config.yml search.{provider}_api_key"
            f"（或环境变量 NEOAGENT_SEARCH_{provider.upper()}_API_KEY）")
    return ExaSearch(keys[provider]) if provider == "exa" else TavilySearch(keys[provider])
