"""CLI 入口（M0 验证载体）：终端里直接和 agent 对话，验证 tool calling 闭环。

用法：
    python -m neoagent.cli            # 交互式
    echo "建个 hi.txt 写上 hello" | python -m neoagent.cli   # 管道单发（测试用）
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

from .app import build_agent
from .config import load_config
from .core.events import Conversation, MessageEvent, Sender


async def _repl() -> None:
    cfg = load_config()
    agent = build_agent(cfg)

    print(f"neoagent CLI（模型：{cfg.llm.model}）。输入内容开始对话，Ctrl-D 退出。")
    loop = asyncio.get_event_loop()

    while True:
        # stdin 读取放到线程池，避免阻塞事件循环
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (KeyboardInterrupt, EOFError):
            break
        if not line:  # EOF
            break
        line = line.strip()
        if not line:
            continue

        event = MessageEvent(
            id=uuid.uuid4().hex[:12],
            channel="cli",
            sender=Sender(id="cli-user", name="我"),
            conversation=Conversation(id="cli", type="direct"),
            text=line,
            timestamp=time.time(),
        )

        async def printer(text: str, quote_to: str | None = None, media: str | None = None):
            if media:
                print(f"\n（媒体：{media}）\n")
            else:
                print(f"\n{'（引用 ' + quote_to + '）' if quote_to else ''}{text}\n")

        await agent.submit(event, responder=printer)
        await agent.wait_idle(event.session_key())


def main() -> None:
    try:
        asyncio.run(_repl())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
