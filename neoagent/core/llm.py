"""OpenAI 兼容 LLM 客户端。

DeepSeek / 通义 / OpenAI / 自建 vLLM 都走 OpenAI Chat Completions 协议，
换供应商只改 config.yml 的 model（base_url / api_key / name），代码零改动。
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
    """一次模型调用的结果。content 和 tool_calls 可以同时出现。
    reasoning 是模型的思考草稿（reasoning_content，不发给用户，回传存档用）。
    usage 是接口回的真实用量（prompt/completion/reasoning_tokens，拿不到为 None）。"""
    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    reasoning: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 reasoning_effort: str = "medium") -> None:
        self.model = model
        # 思考档位走 OpenAI 标准参数 reasoning_effort（DeepSeek 实测认）：
        # "none"=关思考；"minimal/low/medium/high/xhigh/ultra/max"=开思考；""=不传（服务端默认，
        # ⚠️ DeepSeek 默认是**开**思考）。per-request 可覆盖 → 混用思考/非思考。
        self._effort = reasoning_effort
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def chat(self, messages: List[Dict[str, Any]],
                   tools: Optional[List[Dict[str, Any]]] = None,
                   reasoning_effort: Optional[str] = None) -> ChatResult:
        """跑一轮模型调用。tools 传 OpenAI function-calling 格式的 schema 列表。
        reasoning_effort 覆盖默认档位（None = 用构造时的默认）。"""
        effort = self._effort if reasoning_effort is None else reasoning_effort
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if effort:
            kwargs["reasoning_effort"] = effort

        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        result = ChatResult(content=msg.content,
                            reasoning=getattr(msg, "reasoning_content", None) or None)
        for tc in (msg.tool_calls or []):
            result.tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments or "{}",
            ))
        if getattr(resp, "usage", None) is not None:
            result.usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
            }
            details = getattr(resp.usage, "completion_tokens_details", None)
            rt = getattr(details, "reasoning_tokens", None) if details is not None else None
            if rt:
                result.usage["reasoning_tokens"] = int(rt)
        return result
