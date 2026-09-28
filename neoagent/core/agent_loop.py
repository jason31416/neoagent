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
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from ..tools.registry import ToolRegistry
from .events import MessageEvent, ReplyRef
from .ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_CALL,
                     TYPE_TOOL_RESULT, TYPE_USER)
from .llm import LLMClient
from .render import render_messages

logger = logging.getLogger(__name__)

# 【它是谁】：M 的身份与行为准则（产品文档上下文第 1 段）
# ——内容在独立文件 IDENTITY.md（仓库根，可配置 agent.identity_file），这里不重复。

# 兜底：语音转写失败/不可用时正文的占位
_VOICE_FALLBACK = "（语音消息，转写暂不可用）"

MAX_IMAGES_PER_MESSAGE = 4
MAX_BUBBLES_PER_TURN = 8      # 单 turn 气泡上限（防话痨刷屏）

# 消息块抽取：非贪婪；未闭合标签匹配不到 = 草稿，丢
_MSG_BLOCK_RE = re.compile(r"<message([^>]*)>(.*?)</message>", re.S)
# 输出侧唯一认领的属性：replyTo="msg#N"（其余属性丢弃，模型自编一概不采信）
_REPLYTO_RE = re.compile(r'replyTo\s*=\s*"msg#(\d+)"')


def _extract_messages(content: Optional[str]) -> List[tuple]:
    """从模型输出里抽 <message> 块 → [(正文, 引用 seq|None), …]。

    属性里只认 replyTo="msg#N"（落到通道真引用）；其余属性丢弃、空块丢弃。
    无块 = 静默。"""
    if not content:
        return []
    out = []
    for m in _MSG_BLOCK_RE.finditer(content):
        text = m.group(2).strip()
        if not text:
            continue
        q = _REPLYTO_RE.search(m.group(1))
        out.append((text, int(q.group(1)) if q else None))
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
                 vision=None, transcriber=None,
                 max_tool_rounds: int = 12, history_limit: int = 200,
                 max_images: int = MAX_IMAGES_PER_MESSAGE) -> None:
        self.llm = llm
        self.registry = registry
        self.ledger = ledger
        self.memory_file = Path(memory_file)
        self.identity_file = Path(identity_file) if identity_file else None
        self.vision = vision
        self.transcriber = transcriber
        self.max_tool_rounds = max_tool_rounds
        self.history_limit = history_limit
        self.max_images = max_images
        self._states: Dict[str, SessionState] = {}

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
            # 事实层现渲染 + 本轮工具机器件（渲染前快照输入代数）
            gen_at_render = st.input_gen
            messages = self._render(session_key) + turn_tool_msgs

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
        messages = self._render(session_key) + turn_tool_msgs + [{
            "role": "user",
            "content": "（系统提示：工具调用轮数已达上限，请立即基于已有结果用 <message> 给用户一句总结。）",
        }]
        result = await self.llm.chat(messages)
        await self._emit_bubbles(session_key, st, result.content, bubbles_sent)
        return gen_at_render

    async def _emit_bubbles(self, session_key: str, st: SessionState,
                            content: Optional[str], already: int) -> int:
        """把 <message> 块逐条发出并落账（一条气泡 = 一条 assistant 账，独立 msg#）。

        replyTo="msg#N" → 查库解析成平台消息 id，随 responder 传给通道挂真引用；
        解析不到（编造 seq / 工具行 / 无平台 id）→ 降级纯文本，不炸。"""
        bubbles = _extract_messages(content)
        sent = 0
        for text, quote_seq in bubbles:
            if already + sent >= MAX_BUBBLES_PER_TURN:
                logger.warning("单 turn 气泡超过上限 %d，丢弃剩余 %d 条",
                               MAX_BUBBLES_PER_TURN, len(bubbles) - sent)
                break

            reply_ref, quote_to = self._resolve_bubble_quote(session_key, quote_seq)
            seq = self.ledger.add(session_key=session_key, type=TYPE_ASSISTANT,
                                  sender_name="neoagent", text=text, reply_to=reply_ref)
            result = None
            if st.responder is not None:
                try:
                    result = await st.responder(text, quote_to=quote_to)
                except Exception:
                    logger.exception("回复发送失败")
            # 回填发出消息的平台 id（以后这条气泡也能被引用）
            sent_id = getattr(result, "message_id", None) if result is not None else None
            if sent_id:
                self.ledger.set_message_id(session_key, seq, sent_id)
            sent += 1
            if sent < len(bubbles):
                await asyncio.sleep(0.3)   # 条间小间隔，像连发
        return sent

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

    def _render(self, session_key: str):
        return render_messages(
            self.ledger, session_key,
            identity=self._identity_text(),
            memory_text=self._memory_text(),
            window=self.history_limit,
            vision=self.vision,
        )

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
