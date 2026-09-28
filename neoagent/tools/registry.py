"""工具注册表。

范式照搬 hermes：每个工具 = 一份 JSON Schema（name/description/parameters）
+ 一个 async handler。schema 给 LLM 看（function calling），handler 真正干活。
新增工具只需要 register 一次，agent 循环自动发现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Union

logger = logging.getLogger(__name__)


@dataclass
class ToolOutput:
    """工具结果：文字 + 可选图片（read_file 读图——OpenAI 协议下 tool 消息可带图，
    实测 DeepSeek 收；图片走"工具结果信封"进当轮上下文并落账）。"""
    text: str = ""
    images: List[str] = field(default_factory=list)   # 图片路径（核心读字节/落 blob）


# handler 签名：吃一个 dict 参数，返回字符串或 ToolOutput（给模型看的执行结果）
ToolHandler = Callable[[Dict[str, Any]], Awaitable[Union[str, ToolOutput]]]


@dataclass
class ToolEntry:
    name: str
    description: str
    parameters: Dict[str, Any]   # JSON Schema，OpenAI function-calling 格式
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolEntry] = {}

    def register(self, name: str, description: str,
                 parameters: Dict[str, Any], handler: ToolHandler) -> None:
        """注册一个工具。name 重复注册直接报错（早死早超生）。"""
        if name in self._tools:
            raise ValueError(f"工具重复注册：{name}")
        self._tools[name] = ToolEntry(name, description, parameters, handler)
        logger.info("注册工具 %s", name)

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
        """按名字执行工具。未知工具返回错误字符串（让模型自己纠正）。"""
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}，可用工具：{', '.join(self._tools)}"
        return await entry.handler(args)
