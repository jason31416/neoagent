"""联网搜索：WebSearcher 抽象 + DeepSeek / Exa / Tavily 适配（可拆换）。

协议统一：search(query, count) -> list[SearchResult(title, url, snippet, published, site)]。
换供应商 = 新实现一个 WebSearcher + config 的 search.provider，零改动核心。
失败一律抛 SearchError（一句话说清原因：缺钥 / HTTP / 超时 / 解析），
由 web_search 工具归一成结果文本——不让 HTTP/JSON 的异常形状漏进 agent 循环。

协议事实：
- DeepSeek（https://api-docs.deepseek.com/zh-cn/guides/search）：POST
  {base}/v1/tools/search，header Authorization: Bearer，body {query, count}，
  返回 results[].title/url/snippet/summary/date/site_name/rerank_score/authority。
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

from .deepseek import DEFAULT_BASE_URL, DeepSeekToolError, DeepSeekToolsClient

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
    site: str = ""        # 站点名（服务没给就空）


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


class DeepSeekSearch(_BaseSearcher):
    """DeepSeek 自研搜索引擎：返回排序分数 + 长摘要 + 权威性。

    给模型读的是 summary（通常 600~800 字，比 snippet 更适合当资料）；
    date / site_name 顺带带上，方便判断时效与来源。
    """

    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = SEARCH_TIMEOUT) -> None:
        super().__init__(api_key, timeout)
        self._client = DeepSeekToolsClient(api_key, base_url, timeout)

    async def search(self, query: str, count: int) -> List[SearchResult]:
        try:
            body = await self._client.call("search", {"query": query, "count": count})
        except DeepSeekToolError as e:
            raise SearchError(f"{self.name} {e.message}") from None
        rows = body.get("results") or []
        out: List[SearchResult] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            date = str(r.get("date") or "").strip()
            # summary 比 snippet 长、更适合给模型；没有就退回 snippet
            snippet = str(r.get("summary") or r.get("snippet") or "").strip()
            out.append(SearchResult(
                title=str(r.get("title") or "").strip(),
                url=str(r.get("url") or "").strip(),
                snippet=snippet,
                published=date[:10],
                site=str(r.get("site_name") or "").strip(),
            ))
        return out


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


def _build_searcher(name: str, keys: Dict[str, str],
                    deepseek_base_url: str) -> _BaseSearcher:
    """按服务商名构造实例（所有 key 已在调用方校验非空）。"""
    if name == "deepseek":
        return DeepSeekSearch(keys[name], deepseek_base_url or DEFAULT_BASE_URL)
    if name == "exa":
        return ExaSearch(keys[name])
    return TavilySearch(keys[name])


def make_searcher(provider: str, exa_key: str, tavily_key: str,
                  deepseek_key: str = "",
                  deepseek_base_url: str = "") -> _BaseSearcher:
    """按 config 选服务商：auto = deepseek→exa→tavily 取第一个有钥的；
    缺钥/未知名抛 SearchError。"""
    keys = {
        "deepseek": (deepseek_key or "").strip(),
        "exa": (exa_key or "").strip(),
        "tavily": (tavily_key or "").strip(),
    }
    provider = (provider or "auto").strip().lower()
    if provider == "auto":
        for name in ("deepseek", "exa", "tavily"):
            if keys[name]:
                return _build_searcher(name, keys, deepseek_base_url)
        raise SearchError(
            "未配置搜索密钥：补 config.yml search.deepseek_api_key / search.exa_api_key / "
            "search.tavily_api_key（或环境变量 NEOAGENT_SEARCH_DEEPSEEK_API_KEY / "
            "NEOAGENT_SEARCH_EXA_API_KEY / NEOAGENT_SEARCH_TAVILY_API_KEY）")
    if provider not in keys:
        raise SearchError(
            f"未知 search.provider：{provider}（可选 auto / deepseek / exa / tavily）")
    if not keys[provider]:
        raise SearchError(
            f"search.provider={provider} 但没配密钥：补 config.yml search.{provider}_api_key"
            f"（或环境变量 NEOAGENT_SEARCH_{provider.upper()}_API_KEY）")
    return _build_searcher(provider, keys, deepseek_base_url)
