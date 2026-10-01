"""run_command 工具：让 neoagent 自己也能跑指令（同一注册表、同一权限档）。

- agent 跑指令是**工具调用**（属于助手协议组，不产生用户气泡、不污染时间线）；
- 权限档由指令自己声明（agent_ok）：/stop 这类不给跑；
- 每次执行留审计（日志必记；控制台工作行见控制台批次）。
"""

from __future__ import annotations

import logging
from typing import Callable

from ..core import commands
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_run_command(registry: ToolRegistry,
                         ctx_factory: Callable[[], commands.CommandContext]) -> None:
    """注册进工具面；ctx_factory 每次调用现取上下文（home/档位等以当下为准）。"""

    async def run_command(args) -> str:
        text = str(args.get("command") or "").strip()
        if not text:
            return "缺少 command 参数（要跑的指令全文，如 /effort high）"
        if not text.startswith("/"):
            text = "/" + text
        ctx = ctx_factory()
        ctx.agent_run = True
        receipt = commands.execute(text, ctx)
        logger.info("[command] agent %s → %s", text, receipt)
        return receipt

    registry.register(
        "run_command",
        "执行一条 neoagent 指令（如 /effort high、/home weixin、/status），"
        "和主人在聊天里敲指令等效。指令全文以 / 开头。",
        {"type": "object",
         "properties": {"command": {"type": "string",
                                    "description": "指令全文，如 /effort high"}},
         "required": ["command"]},
        run_command)
