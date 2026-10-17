"""DeepSeek 官方 Search / Fetch 工具接口的共用 HTTP 封装。

协议事实（https://api-docs.deepseek.com/zh-cn/guides/search）：
- 端点：POST {base_url}/v1/tools/search 与 POST {base_url}/v1/tools/fetch；
- 鉴权：Authorization: Bearer <DeepSeek API Key>；
- 错误：OpenAI 兼容的 {"error": {"message": ...}}；
- 408 / 429 / 5xx 建议等待后重试，402 需充值，其它 4xx 改请求前不要重试；
- Search / Fetch 各自 5 QPS，按成功且有结果的请求计费（各 5 元 / 1000 次）。

Search 与 Fetch 的响应映射分别放在 providers/search.py 与 providers/fetch.py；
本模块只管「发请求 + 归一错误」，两边共用，避免把 HTTP 细节漏进工具层。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 20.0
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0,)          # 最多重试 1 次（QPS 抖动通常是瞬时的）

_STATUS_HINTS = {
    400: "请求参数不合法",
    401: "DeepSeek API Key 无效或缺失（检查 search.deepseek_api_key / "
         "NEOAGENT_SEARCH_DEEPSEEK_API_KEY）",
    402: "DeepSeek 账户余额不足，充值后再试",
    403: "DeepSeek 安全策略拒绝了这次请求",
    404: "没有取到可用的网页内容（URL 不可用 / 被站点拦截 / 没有可提取的正文）",
    408: "DeepSeek 读取请求超时",
    413: "请求体超过 DeepSeek 大小限制",
    429: "触发 DeepSeek 限速（Search / Fetch 各 5 QPS），稍后重试",
    500: "DeepSeek 服务器内部错误",
    502: "DeepSeek 网关错误",
    503: "DeepSeek 服务暂不可用",
    504: "DeepSeek 下游请求超时",
}


class DeepSeekToolError(Exception):
    """一次 DeepSeek Search/Fetch 失败：status + 可读消息（工具层直接展示）。"""

    def __init__(self, status: int, message: str, tool: str = "") -> None:
        self.status = status
        self.message = message
        self.tool = tool
        super().__init__(message)


def tool_url(base_url: str, tool: str) -> str:
    """拼端点：base 带不带 /v1 都吃（用户可能照 model.base_url 的习惯写）。"""
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/v1/tools/{tool}"


def error_message(raw: str) -> str:
    """从 OpenAI 兼容错误体里取一句话；拿不到就返回压扁的原始响应。"""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "").strip()
            if msg:
                return msg
    return " ".join((raw or "").split())[:300]


def friendly_error(status: int, raw: str, tool: str = "") -> str:
    """把 HTTP 状态 + 错误体翻成一句人话（工具结果里直接可见）。"""
    msg = error_message(raw)
    hint = _STATUS_HINTS.get(status, "")
    # 401/402 的 API 原文只是复读密钥/余额，hint 更有指导性
    if status in (401, 402) and hint:
        return hint
    if hint and msg and msg not in hint:
        return f"{hint}（{msg}）"
    return hint or msg or f"DeepSeek {tool or '请求'}失败（HTTP {status}）"


class DeepSeekToolsClient:
    """Search / Fetch 共用的 POST JSON 客户端（Bearer 鉴权 + 轻量重试）。"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url or DEFAULT_BASE_URL
        self._timeout = timeout

    async def call(self, tool: str, payload: Dict[str, Any]) -> dict:
        """调一个工具端点，返回解析后的 JSON；失败抛 DeepSeekToolError。"""
        if not self._api_key:
            raise DeepSeekToolError(
                0,
                "未配置 DeepSeek 搜索密钥：补 config.yml search.deepseek_api_key"
                "（或环境变量 NEOAGENT_SEARCH_DEEPSEEK_API_KEY；model.base_url 是 "
                "DeepSeek 官方时会自动复用 model.api_key）",
                tool,
            )
        url = tool_url(self._base_url, tool)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_error: DeepSeekToolError | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            retry = False
            try:
                async with aiohttp.ClientSession(
                        trust_env=True,
                        timeout=aiohttp.ClientTimeout(total=self._timeout)) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        raw = await resp.text()
                        if resp.status == 200:
                            try:
                                body = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                raise DeepSeekToolError(
                                    resp.status, "DeepSeek 返回不是合法 JSON", tool) from None
                            if not isinstance(body, dict):
                                raise DeepSeekToolError(
                                    resp.status, "DeepSeek 返回格式不对（不是 JSON 对象）", tool)
                            return body
                        last_error = DeepSeekToolError(
                            resp.status, friendly_error(resp.status, raw, tool), tool)
                        retry = resp.status in RETRY_STATUSES
            except DeepSeekToolError:
                raise
            except asyncio.TimeoutError:
                last_error = DeepSeekToolError(
                    408, f"DeepSeek 请求超时（{self._timeout:.0f}s）", tool)
                retry = True
            except aiohttp.ClientError as e:
                last_error = DeepSeekToolError(0, f"DeepSeek 网络错误：{e}", tool)
                retry = True

            if not retry or attempt >= len(RETRY_DELAYS):
                raise last_error or DeepSeekToolError(0, "DeepSeek 请求失败", tool)
            logger.warning("[deepseek] %s 失败（%s），%.1fs 后重试",
                           tool, last_error.message, RETRY_DELAYS[attempt])
            await asyncio.sleep(RETRY_DELAYS[attempt])

        raise last_error or DeepSeekToolError(0, "DeepSeek 请求失败", tool)
