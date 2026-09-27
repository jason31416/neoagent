"""agent 循环 = 产品文档的 M（Messenger，本期变体：无 E/R，M 直接持有工具）。

架构要点：
- 每个会话**单活跃 turn**（turn 循环）；turn 中到达的消息不另起任务——
  忙时输入走 steer/redirect（设计稿第八节）：
    · 正在生成（模型请求在飞）→ 取消这次请求（redirect），重渲染重跑，不扣轮数
    · 正在执行工具           → steer：本轮工具跑完、下次渲染自然带上
  因为**每轮模型调用前都从消息库现渲染**，插话（独立用户消息）自动进上下文；
- 回复只从这里出（M 是唯一的声音）；
- 内部消息（E/cron/jev）走 inject_internal() 注入时间线，本期恒空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from ..tools.registry import ToolRegistry
from .events import MessageEvent, ReplyRef
from .ledger import MessageLedger, ToolCallRecord
from .llm import LLMClient
from .render import render_messages

logger = logging.getLogger(__name__)

# 【它是谁】：M 的身份与行为准则（产品文档上下文第 1 段）
IDENTITY_PROMPT = """你是 neoagent，一个跑在微信/飞书里的个人助手，是这个产品里"唯一的声音"。

行为准则：
- 用工具干活：读写文件用 read_file / write_file，跑命令用 exec。不要假装做了没做的事。
- 回复面向聊天窗口：简短、自然、口语化，像朋友发消息，不要写大段报告。
- 需要多步操作时自己一步步做完再汇报，不要每步都问用户。
- 拿不准用户的意图时，简单问一句，不要猜着乱动文件。
- 消息里的 [msg#N | 时间 | 名字] 是会话记录标记；（引用 #N）指向上文某条消息。
- 你自己回复时**绝对不要**输出 [msg#N] 这类标记，正常说话就行。"""

# 兜底：语音转写失败/不可用时正文的占位
_VOICE_FALLBACK = "（语音消息，转写暂不可用）"

MAX_IMAGES_PER_MESSAGE = 4

# 防模仿：模型偶尔学上下文里的 [msg#N | ...] 前缀写进回复，发出前剥掉
_MSG_PREFIX_RE = re.compile(r"^\[msg#\d+ \| \d{4}-\d{2}-\d{2} \d{2}:\d{2} \| [^\]]*\]\s*", re.M)


class SessionState:
    """一个会话的 turn 运行态。"""

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None        # 当前活跃 turn
        self.model_task: Optional[asyncio.Task] = None  # 在飞的模型请求（redirect 的取消对象）
        self.tool_running: bool = False                 # 正在执行工具（steer 的判定）
        self.input_gen: int = 0                          # 输入代数（单调递增）
        self.responder: Optional[Callable] = None       # 回复出口（绑定通道适配器）
        self.last_ack_ts: float = 0.0                   # 忙时回执限频


class AgentLoop:
    """一个核心循环实例服务所有通道、所有会话。"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, ledger: MessageLedger,
                 memory_file: Path, *, vision=None, transcriber=None,
                 max_tool_rounds: int = 12, history_limit: int = 200,
                 max_images: int = MAX_IMAGES_PER_MESSAGE) -> None:
        self.llm = llm
        self.registry = registry
        self.ledger = ledger
        self.memory_file = Path(memory_file)
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
                     responder: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
        """提交一条入站消息（不等待回复；回复经 responder 送回通道）。

        空闲 → 开新 turn；忙 → steer/redirect 注入当前 turn（不起并行任务）。
        quote_fetcher：通道的"按 id 取消息文本"兜底（飞书 message.get）。
        """
        session_key = event.session_key()

        # 1) 预处理：语音转写 → 普通文本；图片上传 vision
        text, attachments = await self._preprocess(event)

        # 2) 引用解析：先查库（指认），取不到才走通道兜底
        reply_to = await self._resolve_reply(event, session_key, quote_fetcher)

        # 3) 记账（事实层）——插话也是一等公民消息（独立 seq、可被引用）
        self.ledger.add(
            session_key=session_key, role="user", message_id=event.id,
            sender_name=event.sender.name or event.sender.id,
            text=text, timestamp=event.timestamp,
            attachments=attachments, reply_to=reply_to,
        )

        st = self._states.setdefault(session_key, SessionState())
        if responder is not None:
            st.responder = responder

        st.input_gen += 1    # 新输入代数：turn 循环据此判断要不要再跑一轮

        if st.task is None or st.task.done():
            st.task = asyncio.get_event_loop().create_task(self._turn_loop(session_key))
        else:
            # 忙时输入
            if st.model_task is not None and not st.model_task.done():
                # redirect：取消在飞的模型请求，重渲染重跑（不扣轮数）
                st.model_task.cancel()
                await self._busy_ack(st, "↪ 已转向当前任务，按你的新消息调整。")
            elif st.tool_running:
                # steer：不杀工具，本轮工具跑完、下次渲染自然带上
                await self._busy_ack(st, "⏩ 已插话，下一步就带上。")
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
        self.ledger.add(session_key=session_key, role="internal", text=text)

    async def _busy_ack(self, st: SessionState, text: str) -> None:
        """忙时回执（限频，防连环插话刷屏）。"""
        now = time.time()
        if now - st.last_ack_ts < 5:
            return
        st.last_ack_ts = now
        if st.responder is not None:
            try:
                await st.responder(text)
            except Exception:
                logger.exception("忙时回执发送失败")

    # ------------------------------------------------------------------
    # turn 循环：一条消息或一批插话共用一轮；回复经 responder 发出
    # ------------------------------------------------------------------
    async def _turn_loop(self, session_key: str) -> None:
        st = self._states[session_key]
        try:
            while True:
                reply, trace, gen_at_render = await self._run_turn(session_key, st)
                self.ledger.add(
                    session_key=session_key, role="assistant", message_id="",
                    sender_name="neoagent", text=reply, tool_trace=trace,
                )
                if st.responder is not None:
                    try:
                        await st.responder(reply)
                    except Exception:
                        logger.exception("回复发送失败")
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

    async def _run_turn(self, session_key: str, st: SessionState):
        """一轮：渲染 → 模型调用/工具循环。每轮模型调用前**重新渲染**，
        所以 turn 中入库的插话自动出现在上下文里（steer/redirect 共用这条性质）。"""
        tools = self.registry.schemas()
        trace: List[ToolCallRecord] = []
        turn_tool_msgs: List[dict] = []   # 本轮的 tool_calls / tool 结果机器件（未入库，本地暂存）
        rounds = 0

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
                # redirect：被新消息取消 → 重渲染重跑；这一步不扣轮数
                logger.info("[turn] 模型请求被转向，重渲染重跑（不扣轮数）")
                st.model_task = None
                continue
            st.model_task = None
            rounds += 1

            if not result.tool_calls:
                return (_sanitize_reply(result.content or "（我好像没想好说什么，稍等下再问我一次）"),
                        trace, gen_at_render)

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
                for tc in result.tool_calls:
                    output = await self._execute_tool(tc.name, tc.arguments)
                    trace.append(ToolCallRecord(name=tc.name, arguments=tc.arguments, result=output))
                    turn_tool_msgs.append({"role": "tool", "tool_call_id": tc.id, "content": output})
            finally:
                st.tool_running = False

        # 轮数打满还没收敛：强制收尾
        logger.warning("工具循环达到上限 %d 轮，强制收尾", self.max_tool_rounds)
        messages = self._render(session_key) + turn_tool_msgs + [{
            "role": "user",
            "content": "（系统提示：工具调用轮数已达上限，请立即基于已有结果给用户一句总结。）",
        }]
        result = await self.llm.chat(messages)
        return (_sanitize_reply(
            result.content or "这活比我想象的绕，我先汇报下进展：工具调用次数超上限了，"
            "你要不要换个说法让我再来一次？"), trace, gen_at_render)

    # ------------------------------------------------------------------
    # 预处理 / 引用解析
    # ------------------------------------------------------------------
    async def _preprocess(self, event: MessageEvent):
        text = event.text
        attachments = list(event.attachments)

        # 语音 → 转写成正文（按普通文本消息处理，音频只是转写原料）。
        # 转写失败降级为占位文本；附件仍记账但渲染时不出附件行。
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

    def _render(self, session_key: str):
        return render_messages(
            self.ledger, session_key,
            identity=IDENTITY_PROMPT,
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


def _sanitize_reply(text: str) -> str:
    """剥掉模型模仿出来的 [msg#N | ...] 行首标记（防模仿的最后一道网）。"""
    return _MSG_PREFIX_RE.sub("", text).strip()
