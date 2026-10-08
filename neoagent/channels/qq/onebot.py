"""QQ 通道协议层：OneBot 11 标准 WebSocket（NapCat 起 ws server，我们是 client）。

只做协议：连接/重连、API 调用（echo 配对）、事件帧、消息段解析（数组 / CQ 码两种形态）。
不做任何 agent 逻辑——那是 adapter.py 和核心的事。

协议事实（NapCat.Onebot 4.18 实测 2026-10）：
- 连 ws://host:port（根路径），token 走 Authorization: Bearer <token>；
  连上先收一条 meta_event lifecycle/connect，随后事件与 API 回包混在同一连接；
- API：发送 {"action": "send_group_msg", "params": {...}, "echo": "xx"} →
  回 {"status":"ok","retcode":0,"data":{...},"echo":"xx"}——回包靠 echo 对号；
- 事件：post_type=message 的 private/group 消息。message 字段是数组
  （messagePostFormat=array）或 CQ 码字符串（=string），raw_message 恒为 CQ 字符串；
  两种形态都归一成消息段列表 [{"type": ..., "data": {...}}, ...]。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

API_TIMEOUT = 15.0        # 一次 API 调用的回包等待上限（秒）
RECONNECT_MAX = 30.0      # 断线重连退避上限（秒）

# CQ 码：[CQ:type,key=value,...]，值里的 , [ ] & 分别转义为 &#44; &#91; &#93; &amp;
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")


def _cq_unescape(v: str) -> str:
    return (v.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
             .replace("&amp;", "&"))


def parse_cq(raw: str) -> List[Dict[str, Any]]:
    """CQ 码字符串 → 消息段列表（码间裸文本聚成 text 段）。"""
    segments: List[Dict[str, Any]] = []
    pos = 0
    for m in _CQ_RE.finditer(raw or ""):
        if m.start() > pos:
            segments.append({"type": "text",
                             "data": {"text": _cq_unescape(raw[pos:m.start()])}})
        params: Dict[str, str] = {}
        for kv in (m.group(2) or "").lstrip(",").split(","):
            if "=" in kv:
                k, _, v = kv.partition("=")
                params[k] = _cq_unescape(v)
        segments.append({"type": m.group(1), "data": params})
        pos = m.end()
    if pos < len(raw or ""):
        segments.append({"type": "text", "data": {"text": _cq_unescape(raw[pos:])}})
    return segments


def normalize_message(message: Any, raw_message: str = "") -> List[Dict[str, Any]]:
    """事件的 message 字段 → 消息段列表。数组/CQ 字符串都认，兜底用 raw_message。"""
    if isinstance(message, list):
        out = []
        for s in message:
            if isinstance(s, dict):
                out.append({"type": str(s.get("type") or ""),
                            "data": dict(s.get("data") or {})})
        return out
    if isinstance(message, str) and message:
        return parse_cq(message)
    return parse_cq(raw_message or "")


def message_text(message: Any, raw_message: str = "") -> str:
    """一条消息的纯文本（get_msg 兜底取引用原文用）：text/at/占位拼一起。"""
    parts: List[str] = []
    for seg in normalize_message(message, raw_message):
        t, d = seg["type"], seg["data"]
        if t == "text":
            parts.append(str(d.get("text") or ""))
        elif t == "at":
            parts.append(f"@{d.get('name') or d.get('qq') or ''}")
        elif t == "image":
            parts.append("（图）")
        elif t == "record":
            parts.append("（语音）")
        elif t == "video":
            parts.append("（视频）")
        elif t == "file":
            parts.append(f"（文件 {d.get('name') or ''}）".rstrip())
    return "".join(parts).strip()


class OneBotClient:
    """一条 WS 连接：API 调用（echo 配对）+ 事件流；断线自动重连。

    start() 在**首次连上**后返回（调用方据此报"已连接"）；之后掉线由内部
    退避重连，不用调用方管。call() 在连接断开的空窗里抛 RuntimeError。
    """

    def __init__(self, url: str, token: str = "",
                 on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None) -> None:
        self._url = url
        self._token = token or ""
        self.on_event = on_event
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._running = False
        self._ready = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.self_id = ""          # 登录后从事件/回包里认出的机器人 qq

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        if not self._url:
            raise RuntimeError("OneBot 未配置 ws_url")
        self._session = aiohttp.ClientSession(trust_env=True)
        self._running = True
        self._ready.clear()
        self._task = asyncio.get_event_loop().create_task(self._run())
        # 等首连（15s 连不上就算失败，让 /connect 的回执如实报）
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            await self.close()
            raise RuntimeError(f"连接不上 OneBot 服务器：{self._url}")

    async def close(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._fail_pending("连接已断开")

    # ---------------------------------------------------------------- 收发

    async def _run(self) -> None:
        assert self._session is not None
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        backoff = 1.0
        while self._running:
            try:
                async with self._session.ws_connect(
                        self._url, headers=headers, heartbeat=30,
                        autoclose=False) as ws:
                    self._ws = ws
                    self._ready.set()
                    backoff = 1.0
                    logger.info("[qq] OneBot 连接已建立 %s", self._url)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_frame(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[qq] OneBot 连接出错：%s", exc)
            finally:
                self._ws = None
                self._fail_pending("连接断开，等回包的调用作废")
            if not self._running:
                break
            logger.info("[qq] %.0fs 后重连 %s", backoff, self._url)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[qq] 非 JSON 帧：%s", raw[:200])
            return
        echo = frame.get("echo")
        if echo is not None:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(frame)
            return
        # 事件帧：认出 self_id（get_login_info 之外的兜底），转给上层
        if frame.get("self_id"):
            self.self_id = self.self_id or str(frame["self_id"])
        if self.on_event is not None:
            try:
                await self.on_event(frame)
            except Exception:
                logger.exception("[qq] 事件处理出错")

    async def call(self, action: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """调一个 OneBot API，返回 data；status 不 ok 抛 RuntimeError。"""
        ws = self._ws
        if ws is None or ws.closed:
            raise RuntimeError("OneBot 连接不可用")
        echo = uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[echo] = fut
        await ws.send_str(json.dumps({"action": action, "params": params or {},
                                      "echo": echo}, ensure_ascii=False))
        try:
            frame = await asyncio.wait_for(fut, timeout=API_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise RuntimeError(f"{action} 回包超时（{API_TIMEOUT:.0f}s）") from None
        if str(frame.get("status") or "") != "ok":
            raise RuntimeError(f"{action} 失败 status={frame.get('status')}"
                               f" retcode={frame.get('retcode')}"
                               f" message={frame.get('message') or frame.get('wording')}")
        return frame.get("data")
