"""prompt_toolkit 行式控制台客户端：常驻 >，输出重绘到提示行上方。

朴素观感（docs/设计-控制台与配置.md 第三节）：banner 后直接是转录，
无分割线无装饰字符；输入一行回车后由服务处理，输出以事件流回来。
裸输入 = 跟 neoagent 说话（channel=cli）；/ 开头 = 指令（与聊天同语法）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from .render import banner, render

logger = logging.getLogger(__name__)


async def run_client(sock_path: Path, *, home: str = "~/.neoagent",
                     detachable: bool = True) -> str:
    """连上服务跑控制台。返回退出原因："stop"（服务停了/一体化退出）或 "detach"。

    detachable=True（attach 形态）：Ctrl-C / Ctrl-D 只断开自己；
    detachable=False（一体化）：Ctrl-C / Ctrl-D = 停服务退出。
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
    except Exception as exc:
        print(f"连不上服务（{sock_path}）：{exc}")
        return "detach"

    for line in banner(home):
        print(line)
    print()

    stop_reason = "detach"

    async def pump() -> None:
        """服务 → 终端：转录回放 / 实时事件 / 视图输出。"""
        nonlocal stop_reason
        while True:
            raw = await reader.readline()
            if not raw:
                stop_reason = "stop"
                return
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            t = msg.get("t")
            if t == "hist":
                rows = msg.get("rows") or []
                if rows:
                    for ev in rows:
                        print(render(ev, color=True))
            elif t == "ev":
                print(render(msg.get("ev") or {}, color=True))
            elif t == "rows":
                for ev in msg.get("rows") or []:
                    print(render(ev, color=True))
            elif t == "out":
                print(msg.get("text") or "")
            elif t == "bye":
                stop_reason = "stop"
                return

    pump_task = asyncio.get_event_loop().create_task(pump())
    session: PromptSession = PromptSession("> ")
    try:
        while True:
            with patch_stdout():
                try:
                    line = await session.prompt_async()
                except KeyboardInterrupt:
                    print("（Ctrl-C：断开控制台；停服务用 /stop）" if detachable else "（停服务退出）")
                    stop_reason = "detach" if detachable else "stop"
                    break
                except EOFError:
                    stop_reason = "detach" if detachable else "stop"
                    break
            if pump_task.done():
                break
            text = (line or "").strip()
            if not text:
                continue
            try:
                writer.write((json.dumps({"t": "in", "text": text},
                                         ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception:
                print("（连接断了）")
                stop_reason = "stop"
                break
    finally:
        pump_task.cancel()
        try:
            writer.close()
        except Exception:
            pass
    return stop_reason
