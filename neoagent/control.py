"""控制协议：控制台 ↔ 服务的本机 socket（前端无关内核的一部分）。

- 控制台永远是 socket 客户端：一体化 = 服务恰好跑在旁边，attach = 随时接上；
- 事件流（JSON Lines，本机 0600 的 unix socket）：
  服务 → 客户端  {"t":"hist","rows":[ev,…]}   连上先回放转录
                {"t":"ev","ev":ev}           实时事件
                {"t":"rows","rows":[ev,…]}   视图命令输出（只给发起者，不落转录）
                {"t":"out","text":…}         原样文本输出（/logs 之类）
                {"t":"bye","reason":"stop"}  服务要停了
  客户端 → 服务  {"t":"in","text":…}         一行输入（/ 指令或聊天）
- 多终端共看共操作，转录同一份；`/quiet` 的客户端收窄为只看对话。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .console.transcript import ConsoleTranscript

logger = logging.getLogger(__name__)


class ClientConn:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.quiet = False      # /quiet：工作行不发给它


def control_alive(sock_path: Path) -> bool:
    """socket 通着 = 服务已经在跑。"""
    if not sock_path.exists():
        return False
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(str(sock_path))
        s.close()
        return True
    except Exception:
        return False


class ControlServer:
    def __init__(self, sock_path: Path, transcript: ConsoleTranscript,
                 on_input: Callable[[ClientConn, str], Awaitable[None]],
                 history: int = 100) -> None:
        self._sock_path = Path(sock_path)
        self.transcript = transcript
        self._on_input = on_input
        self._history = history
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: List[ClientConn] = []

    async def start(self) -> None:
        if self._sock_path.exists():
            if control_alive(self._sock_path):
                raise RuntimeError(f"服务已经在跑（{self._sock_path} 通着）")
            self._sock_path.unlink()      # 残留 socket 文件，清掉
        self._sock_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._sock_path))
        os.chmod(self._sock_path, 0o600)

    async def stop(self) -> None:
        for conn in list(self._conns):
            self._send(conn, {"t": "bye", "reason": "stop"})
            try:
                conn.writer.close()
            except Exception:
                pass
        self._conns.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        try:
            self._sock_path.unlink()
        except OSError:
            pass

    # ---------------------------------------------------------------- 收发

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        conn = ClientConn(writer)
        self._conns.append(conn)
        try:
            self._send(conn, {"t": "hist", "rows": self.transcript.recent(self._history)})
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("t") == "in":
                    try:
                        await self._on_input(conn, str(msg.get("text") or ""))
                    except Exception:
                        logger.exception("处理控制台输入出错")
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
            try:
                writer.close()
            except Exception:
                pass

    def _send(self, conn: ClientConn, obj: Dict[str, Any]) -> None:
        try:
            conn.writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass

    # ---------------------------------------------------------------- 对外

    def broadcast(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        """事件入转录 + 推给所有客户端（/quiet 的不给工作行）。"""
        ev = self.transcript.append(ev)
        for conn in list(self._conns):
            if conn.quiet and ev.get("kind") == "tool":
                continue
            self._send(conn, {"t": "ev", "ev": ev})
        return ev

    def reply_rows(self, conn: ClientConn, rows: List[Dict[str, Any]]) -> None:
        """视图命令输出（只给发起者，不落转录）。"""
        self._send(conn, {"t": "rows", "rows": rows})

    def reply_text(self, conn: ClientConn, text: str) -> None:
        self._send(conn, {"t": "out", "text": text})
