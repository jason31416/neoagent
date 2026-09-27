"""OpenAI 兼容 LLM 客户端。

DeepSeek / 通义 / OpenAI / 自建 vLLM 都走 OpenAI Chat Completions 协议，
换供应商只改 config.toml 的 base_url / api_key / model，代码零改动。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 环境里的 NO_PROXY 若含裸 IPv6（如 "[::1]"、"::1"），httpx 解析会炸
# （Invalid port: ':1]'）。这里规范化一遍：去掉 IPv6 条目，其余保留。
# 只影响本进程，不动用户的代理设置。
for _var in ("NO_PROXY", "no_proxy"):
    if _var in os.environ:
        # 只剔 IPv6 条目（含 "::" 或 [xxx] 形式），host:port 等合法条目保留
        _entries = [e for e in os.environ[_var].split(",")
                    if e.strip() and "::" not in e and not e.strip().startswith("[")]
        os.environ[_var] = ",".join(_entries)

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""
    id: str
    name: str
    arguments: str        # JSON 字符串（协议原样，解析放到工具执行层）


@dataclass
class ChatResult:
    """一次模型调用的结果。content 和 tool_calls 可以同时出现。"""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 thinking: str = "disabled") -> None:
        self.model = model
        # DeepSeek 的 thinking 开关（默认开）。开着的话多轮要求回传
        # reasoning_content，而我们从消息库重渲染会丢它 → 400。默认显式关掉；
        # "" 表示不传该参数（换供应商时用）。
        self._thinking = thinking
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def chat(self, messages: List[Dict[str, Any]],
                   tools: Optional[List[Dict[str, Any]]] = None) -> ChatResult:
        """跑一轮模型调用。tools 传 OpenAI function-calling 格式的 schema 列表。"""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._thinking:
            kwargs["extra_body"] = {"thinking": {"type": self._thinking}}

        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        result = ChatResult(content=msg.content)
        for tc in (msg.tool_calls or []):
            result.tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments or "{}",
            ))
        return result
