"""Jev（TypeSafe System One 决策模型）接入：不生成文字，一次调用回答类型化问题。

走 OpenCode Zen 的 systemone 端点：发 state + questions（题型 noul/choice/score），
回 answers（choice 带 confidence 与全概率分布）。攒批沉降的档位判定用它；
将来主动 jev（该不该唤醒）也从这走。

实测踩过的坑（改这里先看）：
- Zen 会 403 掉 Python 默认 User-Agent，必须带自定义 UA；
- choice 的选项字段叫 criteria（标签 → 说明/null），写 options/choices 会报
  误导性的 server_error；
- 免费模型 jev-1.13-free 是限时的，挂了调用方自己兜底（ask 返回 None）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1/systemone"
DEFAULT_MODEL = "jev-1.13-free"
USER_AGENT = "neoagent/0.1"


class JevClient:
    """systemone 端点的轻客户端。ask() 任何失败返回 None（调用方兜底）。"""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL, timeout: float = 8.0) -> None:
        self._key = api_key
        self._base = base_url or DEFAULT_BASE_URL
        self._model = model or DEFAULT_MODEL
        self._timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def ask(self, state: str, questions: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """发一次决策请求，返回 answers 字典；失败返回 None。"""
        body = {"model": self._model, "state": state, "questions": questions}
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    headers={"Authorization": f"Bearer {self._key}",
                             "User-Agent": USER_AGENT})
            async with self._session.post(self._base, json=body) as resp:
                if resp.status != 200:
                    logger.warning("jev 请求失败 HTTP %s：%s", resp.status,
                                   (await resp.text())[:200])
                    return None
                data = await resp.json(content_type=None)
                return data.get("answers")
        except Exception as exc:
            logger.warning("jev 请求出错：%s", exc)
            return None
