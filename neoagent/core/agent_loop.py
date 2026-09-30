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
import random
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from ..providers.tokens import HeuristicTokenCounter, TokenAnchor, TokenCounter, count_chat_payload
from ..providers.vision import normalize_image
from ..tools.file_tools import _resolve_in_roots
from ..tools.registry import ToolOutput, ToolRegistry
from . import modellog
from .events import Attachment, MessageEvent
from .frames import (Bubble, attachment_notes, finalize_assistant, finalize_user,
                     recall_tag)
from .ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_RESULT,
                     TYPE_USER)
from .llm import LLMClient
from .render import render_messages

logger = logging.getLogger(__name__)

# 【它是谁】：M 的身份与行为准则（产品文档上下文第 1 段）
# ——内容在独立文件 prompts/messenger.md（可配置 agent.identity_file），这里不重复。

# 兜底：语音转写失败/不可用时正文的占位
_VOICE_FALLBACK = "（语音消息，转写暂不可用）"

MAX_IMAGES_PER_MESSAGE = 4
MAX_BUBBLES_PER_TURN = 8      # 单 turn 气泡上限（防话痨刷屏）

# 媒体类型白名单（mime 前缀）；不在内 → 降级文本
_MEDIA_MIME_PREFIXES = (
    "image/", "video/", "audio/", "text/",
    "application/pdf", "application/json", "application/zip",
    "application/x-tar", "application/gzip", "application/msword",
    "application/vnd.", "application/octet-stream",
)




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
                 max_tool_rounds: int = 90, token_budget: int = 250_000,
                 max_images: int = MAX_IMAGES_PER_MESSAGE,
                 media_roots: Optional[List[str]] = None,
                 media_max_bytes: int = 20 * 1024 * 1024,
                 bubble_gap_min: float = 0.0, bubble_gap_max: float = 0.0,
                 workspace: Optional[Path] = None,
                 effort_provider: Optional[Callable[[], str]] = None,
                 recorder=None) -> None:
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
        # 气泡连发间隔（秒）：0=不间隔（smoke 用默认）；产品默认 1.5–2.5 走 config 注入
        self.bubble_gap_min = float(bubble_gap_min)
        self.bubble_gap_max = float(bubble_gap_max)
        self.workspace = Path(workspace) if workspace else None   # 工作台（相对路径基准）
        # 思考档位（reasoning_effort）：每轮请求前现取 → 改配置即生效，可随时切换
        # 思考/非思考混用；默认 "medium"（none=不思考）
        self.effort_provider = effort_provider or (lambda: "medium")
        self.recorder = recorder                # R（Recorder）：紧急压缩钩子用（可空）
        self._states: Dict[str, SessionState] = {}
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

        # 2) 入库定稿：引用解析（平台 id → 气泡）+ 净化/气泡编号/recall 固化
        await self._ingest_user(event, session_key, text, attachments, quote_fetcher)

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
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(text, sender="internal", bubble_id=bid, ts=time.time())
        self.ledger.add_turn(session_key=session_key, type=TYPE_INTERNAL, text=final)

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
        - 输出定稿入库（净化/气泡编号/recall 固化，一条 assistant 行）→ 气泡逐条投递；
        - tool_calls 随 assistant 行落账（calls 数组），执行后逐个落 tool_result 行。
        """
        tools = self.registry.schemas()
        turn_seen_imgs: set = set()       # 本轮已嵌图（同轮同图去重）
        rounds = 0
        bubbles_sent = 0

        gen_at_render = st.input_gen
        compressed = False          # 紧急压缩一次 turn 最多试一次（R 没进展就走兜底）
        while rounds < self.max_tool_rounds:
            # 事实层现渲染（工具流水当轮已落库，渲染即含，不再另拼机器件——
            # 曾经的 turn_tool_msgs 暂存链会造成同一轮流水在请求里出现两次）。
            # 裁剪预算扣掉：工具 schema + 锚定残差（真值锚定）。
            gen_at_render = st.input_gen
            overhead, extra_est = self._request_overhead(session_key, tools)
            messages, timeline_cost, trimmed = self._render(session_key, overhead=overhead)

            # 紧急压缩（harness 式，罕见）：装配被迫砍最老 → 挂起 turn、同步跑 R 整理。
            # "压缩上下文中/压缩完成"是系统播报，直发不过 M 的模型。
            if trimmed and self.recorder is not None \
                    and not compressed:
                compressed = True
                _, cut_before = self.ledger.get_recorder_state(session_key)
                if st.responder is not None:
                    await st.responder("压缩上下文中")
                logger.info("[turn] 预算吃紧，同步整理 session=%s", session_key)
                await self.recorder.run(session_key, "fast")
                _, cut_after = self.ledger.get_recorder_state(session_key)
                if st.responder is not None:
                    await st.responder("压缩完成")
                if cut_after > cut_before:
                    continue        # 切点推进 → 重渲染继续，不扣轮数
                logger.warning("[turn] 整理未推进切点，走渲染端兜底截断（欠一次整理）")

            est_total = timeline_cost + extra_est
            predicted = self._anchors[session_key].predict(est_total)

            # 请求发出前先落 modellog（400 翻车现场也留证据；未开启时空转）
            effort = self.effort_provider()
            modellog.dump("m", session_key, {"round": rounds, "messages": messages})
            st.model_task = asyncio.get_event_loop().create_task(
                self.llm.chat(messages, tools=tools, reasoning_effort=effort))
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

            # ① 一轮输出定稿入库（含思考草稿，一份/轮）+ 气泡逐条投递
            calls = None
            if result.tool_calls:
                rid = uuid4().hex[:8]
                calls = [{"call_id": f"call_{rid}_{i}", "name": tc.name,
                          "arguments": tc.arguments}
                         for i, tc in enumerate(result.tool_calls)]
            assistant_seq, sent = await self._emit_round(
                session_key, st, result, calls, bubbles_sent)
            bubbles_sent += sent

            if not result.tool_calls:
                return gen_at_render

            st.tool_running = True
            try:
                for i, tc in enumerate(result.tool_calls):
                    output, images = await self._execute_tool(tc.name, tc.arguments)
                    atts, note = self._embed_images(images, turn_seen_imgs)
                    if note:
                        output += note
                    self.ledger.add_turn(           # ② 结果行挂同组（tool 链原子）
                        session_key=session_key, type=TYPE_TOOL_RESULT,
                        text=output, call_id=calls[i]["call_id"],
                        tool_name=tc.name, attachments=atts,
                        group_id=assistant_seq)
            finally:
                st.tool_running = False

        # 轮数打满还没收敛：强制收尾（同样只认 <message> 块，模型静默就静默）
        logger.warning("工具循环达到上限 %d 轮，强制收尾", self.max_tool_rounds)
        nudge = {"role": "user",
                 "content": "（系统提示：工具调用轮数已达上限，请立即基于已有结果用 <message> 给用户一句总结。）"}
        overhead, extra_est = self._request_overhead(session_key, tools)
        messages = self._render(session_key, overhead=overhead) + [nudge]
        est_total = self._caches[session_key].last_cost + extra_est \
            + count_chat_payload(self.token_counter, [nudge])
        predicted = self._anchors[session_key].predict(est_total)
        effort = self.effort_provider()
        modellog.dump("m", session_key, {
            "round": rounds, "phase": "max_tool_rounds", "messages": messages})
        result = await self.llm.chat(messages, reasoning_effort=effort)
        self._anchor_and_log(session_key, result, est_total, predicted)
        sent, first_seq = await self._emit_bubbles(session_key, st, result.content, bubbles_sent)
        if result.reasoning and first_seq:
            self.ledger.attach_reasoning(session_key, first_seq, result.reasoning)
        return gen_at_render

    # ------------------------------------------------------------------
    # 入库定稿 / 气泡投递
    # ------------------------------------------------------------------
    @staticmethod
    def _key_parts(session_key: str) -> tuple:
        channel, _, chat = session_key.partition(":")
        return channel, chat

    def _alloc_bubble(self, session_key: str) -> Callable[[], int]:
        """定稿器用的气泡编号分配口（连续、持久化）。"""
        return lambda: self.ledger.alloc_bubbles(session_key, 1)[0]

    def _bubble_lookup(self, session_key: str):
        """定稿器用的气泡查询口（replyTo 验真 + recall 快照）。"""
        return lambda bid: self.ledger.get_bubble(session_key, bid)

    async def _ingest_user(self, event: MessageEvent, session_key: str, text: str,
                           attachments: List[Attachment], quote_fetcher) -> None:
        """用户消息入库定稿：引用解析（平台 id → 气泡）+ 编号 + recall 固化。"""
        atts = [a for a in attachments if a.kind != "audio"]
        recall = ""
        ref = event.reply_to
        if ref is not None:
            hit = self.ledger.find_bubble(session_key, ref.message_id)
            if hit is not None:
                recall = recall_tag(hit.bubble_id, hit.sender, hit.timestamp,
                                    hit.content)
            else:
                # 库没有：通道内联文本 / 通道 API 兜底（飞书 message.get）
                if not ref.text and quote_fetcher is not None:
                    try:
                        fetched = await quote_fetcher(ref.message_id)
                        if fetched:
                            ref.text = fetched
                    except Exception as exc:
                        logger.warning("引用消息拉取失败 %s：%s", ref.message_id, exc)
                if ref.text:
                    recall = recall_tag(None, ref.sender_name or "未知",
                                        ref.timestamp or event.timestamp, ref.text)
        body = (text or "") + attachment_notes(attachments)
        sender = event.sender.name or event.sender.id
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(body, sender=sender, bubble_id=bid,
                              ts=event.timestamp, recall=recall)
        seq = self.ledger.add_turn(session_key=session_key, type=TYPE_USER,
                                   text=final, attachments=atts,
                                   timestamp=event.timestamp)
        kind = ("text" if text.strip()
                else "image" if any(a.kind == "image" for a in atts) else "file")
        channel, chat = self._key_parts(session_key)
        self.ledger.add_bubble(
            session_key=session_key, bubble_id=bid, seq_id=seq, direction="in",
            platform=channel, chat_id=chat, message_id=event.id,
            kind=kind, content=text or "", sender=sender, timestamp=event.timestamp)

    async def _emit_round(self, session_key: str, st: SessionState, result,
                          calls: Optional[List[dict]], already: int) -> tuple:
        """一轮模型输出：定稿入库（assistant 行 + 气泡行）→ 逐个气泡投递。

        返回 (assistant 行 seq|None, 实发条数)。思考草稿随 assistant 行入库
        （回传锚点，一份/轮）；完全静默的轮不落行。"""
        ts = time.time()
        final, bubbles = finalize_assistant(
            result.content, ts=ts,
            alloc=self._alloc_bubble(session_key),
            lookup=self._bubble_lookup(session_key))
        if not final.strip() and not calls:
            return None, 0
        seq = self.ledger.add_turn(
            session_key=session_key, type=TYPE_ASSISTANT, text=final,
            reasoning=result.reasoning or "", calls=calls, timestamp=ts)
        channel, chat = self._key_parts(session_key)
        for b in bubbles:
            self.ledger.add_bubble(
                session_key=session_key, bubble_id=b.bubble_id, seq_id=seq,
                direction="out", platform=channel, chat_id=chat, kind=b.kind,
                content=b.content, sender="neoagent", timestamp=ts)
        sent = await self._deliver_bubbles(session_key, st, bubbles, already)
        return seq, sent

    async def _deliver_bubbles(self, session_key: str, st: SessionState,
                               bubbles: List[Bubble], already: int) -> int:
        """气泡逐条投递（条间随机停顿）；平台回执 id 回填气泡行。"""
        sent = 0
        for b in bubbles:
            if already + sent >= MAX_BUBBLES_PER_TURN:
                logger.warning("单 turn 消息超过上限 %d，丢弃剩余 %d 条",
                               MAX_BUBBLES_PER_TURN, len(bubbles) - sent)
                self.ledger.set_bubble_meta(session_key, b.bubble_id,
                                            {"skipped": "over cap"})
                continue
            quote_to = self._bubble_platform_id(session_key, b.reply_to)
            if b.kind == "text":
                result = None
                if st.responder is not None:
                    try:
                        result = await st.responder(b.content, quote_to=quote_to)
                    except Exception:
                        logger.exception("回复发送失败")
                self._backfill_bubble(session_key, b.bubble_id, result)
            else:
                await self._deliver_media(session_key, st, b, quote_to)
            sent += 1
            if sent < len(bubbles) and self.bubble_gap_max > 0:
                # 条间随机间隔：真实感 + 给用户阅读时间（每次都不一样，别用固定值）
                await asyncio.sleep(random.uniform(self.bubble_gap_min,
                                                   self.bubble_gap_max))
        return sent

    def _backfill_bubble(self, session_key: str, bubble_id: int, result) -> None:
        """回填气泡的平台消息 id（以后这条也能被真引用）；失败记投递元数据。"""
        sent_id = getattr(result, "message_id", None) if result is not None else None
        if sent_id:
            self.ledger.set_bubble_message_id(session_key, bubble_id, sent_id)
        elif result is None or not getattr(result, "ok", False):
            err = getattr(result, "error", None) or "发送失败"
            self.ledger.set_bubble_meta(session_key, bubble_id, {"error": str(err)})

    def _bubble_platform_id(self, session_key: str,
                            bubble_id: Optional[int]) -> Optional[str]:
        """replyTo 气泡 → 平台消息 id（通道真引用）；拿不到就降级纯文本。"""
        if bubble_id is None:
            return None
        hit = self.ledger.get_bubble(session_key, int(bubble_id))
        if hit is None or not hit.message_id:
            logger.warning("replyTo=msg#%s 没有平台消息 id，降级纯文本", bubble_id)
            return None
        return hit.message_id

    async def _deliver_media(self, session_key: str, st: SessionState, b: Bubble,
                             quote_to) -> None:
        """媒体气泡：安全校验 → 走通道 send_media；不成降级为文本说明（_say）。"""
        display = b.name or b.src.rsplit("/", 1)[-1] or b.src
        try:
            _kind, _mime, p = self._validate_media(b.src)
        except Exception as exc:
            logger.warning("媒体消息被拒 %s：%s", b.src, exc)
            await self._say(session_key, st, f"（{display} 发不出去：{exc}）")
            return
        result = None
        if st.responder is not None:
            try:
                result = await st.responder("", quote_to=quote_to, media=str(p))
            except Exception:
                logger.exception("媒体发送失败")
        self._backfill_bubble(session_key, b.bubble_id, result)
        if not (result is not None and getattr(result, "ok", False)):
            err = getattr(result, "error", None) or "通道不支持"
            await self._say(session_key, st, f"（{display} 发不出去：{err}）")

    async def _say(self, session_key: str, st: SessionState, text: str) -> int:
        """发一条系统生成的文本消息并定稿入库（降级说明用），返回气泡编号。"""
        ts = time.time()
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(text, sender="neoagent", bubble_id=bid, ts=ts)
        seq = self.ledger.add_turn(session_key=session_key, type=TYPE_ASSISTANT,
                                   text=final, timestamp=ts)
        channel, chat = self._key_parts(session_key)
        self.ledger.add_bubble(session_key=session_key, bubble_id=bid, seq_id=seq,
                               direction="out", platform=channel, chat_id=chat,
                               kind="text", content=text, sender="neoagent",
                               timestamp=ts)
        result = None
        if st.responder is not None:
            try:
                result = await st.responder(text)
            except Exception:
                logger.exception("回复发送失败")
        self._backfill_bubble(session_key, bid, result)
        return bid

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

        # 图片 → 读字节入账（blobs 内容寻址；渲染时现编码进上下文，
        # 本地删/移文件不影响上下文还原）。格式归一化：不支持的图绝不进上下文
        # （一张坏图会让之后每个请求 400）。
        images = [a for a in attachments if a.kind == "image"][:self.max_images]
        for a in images:
            if a.local_path and not a.data:
                try:
                    raw = Path(a.local_path).read_bytes()
                except OSError as exc:
                    logger.warning("图片读取失败 %s：%s", a.filename, exc)  # 渲染层降级"（图内容未附）"
                    continue
                norm = normalize_image(raw, a.mime)
                if norm is None:
                    logger.warning("图片格式不支持，不进上下文 %s（mime=%s）",
                                   a.filename, a.mime or "未知")
                    continue
                a.data, a.mime = norm

        return text, attachments


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
        """【它是谁】段：prompts/messenger.md 全文（可配置 agent.identity_file）。"""
        if self.identity_file is None:
            return ""
        try:
            return self.identity_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("身份文件读取失败 %s：%s", self.identity_file, exc)
            return ""

    def _render(self, session_key: str, overhead: int = 0):
        """装配一次请求 → (messages, 时间线估算 token, 是否被迫裁剪)。"""
        identity = self._identity_text()
        if self.workspace is not None:
            identity += f"\n\n（你的工作目录：{self.workspace}，产出物放这里；文件工具的相对路径以它为基准）"
        _, cut_seq = self.ledger.get_recorder_state(session_key)   # R 的上下文切点
        return render_messages(
            self.ledger, session_key,
            identity=identity,
            memory_text=self._memory_text(),
            budget=self.token_budget,
            counter=self.token_counter,
            vision=self.vision,
            overhead=overhead,
            cut_seq=cut_seq,
        )

    def _request_overhead(self, session_key: str, tools) -> tuple:
        """(裁剪用开销, 非时间线估算)。

        开销 = 工具 schema + 真值锚定残差——都是"时间线之外
        但真实占用上下文"的部分，得挤占时间线预算。
        （工具流水在时间线里，由渲染计价，不在此重复计入。）
        """
        if self._tools_est is None:
            self._tools_est = count_chat_payload(self.token_counter, tools=tools)
        extra_est = self._tools_est
        anchor = self._anchors.setdefault(session_key, TokenAnchor())
        return extra_est + anchor.offset, extra_est

    def _anchor_and_log(self, session_key: str, result, est_total: int, predicted: int) -> None:
        """usage 统计 + 真值锚定：拿到接口真实 prompt_tokens 就重新锚定。"""
        u = getattr(result, "usage", None) or {}
        logger.info("[llm] usage prompt=%s completion=%s（reasoning=%s，锚定预估=%d，本地估算=%d）",
                    u.get("prompt_tokens"), u.get("completion_tokens"),
                    u.get("reasoning_tokens", 0), predicted, est_total)
        if u.get("prompt_tokens"):
            self._anchors[session_key].update(u["prompt_tokens"], est_total)

    async def _execute_tool(self, name: str, arguments_json: str) -> tuple:
        """执行一个工具调用，返回 (文字结果, 图片路径列表)；错误也转成字符串回给模型自纠。"""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            return f"工具参数不是合法 JSON：{e}", []

        try:
            output = await self.registry.call(name, args)
        except Exception as e:  # 工具内部炸了也不能炸掉整个循环
            logger.exception("工具 %s 执行异常", name)
            output = f"工具执行异常：{e}"

        images: List[str] = []
        if isinstance(output, ToolOutput):
            images = list(output.images or [])
            output = output.text

        if len(output) > 20000:
            output = output[:20000] + "\n…（输出过长已截断）"

        return output, images

    def _embed_images(self, paths: List[str], seen: set) -> tuple:
        """工具结果里的图片：读字节备落账/入上下文；同轮同图去重（hermes 同款防重复嵌）。
        返回 (附件列表, 附加说明)。"""
        atts: List[Attachment] = []
        notes: List[str] = []
        for p in paths:
            key = str(Path(p).resolve())
            if key in seen:
                notes.append(f"\n（图 {Path(p).name} 本轮回上文已附，不重复）")
                continue
            seen.add(key)
            try:
                raw = Path(p).read_bytes()
            except OSError as exc:
                notes.append(f"\n（图 {Path(p).name} 读取失败：{exc}）")
                continue
            norm = normalize_image(raw, mimetypes.guess_type(p)[0] or "")
            if norm is None:
                notes.append(f"\n（图 {Path(p).name} 格式不支持（仅 webp/png/jpeg/gif），看不了）")
                continue
            data, mime = norm
            atts.append(Attachment(kind="image", filename=Path(p).name, mime=mime,
                                   size=len(data), local_path=p, data=data))
        return atts, "".join(notes)
