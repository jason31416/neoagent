"""网页正文抓取：WebFetcher 抽象 + DeepSeek Fetch 适配（可拆换）。

协议统一：fetch(url) -> FetchResult(title, content)。
当前只有 DeepSeek 官方提供 Fetch（POST {base}/v1/tools/fetch，返回 Markdown 正文）；
换供应商 = 新实现一个 WebFetcher + make_fetcher 分支，web_read 工具面不变。
失败一律抛 FetchError（一句话说清原因），由 web_read 工具归一成结果文本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .deepseek import DEFAULT_BASE_URL, DeepSeekToolError, DeepSeekToolsClient

FETCH_TIMEOUT = 30.0   # 抓网页比搜索慢，给宽一点（秒）


class FetchError(Exception):
    """抓取失败：可读一句话，给工具层直接展示。"""


@dataclass
class FetchResult:
    title: str
    content: str        # Markdown 正文


class WebFetcher(Protocol):
    name: str

    async def fetch(self, url: str) -> FetchResult: ...


class DeepSeekFetch:
    """DeepSeek Fetch：返回页面标题 + 可读 Markdown 正文。"""

    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = FETCH_TIMEOUT) -> None:
        self._client = DeepSeekToolsClient(api_key, base_url, timeout)

    async def fetch(self, url: str) -> FetchResult:
        try:
            body = await self._client.call("fetch", {"url": url})
        except DeepSeekToolError as e:
            raise FetchError(f"{self.name} {e.message}") from None
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "")
        if not content.strip():
            raise FetchError(f"{self.name} 没有返回网页正文（页面可能被拦截或没有可提取内容）")
        return FetchResult(title=title, content=content)


def make_fetcher(api_key: str, base_url: str = "") -> WebFetcher:
    """按 config 构造 WebFetcher；DeepSeek 密钥缺失时抛 FetchError（工具层给可读提示）。"""
    key = (api_key or "").strip()
    if not key:
        raise FetchError(
            "未配置 DeepSeek 搜索密钥，读不了网页正文：补 config.yml search.deepseek_api_key"
            "（或环境变量 NEOAGENT_SEARCH_DEEPSEEK_API_KEY；model.base_url 是 DeepSeek "
            "官方时会自动复用 model.api_key）")
    return DeepSeekFetch(key, base_url or DEFAULT_BASE_URL)
