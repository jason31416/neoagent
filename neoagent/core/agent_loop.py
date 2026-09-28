"""agent 循环 = 产品文档的 M（Messenger，本期变体：无 E/R，M 直接持有工具）。

架构要点：
- 每个会话**单活跃 turn**（turn 循环）；turn 中到达的消息不另起任务——
  忙时输入走 steer/redirect（设计稿第八节，**静默执行、不发回执**）：
    · 正在生成（模型请求在飞）→ 取消这次请求（redirect），重渲染重跑，不扣轮数
    · 正在执行工具           → steer：本轮工具跑完、下次渲染自然带上
  因为**每轮模型调用前都从消息库现渲染**，插话（独立用户消息）自动进上下文；
- 回复只从这里出（M 是唯一的声音）；
- **消息协议**：模型每条要说的话用 <message>…</message> 包起来
  （replyTo="msg#N" 落到通道真引用，其余属性是元数据、一律丢弃），
  可连发多条、可干活中途先发一条；无块 = 静默；标签外文字 = 草稿，丢弃不入账；
- 工具流水（tool_call/tool_result）当轮即落消息库——事实层 = 完整上下文快照；
- 内部消息（E/cron/jev）走 inject_internal() 注入时间线，本期恒空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from ..providers.tokens import HeuristicTokenCounter, TokenAnchor, TokenCounter, count_chat_payload
from ..tools.file_tools import _resolve_in_roots
from ..tools.registry import ToolRegistry
from .events import Attachment, MessageEvent, ReplyRef
from .ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_CALL,
                     TYPE_TOOL_RESULT, TYPE_USER)
from .llm import LLMClient
from .render import TimelineCache, render_messages

logger = logging.getLogger(__name__)

# 【它是谁】：M 的身份与行为准则（产品文档上下文第 1 段）
# ——内容在独立文件 IDENTITY.md（仓库根，可配置 agent.identity_file），这里不重复。

# 兜底：语音转写失败/不可用时正文的占位
_VOICE_FALLBACK = "（语音消息，转写暂不可用）"

MAX_IMAGES_PER_MESSAGE = 4
MAX_BUBBLES_PER_TURN = 8      # 单 turn 气泡上限（防话痨刷屏）

# 消息块抽取：文本块与媒体自闭合标签（<img>/<file>）统一按出现顺序抽取；
# 未闭合/没 src 的坏标签匹配不到 = 草稿，丢
_ITEM_RE = re.compile(
    r"<message([^>]*)>(.*?)</message>|<(img|file)\b([^>]*?)/?>", re.S)
# 输出侧认领的属性：replyTo="msg#N"（落到通道真引用）、src（媒体路径）、name（文件显示名）
_REPLYTO_RE = re.compile(r'replyTo\s*=\s*"msg#(\d+)"')
_SRC_RE = re.compile(r'src\s*=\s*"([^"]*)"')
_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"')

# 媒体类型白名单（mime 前缀）；不在内 → 降级文本
_MEDIA_MIME_PREFIXES = (
    "image/", "video/", "audio/", "text/",
    "application/pdf", "application/json", "application/zip",
    "application/x-tar", "application/gzip", "application/msword",
    "application/vnd.", "application/octet-stream",
)


@dataclass
class OutMsg:
    """模型输出里的一条消息：文本气泡（text）或媒体消息（image/file，独立消息）。"""
    kind: str                       # "text" | "image" | "file"
    text: str = ""
    path: str = ""
    name: str = ""
    quote_seq: Optional[int] = None


def _parse_attrs(attr_text: str) -> tuple:
    q = _REPLYTO_RE.search(attr_text)
    src = _SRC_RE.search(attr_text)
    name = _NAME_RE.search(attr_text)
    return (int(q.group(1)) if q else None,
            src.group(1).strip() if src else "",
            name.group(1).strip() if name else "")


def _extract_messages(content: Optional[str]) -> List[OutMsg]:
    """从模型输出里抽消息（属性只认 replyTo/src/name，其余丢弃；空块丢弃）。

    - <message>…</message> → 文本气泡；
    - <img src="…"/> / <file src="…"/> → 媒体消息（独立消息，不是谁的附件）；
    - 无任何块 = 静默；标签外文字 = 草稿。"""
    if not content:
        return []
    out: List[OutMsg] = []
    for m in _ITEM_RE.finditer(content):
        if m.group(1) is not None:                       # <message>
            text = m.group(2).strip()
            if not text:
                continue
            quote_seq, _src, _name = _parse_attrs(m.group(1))
            out.append(OutMsg("text", text=text, quote_seq=quote_seq))
        else:                                            # <img>/<file>
            quote_seq, src, name = _parse_attrs(m.group(4) or "")
            if not src:
                continue                                 # 没 src 的坏标签按草稿丢
            kind = "image" if m.group(3) == "img" else "file"
            out.append(OutMsg(kind, path=src, name=name, quote_seq=quote_seq))
    return out


class SessionState:
    """一个会话的 turn 运行态。"""

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None        # 当前活跃 turn
        self.model_task: Optional[asyncio.Task] = None  # 在飞的模型请求（redirect 的取消对象）
        self.tool_running: bool = False                 # 正在执行工具（steer 的判定）
        self.input_gen: int = 0                          # 输入代数（单调递增）
        self.responder: Optional[Callable] = None       # 回复出口（绑定通道适配器）：
        #   调用 responder(text, quote_to=平台消息id|None) → SendResult（message_id 回填账）


class AgentLoop:
    """一个核心循环实例服务所有通道、所有会话。"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, ledger: MessageLedger,
                 memory_file: Path, *, identity_file: Optional[Path] = None,
                 vision=None, transcriber=None, token_counter: Optional[TokenCounter] = None,
                 max_tool_rounds: int = 12, token_budget: int = 250_000,
                 max_images: int = MAX_IMAGES_PER_MESSAGE,
                 media_roots: Optional[List[str]] = None,
                 media_max_bytes: int = 20 * 1024 * 1024,
                 workspace: Optional[Path] = None) -> None:
        self.llm = llm
        self.registry = registry
        self.ledger = ledger
        self.memory_file = Path(memory_file)
        self.identity_file = Path(identity_file) if identity_file else None
        self.vision = vision
        self.transcriber = transcriber
        self.token_counter = token_counter or HeuristicTokenCounter()
        self.max_tool_rounds = max_tool_rounds
        self.token_budget = token_budget
        self.max_images = max_images
        self.media_roots = list(media_roots or [])
        self.media_max_bytes = int(media_max_bytes)
        self.workspace = Path(workspace) if workspace else None   # 工作台（相对路径基准）
        self._states: Dict[str, SessionState] = {}
        self._caches: Dict[str, TimelineCache] = {}   # 每会话增量渲染缓存
        self._anchors: Dict[str, TokenAnchor] = {}    # 每会话真值锚定（token 预估）
        self._tools_est: Optional[int] = None        # 工具 schema 估算（常量，算一次）

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def submit(self, event: MessageEvent,
                     *, quote_fetcher: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
                     responder: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
        """提交一条入站消息（不等待回复；回复经 responder 送回通道）。

        空闲 → 开新 turn；忙 → steer/redirect 注入当前 turn（不起并行任务）。
        quote_fetcher：通道的"按 id 取消息文本"兜底（飞书 message.get）。
        """
        session_key = event.session_key()

        # 1) 预处理：语音转写 → 普通文本；图片上传 vision
        text, attachments = await self._preprocess(event)

        # 2) 引用解析：先查库（指认），取不到才走通道兜底
        reply_to = await self._resolve_reply(event, session_key, quote_fetcher)

        # 3) 记账（事实层）：文本一行 + 每个附件一行，各自独立 msg#、可被引用。
        #    语音附件只是转写原料（正文已是转写文本），不单独成行；
        #    平台消息 id 记在首行（引用解析按 id 命中首行）。
        atts = [a for a in attachments if a.kind != "audio"]
        rows: List[tuple] = []
        if text.strip() or not atts:
            rows.append((text, None))
        rows.extend(("", a) for a in atts)
        for i, (row_text, att) in enumerate(rows):
            self.ledger.add(
                session_key=session_key, type=TYPE_USER,
                message_id=event.id if i == 0 else "",
                sender_name=event.sender.name or event.sender.id,
                text=row_text, timestamp=event.timestamp,
                attachments=[att] if att is not None else [],
                reply_to=reply_to if i == 0 else None,
            )

        st = self._states.setdefault(session_key, SessionState())
        if responder is not None:
            st.responder = responder

        st.input_gen += 1    # 新输入代数：turn 循环据此判断要不要再跑一轮

        if st.task is None or st.task.done():
            st.task = asyncio.get_event_loop().create_task(self._turn_loop(session_key))
        else:
            # 忙时输入（静默执行，不发回执）
            if st.model_task is not None and not st.model_task.done():
                # redirect：取消在飞的模型请求，重渲染重跑（不扣轮数）
                st.model_task.cancel()
            elif st.tool_running:
                # steer：不杀工具，本轮工具跑完、下次渲染自然带上
                pass
            # 缝隙（两态都不在）：pending 已置，下次渲染自然带上

    async def wait_idle(self, session_key: str) -> None:
        """等该会话的活跃 turn 结束（CLI 顺序输入用）。"""
        st = self._states.get(session_key)
        if st and st.task is not None and not st.task.done():
            try:
                await st.task
            except Exception:
                pass

    def inject_internal(self, session_key: str, text: str) -> None:
        """内部消息注入（E 的输出 / cron / jev 的提醒）。本期无人调用，接口留好。"""
        self.ledger.add(session_key=session_key, type=TYPE_INTERNAL, text=text)

    # ------------------------------------------------------------------
    # turn 循环：一条消息或一批插话共用一轮；气泡在 _run_turn 内即时发出
    # ------------------------------------------------------------------
    async def _turn_loop(self, session_key: str) -> None:
        st = self._states[session_key]
        try:
            while True:
                gen_at_render = await self._run_turn(session_key, st)
                # 只有"这一轮最终渲染之后又来了新输入"才再跑一轮。
                # redirect 重跑时新输入已随重渲染进上下文并被回答，
                # 代数已含在 gen_at_render 里——不会多跑幽灵轮。
                if st.input_gen <= gen_at_render:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 模型接口/工具层出问题时给用户一句人话，不静默
            logger.exception("turn 循环异常退出 session=%s", session_key)
            if st.responder is not None:
                try:
                    await st.responder(f"我这边出了点状况，这轮没答上来（{type(exc).__name__}）。稍等会儿再问我一次。")
                except Exception:
                    pass
        finally:
            st.model_task = None
            st.tool_running = False

    async def _run_turn(self, session_key: str, st: SessionState) -> int:
        """一轮：渲染 → 模型调用/工具循环，返回最终渲染时的输入代数。

        每轮模型返回后立即处理（边干边说）：
        - <message> 块 → 当场逐条发送 + 各落一条 assistant 账；
        - tool_calls → 落 tool_call 行（一轮数组），执行后逐个落 tool_result 行。
        """
        tools = self.registry.schemas()
        turn_tool_msgs: List[dict] = []   # 本轮的 tool_calls / tool 结果机器件（本地暂存，未入库）
        rounds = 0
        bubbles_sent = 0

        gen_at_render = st.input_gen
        while rounds < self.max_tool_rounds:
            # 事实层现渲染 + 本轮工具机器件（渲染前快照输入代数）。
            # 裁剪预算扣掉：工具 schema + turn 内机器件 + 锚定残差（真值锚定）。
            gen_at_render = st.input_gen
            overhead, extra_est = self._request_overhead(session_key, tools, turn_tool_msgs)
            messages = self._render(session_key, overhead=overhead) + turn_tool_msgs
            est_total = self._caches[session_key].last_cost + extra_est
            predicted = self._anchors[session_key].predict(est_total)

            st.model_task = asyncio.get_event_loop().create_task(
                self.llm.chat(messages, tools=tools))
            try:
                result = await st.model_task
            except asyncio.CancelledError:
                # redirect：被新消息取消 → 重渲染重跑；这一步不扣轮数、不落账
                logger.info("[turn] 模型请求被转向，重渲染重跑（不扣轮数）")
                st.model_task = None
                continue
            st.model_task = None
            rounds += 1
            self._anchor_and_log(session_key, result, est_total, predicted)

            # ① <message> 块：立即发出 + 落账（标签属性丢弃；无块 = 静默）
            bubbles_sent += await self._emit_bubbles(
                session_key, st, result.content, bubbles_sent)

            if not result.tool_calls:
                return gen_at_render

            # ② 工具流水落库：tool_call 行（本轮数组，一条协议消息）
            rid = uuid4().hex[:8]
            calls = [{"call_id": f"call_{rid}_{i}", "name": tc.name, "arguments": tc.arguments}
                     for i, tc in enumerate(result.tool_calls)]
            self.ledger.add(session_key=session_key, type=TYPE_TOOL_CALL,
                            sender_name="neoagent", calls=calls)

            turn_tool_msgs.append({
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in result.tool_calls
                ],
            })

            st.tool_running = True
            try:
                for i, tc in enumerate(result.tool_calls):
                    output = await self._execute_tool(tc.name, tc.arguments)
                    self.ledger.add(session_key=session_key, type=TYPE_TOOL_RESULT,
                                    sender_name="neoagent", text=output,
                                    call_id=calls[i]["call_id"], name=tc.name)
                    turn_tool_msgs.append({"role": "tool", "tool_call_id": tc.id, "content": output})
            finally:
                st.tool_running = False

        # 轮数打满还没收敛：强制收尾（同样只认 <message> 块，模型静默就静默）
        logger.warning("工具循环达到上限 %d 轮，强制收尾", self.max_tool_rounds)
        nudge = {"role": "user",
                 "content": "（系统提示：工具调用轮数已达上限，请立即基于已有结果用 <message> 给用户一句总结。）"}
        overhead, extra_est = self._request_overhead(session_key, tools, turn_tool_msgs)
        messages = self._render(session_key, overhead=overhead) + turn_tool_msgs + [nudge]
        est_total = self._caches[session_key].last_cost + extra_est \
            + count_chat_payload(self.token_counter, [nudge])
        predicted = self._anchors[session_key].predict(est_total)
        result = await self.llm.chat(messages)
        self._anchor_and_log(session_key, result, est_total, predicted)
        await self._emit_bubbles(session_key, st, result.content, bubbles_sent)
        return gen_at_render

    async def _emit_bubbles(self, session_key: str, st: SessionState,
                            content: Optional[str], already: int) -> int:
        """把模型输出的 <message>/<img>/<file> 逐条发出并落账。

        一条 = 一条独立消息（媒体消息不是谁的附件，各自 msg#、可被引用）；
        replyTo="msg#N" → 查库解析成平台消息 id，随发送传给通道挂真引用；
        解析不到（编造 seq / 工具行 / 无平台 id）→ 降级纯发送，不炸。"""
        items = _extract_messages(content)
        sent = 0
        for it in items:
            if already + sent >= MAX_BUBBLES_PER_TURN:
                logger.warning("单 turn 消息超过上限 %d，丢弃剩余 %d 条",
                               MAX_BUBBLES_PER_TURN, len(items) - sent)
                break
            reply_ref, quote_to = self._resolve_bubble_quote(session_key, it.quote_seq)
            if it.kind == "text":
                seq = self.ledger.add(session_key=session_key, type=TYPE_ASSISTANT,
                                      sender_name="neoagent", text=it.text, reply_to=reply_ref)
                result = None
                if st.responder is not None:
                    try:
                        result = await st.responder(it.text, quote_to=quote_to)
                    except Exception:
                        logger.exception("回复发送失败")
                self._backfill_id(session_key, seq, result)
            else:
                await self._send_media_item(session_key, st, it, reply_ref, quote_to)
            sent += 1
            if sent < len(items):
                await asyncio.sleep(0.3)   # 条间小间隔，像连发
        return sent

    def _backfill_id(self, session_key: str, seq: int, result) -> None:
        """回填发出消息的平台 id（以后这条也能被引用）。"""
        sent_id = getattr(result, "message_id", None) if result is not None else None
        if sent_id:
            self.ledger.set_message_id(session_key, seq, sent_id)

    async def _say(self, session_key: str, st: SessionState, text: str) -> None:
        """发一条普通文本消息并落账（降级说明用）。"""
        seq = self.ledger.add(session_key=session_key, type=TYPE_ASSISTANT,
                              sender_name="neoagent", text=text)
        result = None
        if st.responder is not None:
            try:
                result = await st.responder(text)
            except Exception:
                logger.exception("回复发送失败")
        self._backfill_id(session_key, seq, result)

    async def _send_media_item(self, session_key: str, st: SessionState, it: OutMsg,
                               reply_ref, quote_to) -> None:
        """媒体消息：安全校验 → vision 上传（图片）→ 落账 → 走通道 send_media。
        任何一环不成就降级为文本说明（记账记实际发出的内容）。"""
        display = it.name or it.path.rsplit("/", 1)[-1] or it.path
        try:
            kind, mime, p = self._validate_media(it.path)
        except Exception as exc:
            logger.warning("媒体消息被拒 %s：%s", it.path, exc)
            await self._say(session_key, st, f"（{display} 发不出去：{exc}）")
            return

        provider_ref = None
        if kind == "image" and self.vision is not None:
            try:
                provider_ref = await self.vision.attach(str(p))   # 自己发的图也能被回忆
            except Exception as exc:
                logger.warning("发出图片上传 vision 失败 %s：%s", p, exc)

        att = Attachment(kind=kind, filename=it.name or p.name, mime=mime,
                         size=p.stat().st_size, local_path=str(p), provider_ref=provider_ref)
        seq = self.ledger.add(session_key=session_key, type=TYPE_ASSISTANT,
                              sender_name="neoagent", text="", attachments=[att],
                              reply_to=reply_ref)
        result = None
        if st.responder is not None:
            try:
                result = await st.responder("", quote_to=quote_to, media=str(p))
            except Exception:
                logger.exception("媒体发送失败")
        ok = bool(result is not None and getattr(result, "ok", False))
        self._backfill_id(session_key, seq, result)
        if not ok:
            err = getattr(result, "error", None) or "通道不支持"
            await self._say(session_key, st, f"（{display} 发不出去：{err}）")

    def _validate_media(self, path_str: str):
        """媒体发送前的安全校验。返回 (kind, mime, Path)，不通过直接抛异常。"""
        if not self.media_roots:
            raise PermissionError("未配置允许的媒体目录，禁止发送")
        p = _resolve_in_roots(path_str, self.media_roots, base=self.workspace)  # 越界抛 PermissionError
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在：{p}")
        size = p.stat().st_size
        if size > self.media_max_bytes:
            raise ValueError(f"文件 {size // 1024}KB 超过上限 {self.media_max_bytes // 1024}KB")
        mime = mimetypes.guess_type(str(p))[0] or ""
        if not mime.startswith(_MEDIA_MIME_PREFIXES):
            raise ValueError(f"不支持的文件类型：{mime or '未知'}")
        kind = "image" if mime.startswith("image/") else "file"
        return kind, mime, p

    def _resolve_bubble_quote(self, session_key: str, quote_seq: Optional[int]):
        """气泡的 replyTo 解析：(落账用 ReplyRef, 通道引用用平台 id)。"""
        if quote_seq is None:
            return None, None
        rows = self.ledger.by_seqs(session_key, [quote_seq])
        row = rows[0] if rows else None
        if row is None or row.type not in (TYPE_USER, TYPE_ASSISTANT, TYPE_INTERNAL):
            logger.warning("replyTo=msg#%s 解析不到（不存在或非对话行），降级纯文本", quote_seq)
            return None, None
        ref = ReplyRef(
            message_id=row.message_id, sender_name=row.sender_name,
            timestamp=row.timestamp, text=row.text, attachments=row.attachments,
            resolved_from="db", seq_id=row.seq_id)
        if not row.message_id:
            logger.warning("replyTo=msg#%s 没有平台消息 id（发出未回执/旧数据），降级纯文本",
                           quote_seq)
            return ref, None
        return ref, row.message_id

    # ------------------------------------------------------------------
    # 预处理 / 引用解析
    # ------------------------------------------------------------------
    async def _preprocess(self, event: MessageEvent):
        text = event.text
        attachments = list(event.attachments)

        # 语音 → 转写成正文（按普通文本消息处理，音频只是转写原料）。
        # 转写失败降级为占位文本。
        audio_atts = [a for a in attachments if a.kind == "audio"]
        if audio_atts and self.transcriber is not None:
            try:
                transcribed = await self.transcriber.transcribe(
                    audio_atts[0].local_path, audio_atts[0].mime)
                text = transcribed or text or _VOICE_FALLBACK
            except Exception as exc:
                logger.warning("语音转写失败：%s", exc)
                text = text or _VOICE_FALLBACK
        elif audio_atts:
            text = text or _VOICE_FALLBACK

        # 图片 → 上传 vision 拿句柄（DeepSeek file_id），历史可复用
        images = [a for a in attachments if a.kind == "image"][:self.max_images]
        if images and self.vision is not None:
            for a in images:
                try:
                    a.provider_ref = await self.vision.attach(a.local_path)
                except Exception as exc:
                    logger.warning("图片上传失败 %s：%s", a.filename, exc)
                    a.provider_ref = None  # 渲染层降级为"（图内容不可用）"

        return text, attachments

    async def _resolve_reply(self, event: MessageEvent, session_key: str,
                             quote_fetcher) -> Optional[ReplyRef]:
        """引用解析：库里指认（零成本）→ 通道内联（微信自带）→ 通道 API（飞书兜底）。"""
        ref = event.reply_to
        if ref is None:
            return None

        # ① 查库（主路径）：凡是收到过的消息都在库里
        hit = self.ledger.find(session_key, ref.message_id)
        if hit is not None:
            ref.seq_id = hit.seq_id
            ref.sender_name = ref.sender_name or hit.sender_name
            ref.timestamp = ref.timestamp or hit.timestamp
            ref.text = ref.text or hit.text
            if ref.resolved_from == "unknown":
                ref.resolved_from = "db"

        # ② 文本还没有（库没有 + 微信内联也没有）→ 通道 API 兜底（飞书 message.get）
        if not ref.text and quote_fetcher is not None:
            try:
                fetched = await quote_fetcher(ref.message_id)
                if fetched:
                    ref.text = fetched
                    ref.resolved_from = "api"
            except Exception as exc:
                logger.warning("引用消息拉取失败 %s：%s", ref.message_id, exc)

        return ref

    # ------------------------------------------------------------------
    # 渲染 / 工具执行
    # ------------------------------------------------------------------
    def _memory_text(self) -> str:
        """【记忆】段：记忆文件全文（M 自己用 write_file 维护，空文件整段省略）。"""
        try:
            return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
        except OSError as exc:
            logger.warning("记忆文件读取失败：%s", exc)
            return ""

    def _identity_text(self) -> str:
        """【它是谁】段：IDENTITY.md 全文（独立文件，可配置 agent.identity_file）。"""
        if self.identity_file is None:
            return ""
        try:
            return self.identity_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("身份文件读取失败 %s：%s", self.identity_file, exc)
            return ""

    def _render(self, session_key: str, overhead: int = 0):
        cache = self._caches.setdefault(session_key, TimelineCache())
        identity = self._identity_text()
        if self.workspace is not None:
            identity += f"\n\n（你的工作目录：{self.workspace}，产出物放这里；文件工具的相对路径以它为基准）"
        return render_messages(
            self.ledger, session_key,
            identity=identity,
            memory_text=self._memory_text(),
            budget=self.token_budget,
            counter=self.token_counter,
            vision=self.vision,
            cache=cache,
            overhead=overhead,
        )

    def _request_overhead(self, session_key: str, tools, turn_tool_msgs) -> tuple:
        """(裁剪用开销, 非时间线估算)。

        开销 = 工具 schema + turn 内工具机器件 + 真值锚定残差——都是"时间线之外
        但真实占用上下文"的部分，得挤占时间线预算。
        """
        if self._tools_est is None:
            self._tools_est = count_chat_payload(self.token_counter, tools=tools)
        extra_est = self._tools_est + count_chat_payload(self.token_counter, turn_tool_msgs)
        anchor = self._anchors.setdefault(session_key, TokenAnchor())
        return extra_est + anchor.offset, extra_est

    def _anchor_and_log(self, session_key: str, result, est_total: int, predicted: int) -> None:
        """usage 统计 + 真值锚定：拿到接口真实 prompt_tokens 就重新锚定。"""
        u = getattr(result, "usage", None) or {}
        logger.info("[llm] usage prompt=%s completion=%s（锚定预估=%d，本地估算=%d）",
                    u.get("prompt_tokens"), u.get("completion_tokens"), predicted, est_total)
        if u.get("prompt_tokens"):
            self._anchors[session_key].update(u["prompt_tokens"], est_total)

    async def _execute_tool(self, name: str, arguments_json: str) -> str:
        """执行一个工具调用，永远返回字符串（错误也转成字符串回给模型自纠）。"""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            return f"工具参数不是合法 JSON：{e}"

        logger.info("调用工具 %s(%s)", name, arguments_json[:200])
        try:
            output = await self.registry.call(name, args)
        except Exception as e:  # 工具内部炸了也不能炸掉整个循环
            logger.exception("工具 %s 执行异常", name)
            output = f"工具执行异常：{e}"

        if len(output) > 20000:
            output = output[:20000] + "\n…（输出过长已截断）"

        snippet = output[:500] + ("…" if len(output) > 500 else "")
        logger.info("工具 %s 结果（%d 字符）：%s", name, len(output), snippet)
        return output
