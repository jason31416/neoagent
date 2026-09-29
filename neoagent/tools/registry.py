"""工具注册表。

范式照搬 hermes：每个工具 = 一份 JSON Schema（name/description/parameters）
+ 一个 async handler。schema 给 LLM 看（function calling），handler 真正干活。
新增工具只需要 register 一次，agent 循环自动发现。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Union

from ..log import bold_cyan

logger = logging.getLogger(__name__)


@dataclass
class ToolOutput:
    """工具结果：文字 + 可选图片（read_file 读图——OpenAI 协议下 tool 消息可带图，
    实测 DeepSeek 收；图片走"工具结果信封"进当轮上下文并落账）。"""
    text: str = ""
    images: List[str] = field(default_factory=list)   # 图片路径（核心读字节/落 blob）
    stat: str = ""        # 日志摘要（"+2 行 -1 行" / "读 12 行"）；模型看不到，只进日志


def _args_preview(args: Dict[str, Any]) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 200 else s[:200] + "…"


def _result_preview(text: str) -> str:
    """日志里的结果摘要：第一行前 20 字符（全文太刷屏；要看全文去 modellog/账本）。"""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return (first[:20] + "…") if len(first) > 20 else first


# handler 签名：吃一个 dict 参数，返回字符串或 ToolOutput（给模型看的执行结果）
ToolHandler = Callable[[Dict[str, Any]], Awaitable[Union[str, ToolOutput]]]


@dataclass
class ToolEntry:
    name: str
    description: str
    parameters: Dict[str, Any]   # JSON Schema，OpenAI function-calling 格式
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, part: str = "M") -> None:
        """part：这套工具属于哪个部件（M=主模型 / R=记忆整理），日志里写清楚。"""
        self.part = part
        self._tools: Dict[str, ToolEntry] = {}

    def register(self, name: str, description: str,
                 parameters: Dict[str, Any], handler: ToolHandler) -> None:
        """注册一个工具。name 重复注册直接报错（早死早超生）。"""
        if name in self._tools:
            raise ValueError(f"工具重复注册：{name}")
        self._tools[name] = ToolEntry(name, description, parameters, handler)
        logger.debug("注册工具 %s", name)     # R 每次整理都重建工具组，INFO 会刷屏

    def schemas(self) -> List[Dict[str, Any]]:
        """导出给 LLM 的工具列表（OpenAI function-calling 格式）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, args: Dict[str, Any]) -> Union[str, ToolOutput]:
        """按名字执行工具。未知工具返回错误字符串（让模型自己纠正）。

        调用与结果各打一行日志，写清部件与工具名（[M]/[R] 标签走通用 pattern 染色，
        工具名在这里显式 bold_cyan）：
        结果优先用工具自带的 stat（"+2 行 -1 行" 这类操作摘要），否则取首行前 20 字符。"""
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}，可用工具：{', '.join(self._tools)}"
        logger.info("[%s] 调用工具 %s(%s)", self.part, bold_cyan(name), _args_preview(args))
        out = await entry.handler(args)
        text = out.text if isinstance(out, ToolOutput) else str(out)
        stat = (out.stat if isinstance(out, ToolOutput) else "") or _result_preview(text)
        logger.info("[%s] 工具 %s 结果 %s", self.part, bold_cyan(name), stat)
        logger.debug("[%s] 工具 %s 结果全文（%d 字符）：%s", self.part, bold_cyan(name),
                     len(text), text[:500])
        return out
