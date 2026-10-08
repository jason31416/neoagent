"""chatagent：飞书群里的"群友"子 agent（每群一个，独立上下文）。

角色：看全部群消息、被 @ 才说话、聊天专用（工具只有 memory + tell_neoagent）。
- **消息路由**：所有群消息落群记录（group_messages）；活跃群的 chatagent
  噪音安静入账（不打断在飞模型），被 @ / 上级来话才触发 turn；
- **双向配对**：tell_neoagent（chatagent→主 agent）与 tell_chatagent（主 agent→chatagent）
  用 pending future 配对应答，A→B→A 不产生新轮（防回环）；
- **压缩**：上下文压力超阈值 → 静默总结调用 → `<compact>` 行落为最新消息、
  最近 10 条原样保留、切点推进、切点以下的图回收（压缩即销毁）；
- **状态**：激活与否进 kv（重启不丢）；休眠 = 实例销毁、零 API、消息只落记录。

身份口径：chatagent 是 neoagent 活在群里的子 agent（提示词开头即此），
主 agent 是私聊里的同一个人——一个产品，一个名字。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..config import PACKAGE_DIR
from ..providers.vision import sniff_image_mime
from .events import MessageEvent, SendResult
from .frames import finalize_user
from .ledger import TYPE_INTERNAL, TYPE_USER, MessageLedger
from .llm import LLMClient

logger = logging.getLogger(__name__)

# 数值旋钮（代码常数，不过度配置）
CHATAGENT_TOKEN_BUDGET = 60_000      # 群消息量大，独立小预算（与主 agent 的分开）
CHATAGENT_MAX_TOOL_ROUNDS = 8        # 聊天工具，链短
TELL_TIMEOUT = 90.0                  # tell_* 同步等待上限（秒）
COMPACT_KEEP = 10                    # 压缩后原样保留的最近消息条数
PRESSURE_PCT = 0.9                   # 上下文占比超过它触发静默压缩
COMPACT_MAX_IMAGES = 6               # 总结调用最多带几张图

COMPACT_PROMPT = (
    "把这份群聊记录总结成一份简短的接续备忘，写给刚失忆的自己看、供接话用。"
    "包括这些部分（有就写没有就略过）：最近聊到的内容 / 正在讨论的内容 / "
    "主人的嘱咐与待办 / 人物印象。只写要点，不要客套，不要复述整段对话。"
)


class ChatAgentManager:
    """所有群的 chatagent 生命周期 + 消息路由 + 两 agent 间的消息配对。"""

    def __init__(self, *, main: Any, ledger: MessageLedger, llm: LLMClient,
                 home: Path, vision=None, transcriber=None, token_counter=None,
                 known_channels=()) -> None:
        self.main = main                      # 主 AgentLoop（tell_neoagent 注入它）
        self.ledger = ledger
        self.llm = llm
        self.home = Path(home)
        self.vision = vision
        self.transcriber = transcriber
        self.token_counter = token_counter
        self.known_channels = tuple(known_channels)
        self._loops: Dict[str, Any] = {}      # chat_id → AgentLoop
        self._up: Dict[str, asyncio.Future] = {}    # chatagent 等主 agent 的答复
        self._down: Dict[str, asyncio.Future] = {}  # 主 agent 等 chatagent 的回话
        self._sender: Optional[Callable] = None     # 发群消息出口（run.py 注入）
        self._quote_fetcher: Optional[Callable] = None

    def set_sender(self, sender: Callable) -> None:
        """发群消息出口：await sender(chat_id, text, quote_to=..., media=...)。"""
        self._sender = sender

    def set_quote_fetcher(self, fn: Optional[Callable]) -> None:
        self._quote_fetcher = fn

    # ------------------------------------------------------------------
    # 状态（kv 持久，重启不丢）
    # ------------------------------------------------------------------
    def is_active(self, chat_id: str) -> bool:
        return self.ledger.get_setting(f"group_active:{chat_id}") == "1"

    def activate(self, chat_id: str) -> None:
        self.ledger.set_setting(f"group_active:{chat_id}", "1")
        self._ensure_loop(chat_id)

    def deactivate(self, chat_id: str) -> None:
        self.ledger.set_setting(f"group_active:{chat_id}", "")
        loop = self._loops.pop(chat_id, None)
        if loop is not None:
            st = loop._states.get(f"group:{chat_id}")
            if st is not None and st.task is not None and not st.task.done():
                st.task.cancel()          # 休眠 = 当作不存在：在飞的轮直接掐

    def _ensure_loop(self, chat_id: str):
        loop = self._loops.get(chat_id)
        if loop is not None:
            return loop
        from ..tools.chatagent_tools import register_chatagent_tools
        from ..tools.registry import ToolRegistry

        registry = ToolRegistry(part="chatagent")
        notes = self.home / "groups" / chat_id / "memory.md"
        register_chatagent_tools(registry, memory_file=notes,
                                 manager=self, chat_id=chat_id)
        loop = AgentLoopFactory(self, registry, chat_id)
        self._loops[chat_id] = loop
        logger.info("[chatagent] %s 上岗（便签 %s）", chat_id, notes)
        return loop

    # ------------------------------------------------------------------
    # 群消息路由（run.py 的 on_message 把群聊全量交给这里）
    # ------------------------------------------------------------------
    async def on_group_message(self, event: MessageEvent) -> None:
        chat_id = event.conversation.id
        title = event.conversation.title or ""
        if title:
            self.ledger.set_setting(f"group_title:{chat_id}", title)

        # ① 群记录：一条不少（文字原样；图等非文字只记占位）
        atts = event.attachments or []
        kind = "text" if (event.text or "").strip() else (
            atts[0].kind if atts else "text")
        text = (event.text or "").strip() or f"[{ {'image': '图', 'file': '文件',
                'audio': '语音', 'video': '视频'}.get(kind, '附件') }]"
        self.ledger.log_group_message(
            chat_id=chat_id, sender=event.sender.id, is_owner=event.is_owner,
            kind=kind, text=text, message_id=event.id, timestamp=event.timestamp)

        # ② 指令（主人 @ 并发送才认；/id 谁都能问）
        stripped = _strip_mentions(event.text or "")
        if stripped.startswith("/"):
            await self._handle_command(event, stripped)
            return

        # ③ chatagent：活跃才有人；@ 触发 turn，其余安静入账
        if not self.is_active(chat_id):
            return
        loop = self._ensure_loop(chat_id)
        key = loop.session_key
        if event.metadata.get("at_bot"):
            await loop.ingest(event, quote_fetcher=self._quote_fetcher,
                              responder=self._responder(chat_id))
            loop.kick(key)
            asyncio.get_event_loop().create_task(self._post_turn(chat_id))
        else:
            await loop.ingest_quiet(event, quote_fetcher=self._quote_fetcher)

    async def _handle_command(self, event: MessageEvent, text: str) -> None:
        """群内指令：/activate、/deactivate（主人 @ 才认）、/id（谁都能问）。"""
        chat_id = event.conversation.id
        parts = text.split()
        name = parts[0].lstrip("/").lower()
        at_bot = bool(event.metadata.get("at_bot"))

        if name == "id":
            receipt = f"你的 id：{event.sender.id}"
        elif name in ("activate", "deactivate"):
            if not (event.is_owner and at_bot):
                logger.info("[chatagent] 非主人/未 @ 的指令忽略：%s（%s）", name,
                            event.sender.id)
                return                      # 静默忽略，不教人试
            if name == "activate":
                self.activate(chat_id)
                receipt = "已激活，@我聊天"
            else:
                self.deactivate(chat_id)
                receipt = "已休眠（记忆和记录都留着，/activate 再来）"
        else:
            receipt = "群里只认 /activate、/deactivate、/id（其他指令私聊说）"

        await self._say_group(chat_id, receipt, sender="neoagent")

    # ------------------------------------------------------------------
    # 两 agent 之间的消息配对
    # ------------------------------------------------------------------
    async def chatagent_says(self, chat_id: str, message: str,
                             context: str = "") -> str:
        """tell_neoagent 的实现：回话给等在下面的 tell_chatagent，或上报主 agent。"""
        down = self._down.pop(chat_id, None)
        if down is not None and not down.done():
            down.set_result((message, context))
            return "已回话给 neoagent"

        up = asyncio.get_event_loop().create_future()
        self._up[chat_id] = up
        body = message
        if context.strip():
            body += f"\n<context>\n{context.strip()}\n</context>"
        self._inject_main(chat_id, body)
        self.main.kick(self.main.timeline_bucket or "main")
        try:
            reply = await asyncio.wait_for(up, timeout=TELL_TIMEOUT)
            return reply
        except asyncio.TimeoutError:
            return "已转达，回头群里说"
        finally:
            if self._up.get(chat_id) is up:
                self._up.pop(chat_id, None)

    async def main_says(self, chat_id: str, message: str) -> str:
        """tell_chatagent 的实现：答正在等的 chatagent，或触发它一轮。"""
        if not self.is_active(chat_id):
            return ""                      # 调用方按 not_found 口径处理
        up = self._up.pop(chat_id, None)
        if up is not None and not up.done():
            up.set_result(message)
            return "已回复 chatagent"

        loop = self._ensure_loop(chat_id)
        key = loop.session_key
        down = asyncio.get_event_loop().create_future()
        self._down[chat_id] = down
        try:
            self._inject_chatagent(loop, chat_id, message)
            loop.kick(key)
            idle = asyncio.get_event_loop().create_task(loop.wait_idle(key))
            done, _pending = await asyncio.wait(
                {down, idle}, return_when=asyncio.FIRST_COMPLETED,
                timeout=TELL_TIMEOUT)
            idle.cancel()
            if down in done and not down.cancelled():
                text, _ctx = down.result()
                return text
            return "已记下"
        finally:
            if self._down.get(chat_id) is down:
                self._down.pop(chat_id, None)

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------
    def _inject_main(self, chat_id: str, body: str) -> None:
        """chatagent 的消息进主桶（sender 标明是子 agent 的转述）。"""
        key = self.main.timeline_bucket or "main"
        ts = time.time()
        title = self.ledger.get_setting(f"group_title:{chat_id}")
        sender = f"chatagent@{title or chat_id}"
        bid = self.ledger.alloc_bubbles(key, 1)[0]
        final = finalize_user(body, sender=sender, bubble_id=bid, ts=ts)
        seq = self.ledger.add_turn(session_key=key, type=TYPE_USER, text=final,
                                   timestamp=ts)
        self.ledger.add_bubble(
            session_key=key, bubble_id=bid, seq_id=seq, direction="in",
            platform="", chat_id=chat_id, kind="text", content=body,
            sender=sender, timestamp=ts)
        self.main.states_of(key).input_gen += 1

    def _inject_chatagent(self, loop, chat_id: str, body: str) -> None:
        """上级来话进 chatagent 的上下文（sender=neoagent）。"""
        key = loop.session_key
        ts = time.time()
        bid = self.ledger.alloc_bubbles(key, 1)[0]
        final = finalize_user(body, sender="neoagent", bubble_id=bid, ts=ts)
        seq = self.ledger.add_turn(session_key=key, type=TYPE_USER, text=final,
                                   timestamp=ts)
        self.ledger.add_bubble(
            session_key=key, bubble_id=bid, seq_id=seq, direction="in",
            platform="", chat_id=chat_id, kind="text", content=body,
            sender="neoagent", timestamp=ts)
        loop.states_of(key).input_gen += 1

    # ------------------------------------------------------------------
    # 发群消息
    # ------------------------------------------------------------------
    def _responder(self, chat_id: str):
        async def responder(text: str, quote_to: str | None = None,
                            media: str | None = None, channel: str | None = None):
            if media:
                return SendResult(ok=False, error="chatagent 不发媒体")
            return await self._say_group(chat_id, text, quote_to=quote_to)
        return responder

    async def _say_group(self, chat_id: str, text: str,
                         quote_to: str | None = None,
                         sender: str = "chatagent") -> SendResult:
        if self._sender is None:
            logger.warning("[chatagent] 没有发送出口，丢弃：%s", text[:40])
            return SendResult(ok=False, error="没有发送出口")
        result = await self._sender(chat_id, text, quote_to=quote_to)
        # 自己说的话也进群记录（记录是全场的真相）
        self.ledger.log_group_message(
            chat_id=chat_id, sender=sender, is_owner=False, kind="text",
            text=text, timestamp=time.time(),
            message_id=getattr(result, "message_id", "") or "")
        return result

    # ------------------------------------------------------------------
    # 静默压缩
    # ------------------------------------------------------------------
    async def _post_turn(self, chat_id: str) -> None:
        """turn 收工后看一眼压力，超阈值就静默压缩（群里无感）。"""
        loop = self._loops.get(chat_id)
        if loop is None:
            return
        key = loop.session_key
        try:
            await loop.wait_idle(key)
            stats = loop.context_stats.get(key)
            if stats is not None and stats.tokens > stats.budget * PRESSURE_PCT:
                await self.compact(chat_id)
        except Exception:
            logger.exception("[chatagent] 压缩检查出错 %s", chat_id)

    async def compact(self, chat_id: str) -> None:
        loop = self._loops.get(chat_id)
        if loop is None:
            return
        key = loop.session_key
        _, cut = self.ledger.get_recorder_state(key)
        rows = self.ledger.since(key, cut)
        if not rows:
            return
        kept = rows[-COMPACT_KEEP:]
        try:
            summary = await self._summarize(rows)
        except Exception as exc:
            logger.warning("[chatagent] 总结调用失败，跳过压缩 %s：%s", chat_id, exc)
            return
        ts = time.time()
        self.ledger.add_turn(
            session_key=key, type=TYPE_INTERNAL,
            text=f'<compact time="{time.strftime("%Y-%m-%d %H:%M")}">'
                 f'{summary}</compact>',
            timestamp=ts)
        self.ledger.set_recorder_state(key, cut_seq=kept[0].seq_id)
        gone = self.ledger.reclaim_blobs(key)
        logger.info("[chatagent] 压缩 %s：留 %d 条 + 备忘，回收图 %d",
                    chat_id, len(kept), gone)

    async def _summarize(self, rows) -> str:
        """打包行（文字 + 图）跑一次总结调用（思考关）。"""
        content: list = [{"type": "text",
                          "text": "\n".join(r.text for r in rows if r.text)}]
        n = 0
        for r in rows:
            for a in r.attachments:
                if a.kind == "image" and a.data and n < COMPACT_MAX_IMAGES:
                    mime = sniff_image_mime(a.data)
                    if mime and self.vision is not None:
                        content.append(self.vision.content_part(a.data, mime))
                        n += 1
        msgs = [{"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": content}]
        result = await self.llm.chat(msgs, reasoning_effort="none")
        return (result.content or "").strip()


def _strip_mentions(text: str) -> str:
    """去掉行首的 @xxx token（指令常写成「@neoagent /activate」）。"""
    out = (text or "").strip()
    while out.startswith("@"):
        sp = out.find(" ")
        out = out[sp + 1:].strip() if sp >= 0 else ""
    return out


def AgentLoopFactory(manager: ChatAgentManager, registry, chat_id: str):
    """按群造一个 chatagent 的 AgentLoop（懒实例化；休眠即销毁）。"""
    from .agent_loop import AgentLoop

    loop = AgentLoop(
        manager.llm, registry, manager.ledger, None,
        identity_file=PACKAGE_DIR / "prompts" / "chatagent.md",
        vision=manager.vision, transcriber=manager.transcriber,
        token_counter=manager.token_counter,
        max_tool_rounds=CHATAGENT_MAX_TOOL_ROUNDS,
        token_budget=CHATAGENT_TOKEN_BUDGET,
        media_roots=[],                     # 聊天工具：不发媒体
        known_channels=manager.known_channels,
        timeline_bucket=f"group:{chat_id}")
    loop.session_key = f"group:{chat_id}"   # 本模块内的便捷名（会话 = 群桶）
    return loop
