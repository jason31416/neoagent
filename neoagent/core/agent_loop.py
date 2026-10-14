"""agent 循环 = 产品文档的 M（Messenger，本期变体：无 E/R，M 直接持有工具）。

架构要点：
- 每个会话**单活跃 turn**（turn 循环）；turn 中到达的消息不另起任务——
  忙时输入走 steer/redirect（设计稿第八节，**静默执行、不发回执**）：
    · 正在生成（模型请求在飞）→ 取消这次请求（redirect），重渲染重跑，不扣轮数
    · 正在执行工具           → steer：本轮工具跑完、下次渲染自然带上
  因为**每轮模型调用前都从消息库现渲染**，插话（独立用户消息）自动进上下文；
- 回复只从这里出（M 是唯一的声音）；
- **消息协议**：模型每条要说的话用 <message>…</message> 包起来
  （replyTo="msg#N" 落到通道真引用，channel="feishu" 强制发某频道、不写发到
  用户最后说话的频道，其余属性是元数据、一律丢弃），
  可连发多条、可干活中途先发一条；无块 = 静默；标签外文字 = 碎碎念，原样留存但不发送；
- **/ 指令不进模型**：照常入账，指令解析器直接回 system 消息（见 core/commands.py）；
- 工具流水（tool_call/tool_result）当轮即落消息库——事实层 = 完整上下文快照；
- 内部消息走 inject_internal()（指令回执等，role=system）；自我唤醒（闹钟/哨兵）
  走 inject_wake()（<wake> 块，role=user 的系统消息）。
"""

from __future__ import annotations

import asyncio
import random
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from ..providers.tokens import HeuristicTokenCounter, TokenAnchor, TokenCounter, count_chat_payload
from ..providers.vision import normalize_image
from ..tools.file_tools import _resolve_in_roots
from ..tools.registry import ToolOutput, ToolRegistry, clip_tool_result
from . import commands, compaction, modellog
from .events import Attachment, MessageEvent
from .frames import (Bubble, attachment_notes, finalize_assistant, finalize_user,
                     recall_tag)
from .ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_RESULT,
                     TYPE_USER, TYPE_WAKE)
from .llm import LLMClient
from .compaction import CompactResult
from .recorder import row_line
from .render import ContextStats, render_messages
from .skills import list_names

logger = logging.getLogger(__name__)

# 【它是谁】：M 的身份、行为准则与人格（产品文档上下文第 1 段）
# ——内置部分在包内 prompts/messenger.md（协议/工作方式，随代码仓库走），
# 人格在用户家目录 personality.md（唯一用户可改的提示词）；拼接：messenger → personality。

# 兜底：语音转写失败/不可用时正文的占位
_VOICE_FALLBACK = "（语音消息，转写暂不可用）"

# 干活静默催报：只挂在本次请求尾巴上（不落账本），催模型用 <message> 报进度
_PROGRESS_PING = ("（系统提示：距上次回复用户已经有一会了，"
                  "请尽量及时跟用户汇报进度）")

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
        self.busy: bool = False                         # turn 进行中（含入库/插话窗口）
        self.done_at: Optional[float] = None            # 上次 turn 收工时刻（R 快整理锚点）
        self.input_gen: int = 0                          # 输入代数（单调递增，落账即+1）
        self.kicked_input_gen: int = 0                   # 已放行开工的输入代数（攒批屏障）
        self.kick_event: asyncio.Event = asyncio.Event()  # kick 信号（每次 kick 换新事件）
        self.responder: Optional[Callable] = None       # 回复出口（绑定通道适配器）：
        #   调用 responder(text, quote_to=平台消息id|None) → SendResult（message_id 回填账）
        self.user_waiting: bool = False                 # 用户说了话、还没答完（催报的门槛）
        self.waiting_since: Optional[float] = None      # 用户开始等的时刻（还没说过话时的基准）
        self.last_spoke_at: Optional[float] = None      # 上次真给用户发消息的时刻


class AgentLoop:
    """一个核心循环实例服务所有通道、所有会话。"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, ledger: MessageLedger,
                 memory_file: Optional[Path] = None, *, identity_file: Optional[Path] = None,
                 personality_file: Optional[Path] = None,
                 vision=None, transcriber=None, token_counter: Optional[TokenCounter] = None,
                 max_tool_rounds: int = 90, token_budget: int = 250_000,
                 max_images: int = MAX_IMAGES_PER_MESSAGE,
                 media_roots: Optional[List[str]] = None,
                 media_max_bytes: int = 20 * 1024 * 1024,
                 bubble_gap_min: float = 0.0, bubble_gap_max: float = 0.0,
                 progress_ping_seconds: float = 30,
                 workspace: Optional[Path] = None,
                 effort_provider: Optional[Callable[[], str]] = None,
                 effort_setter: Optional[Callable[[str], bool]] = None,
                 known_channels: Optional[List[str]] = None,
                 timeline_bucket: str = "",
                 recorder=None) -> None:
        self.llm = llm
        self.registry = registry
        self.ledger = ledger
        self.memory_file = Path(memory_file) if memory_file else None
        self.identity_file = Path(identity_file) if identity_file else None
        self.personality_file = Path(personality_file) if personality_file else None
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
        # 干活静默催报（秒）：用户在等、距上次发消息超这么久 → 请求尾巴催一句；0=关
        self.progress_ping_seconds = float(progress_ping_seconds)
        self.workspace = Path(workspace) if workspace else None   # 工作台（相对路径基准）
        # 思考档位（reasoning_effort）：每轮请求前现取 → 改配置即生效，可随时切换
        # 思考/非思考混用；默认 "medium"（none=不思考）
        self.effort_provider = effort_provider or (lambda: "medium")
        self.effort_setter = effort_setter     # /effort 落盘口（保注释写 config.yml）
        # 已知频道 id（= 适配器 name）：模型 channel 属性的白名单
        self.known_channels = tuple(known_channels or ())
        # 时间线桶名：非空时所有事件归同一条连续记录（session 语义退役，
        # 账本的分区列只当机制桶——多用户/群聊将来就是换分桶规则）
        self.timeline_bucket = timeline_bucket
        self.recorder = recorder                # R（Recorder）：紧急压缩钩子用（可空）
        self._states: Dict[str, SessionState] = {}
        self._anchors: Dict[str, TokenAnchor] = {}    # 每会话真值锚定（token 预估）
        # 每会话渲染用量（R 整理时读：上下文 token 数/预算/占比）
        self.context_stats: Dict[str, ContextStats] = {}
        self._tools_est: Optional[int] = None        # 工具 schema 估算（常量，算一次）
        self._bg_tasks: set = set()                  # 后台任务（R 退役前扫描等）防 GC

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def bucket_of(self, event: MessageEvent) -> str:
        """事件归哪条时间线：产品层一条连续记录（timeline_bucket）时全归它。"""
        return self.timeline_bucket or event.session_key()

    async def submit(self, event: MessageEvent,
                     *, quote_fetcher: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
                     responder: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
        """提交一条入站消息并立即开工（不经攒批；指令与直连调用走这）。

        拆两步：ingest（预处理+入账，忙时立即打断在飞模型）→ kick（放行开工）。
        """
        session_key = self.bucket_of(event)
        await self.ingest(event, quote_fetcher=quote_fetcher, responder=responder)
        self.kick(session_key)

    async def ingest(self, event: MessageEvent,
                     *, quote_fetcher: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
                     responder: Optional[Callable[..., Awaitable[Any]]] = None) -> str:
        """预处理 + 入账定稿（不开工）。指令顺带执行（照常入账、回系统消息）。

        返回正文（语音已转写）。忙时收到新输入**立即取消在飞的模型请求**——
        止血：不让基于旧理解的话发出去；工具不杀（跑完即停）。
        重跑由 kick 放行：攒批场景等窗口，直连场景即刻。
        quote_fetcher：通道的"按 id 取消息文本"兜底（飞书 message.get）。
        """
        session_key = self.bucket_of(event)
        raw_text = (event.text or "").strip()
        is_command = raw_text.startswith("/")

        st = self._states.setdefault(session_key, SessionState())
        if responder is not None:
            st.responder = responder
        if not is_command:
            # 置忙：从入库到 turn 收工的整段都算"任务执行中"（R 快整理不插队）
            st.busy = True
            if not st.user_waiting:      # 用户开始等（催报门槛；唤醒注入不算）
                st.user_waiting = True
                st.waiting_since = time.time()

        # 1) 预处理：语音转写 → 普通文本；图片上传 vision
        text, attachments = await self._preprocess(event)

        # 指令分流：不进模型——照常入账（用户气泡），执行后回 system 消息
        if is_command:
            await self._ingest_user(event, session_key, raw_text, attachments, quote_fetcher)
            await self._handle_command(event, session_key, raw_text, responder)
            return raw_text

        # 2) 入库定稿：引用解析（平台 id → 气泡）+ 净化/气泡编号/recall 固化
        await self._ingest_user(event, session_key, text, attachments, quote_fetcher)
        st.input_gen += 1    # 新材料落账（turn 循环据此判断有没有没答的输入）
        if st.model_task is not None and not st.model_task.done():
            st.model_task.cancel()   # 立即打断（redirect 的取消动作提前到此刻）
        return text or ""

    async def ingest_quiet(self, event: MessageEvent,
                           *, quote_fetcher=None) -> str:
        """群聊噪音入账：预处理 + 定稿落账，但**不打断在飞模型、不计待答**。

        群消息流是连续的，照搬 ingest 的 redirect 语义（取消在飞请求）会让
        chatagent 永远答不完一句；安静入账的行会在下次 kick 的重渲染里自然进上下文。"""
        session_key = self.bucket_of(event)
        text, attachments = await self._preprocess(event)
        await self._ingest_user(event, session_key, text, attachments, quote_fetcher)
        return text or ""

    def kick(self, session_key: str) -> None:
        """放行开工：本批已定（攒批窗口到点 / 直连即刻）。

        空闲 → 起新 turn；忙 → 不用额外动作——打断已在 ingest 做过，
        turn 循环的攒批屏障会在 kick 后放行，带着批内全部消息重渲染重跑
        （连补几句只打断模型一次、只重跑一次）。
        """
        st = self._states.setdefault(session_key, SessionState())
        st.kicked_input_gen = st.input_gen
        st.kick_event.set()
        st.kick_event = asyncio.Event()   # 换新：下一轮等待者听新事件
        if st.task is None or st.task.done():
            st.task = asyncio.get_event_loop().create_task(self._turn_loop(session_key))

    async def wait_idle(self, session_key: str) -> None:
        """等该会话的活跃 turn 结束（CLI 顺序输入用）。"""
        st = self._states.get(session_key)
        if st and st.task is not None and not st.task.done():
            try:
                await st.task
            except Exception:
                pass

    def inject_internal(self, session_key: str, text: str) -> None:
        """内部消息注入（指令回执等流程注记；role=system）。"""
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(text, sender="system", bubble_id=bid, ts=time.time())
        self.ledger.add_turn(session_key=session_key, type=TYPE_INTERNAL, text=final)

    def inject_wake(self, session_key: str, blocks_text: str,
                    responder: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
        """自我唤醒注入（<wake> 块；一条消息可并列多块 = 补漏清单）+ 计入待答。

        与 ingest 的关键区别：**不取消在飞模型**——唤醒不是用户插话，不打断
        正在跑的轮；只落账 + input_gen++。开工时机由调用方定（SettleGate.push_wake：
        会话在攒批窗口就随批一起 kick，否则即刻）。
        """
        st = self._states.setdefault(session_key, SessionState())
        if responder is not None:
            st.responder = responder
        self.ledger.add_turn(session_key=session_key, type=TYPE_WAKE,
                             text=blocks_text, timestamp=time.time())
        st.input_gen += 1

    async def _handle_command(self, event: MessageEvent, session_key: str,
                              text: str, responder) -> None:
        """指令执行 + 系统回执：回执是 internal 行 + **真气泡**（可被引用），
        发到指令来的频道，不触发模型回合。

        指令闸：chat 面的指令只认主人（/id 例外，它只回发送者自己的 id）；
        非主人发指令**静默忽略**——不回执不提示，别教人试。"""
        cmd_name = text.strip().split()[0].lstrip("/").lower() if text.strip() else ""
        if not event.is_owner and cmd_name != "id":
            logger.info("[command] 非主人指令静默忽略：%s（%s）", cmd_name,
                        event.sender.id)
            return

        async def _notify(text: str) -> None:
            """异步完成播报（如 /compact 压缩完）：普通系统消息，不落账。"""
            if responder is not None:
                try:
                    await responder(text, channel=event.channel)
                except Exception:
                    logger.exception("异步播报发送失败")

        receipt = commands.execute(text, commands.CommandContext(
            surface="chat",
            channel=event.channel,
            home=self.ledger.get_setting("home"),
            known_channels=self.known_channels,
            sender_id=event.sender.id,
            set_home=lambda ch: self.ledger.set_setting("home", ch),
            thinking=self.effort_provider() if callable(self.effort_provider) else "",
            set_thinking=self.effort_setter,
            model=getattr(self.llm, "model", ""),
            context_info=lambda: self.context_line(session_key),
            status_rows=lambda: self.status_rows(),
            compact=lambda: self.compact_now(session_key),
            notify=_notify))
        logger.info("[command] %s → %s", text, receipt)
        ts = time.time()
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(receipt, sender="system", bubble_id=bid, ts=ts,
                              channel=event.channel)
        seq = self.ledger.add_turn(session_key=session_key, type=TYPE_INTERNAL,
                                   text=final, timestamp=ts)
        self.ledger.add_bubble(
            session_key=session_key, bubble_id=bid, seq_id=seq, direction="out",
            platform=event.channel, chat_id=event.conversation.id,
            kind="text", content=receipt, sender="system", timestamp=ts)
        result = None
        if responder is not None:
            try:
                result = await responder(receipt, channel=event.channel)
            except Exception:
                logger.exception("系统回执发送失败")
        self._backfill_bubble(session_key, bid, result)

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
                self._spoke(st)
        finally:
            st.model_task = None
            st.tool_running = False
            st.busy = False
            st.user_waiting = False      # 这轮答完了，催报门槛落下
            st.waiting_since = None
            st.done_at = time.time()      # 任务收工：R 快整理从这一刻起算静默

    def states_of(self, session_key: str) -> SessionState:
        """取（必要时建）会话运行态——外部注入消息要 bump input_gen 时用。"""
        return self._states.setdefault(session_key, SessionState())

    def turn_state(self, session_key: str) -> tuple:
        """(是否正忙, 上次 turn 收工时刻)——R 快周期调度的判据。

        本进程没碰过该会话 → (False, None)：调度端退回"末条行静默"
        （重启后欠的整理还得有人管）。"""
        st = self._states.get(session_key)
        if st is None:
            return False, None
        return st.busy, st.done_at

    # ------------------------------------------------------------------
    # 状态（/status 卡片的数据源）
    # ------------------------------------------------------------------
    def context_line(self, session_key: str) -> str:
        """当前上下文用量一句话："12.3k/250.0k token（5%）…"。"""
        st = self.context_stats.get(session_key)
        budget = self.token_budget
        if st is None or not st.tokens:
            return "还没有渲染过（发条消息就有数了）"
        pct = st.tokens / budget if budget else 0.0
        line = f"{st.tokens / 1000:.1f}k/{budget / 1000:.1f}k token（{pct:.0%}）"
        if st.predicted:
            line += f"，锚定预测 {st.predicted / 1000:.1f}k"
        return line

    def status_rows(self, channel_lines: Optional[Sequence[str]] = None
                    ) -> List[Tuple[str, str]]:
        """/status 的动态行（label, value）；后端有什么报什么，缺的不硬凑。

        channel_lines：入口层给的每频道一行"名 状态"（没有就只报频道名）。"""
        rows: List[Tuple[str, str]] = []
        ch = [str(x) for x in (channel_lines or self.known_channels)]
        rows.append(("频道", "\n".join(ch) if ch else "（无）"))
        if self.memory_file is not None:
            try:
                n = self.memory_file.stat().st_size
                rows.append(("记忆", f"{self.memory_file.name} {n / 1000:.1f}kB"))
            except OSError:
                pass
        wakes = self.ledger.list_wakes(self.timeline_bucket or "main",
                                       status="pending")
        rows.append(("唤醒", f"{len(wakes)} 个待触发" if wakes else "无"))
        skills_dir = getattr(self, "skills_dir", None)
        if skills_dir is not None:
            n = len(list_names(skills_dir))
            rows.append(("skills", f"{n} 个" if n else "无"))
        mgr = getattr(self, "chatagents", None)
        if mgr is not None:
            n = sum(1 for g in self.ledger.group_log_chats() if mgr.is_active(g))
            rows.append(("群聊", f"{n} 个群已激活" if n else "没激活的群"))
        return rows

    # ------------------------------------------------------------------
    # 上下文压缩（撞限/强制两个时刻；机械推切点，核在 core/compaction.py）
    # ------------------------------------------------------------------
    async def compact(self, session_key: str) -> Optional[CompactResult]:
        """一次机械压缩：总结备忘 + 切点推进 + 图回收。撞限/强制共用。"""
        return await compaction.compact(
            self.ledger, self.llm, session_key,
            prompt=compaction.COMPACT_PROMPT, counter=self.token_counter,
            row_view=row_line, vision=self.vision, session=session_key)

    async def compact_now(self, session_key: str) -> str:
        """用户强制压缩（/compact）：压缩 + R 退役前扫描并发跑、都等完，
        合并简报（给人看）。"""
        cut0 = self.ledger.get_recorder_state(session_key)[1]   # 触发时刻切点快照
        r_task = (asyncio.get_event_loop().create_task(
                      self.recorder.run_retiring(session_key, cut0))
                  if self.recorder is not None else None)
        result = await self.compact(session_key)
        r_report = ""
        if r_task is not None:
            try:
                r_report = (await r_task) or ""
            except Exception:
                logger.exception("R 退役前扫描失败 session=%s", session_key)
        if result is None:
            return "无需压缩（活行不多，没什么可退役的）"
        line = (f"压缩完成：保留最近 {result.kept} 条 + 备忘，"
                f"释放约 {result.freed:,} token（回收图 {result.blobs_gone} 张）")
        if r_report:
            line += f"；记忆整理：{r_report}"
        return line

    async def _compact_midturn(self, session_key: str, st: SessionState) -> bool:
        """turn 中撞限的压缩：压缩 + R 退役前扫描并发；只等压缩（重渲染要用），
        R 后台跑完自动落记忆。返回有没有真的压（False = 活行不足，走渲染端兜底）。"""
        cut0 = self.ledger.get_recorder_state(session_key)[1]   # 触发时刻切点快照
        if st.responder is not None:
            await st.responder("压缩上下文中")
            self._spoke(st)
        if self.recorder is not None:
            self._spawn(self.recorder.run_retiring(session_key, cut0),
                        "R 退役前扫描")
        try:
            result = await self.compact(session_key)
        except Exception:
            logger.exception("压缩失败 session=%s", session_key)
            result = None
        if st.responder is not None:
            await st.responder("压缩完成")
            self._spoke(st)
        return result is not None

    def _spawn(self, coro, what: str) -> None:
        """后台任务挂账：防 GC，完事记日志（异常不静默）。"""
        task = asyncio.get_event_loop().create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: self._bg_done(t, what))

    def _bg_done(self, t: asyncio.Task, what: str) -> None:
        self._bg_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("%s 失败：%r", what, exc)
        else:
            logger.info("%s 完成：%s", what, t.result())

    # ------------------------------------------------------------------
    # 干活静默催报（用户在等时催它报进度；提示只存在于本次请求）
    # ------------------------------------------------------------------
    def _spoke(self, st: SessionState) -> None:
        """记一笔"真给用户发了消息"（催报计时基准）。"""
        st.last_spoke_at = time.time()

    def _progress_ping(self, st: SessionState) -> Optional[dict]:
        """催报消息（挂请求尾巴，不落账本）；不触发返回 None。

        触发 = 用户在等（user_waiting；闹钟/哨兵的自主干活不算）且距上次真发消息
        超过 progress_ping_seconds（还没说过话就从用户开始等起算）。"""
        if self.progress_ping_seconds <= 0 or not st.user_waiting:
            return None
        base = st.last_spoke_at or st.waiting_since
        if base is None or time.time() - base <= self.progress_ping_seconds:
            return None
        return {"role": "user", "content": _PROGRESS_PING}

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
        compressed = False          # 一次 turn 最多压一次（压完重渲染再看）
        while rounds < self.max_tool_rounds:
            # 攒批屏障：新材料已落账但 kick 未到（用户可能还有下文）→ 原地待命。
            # 打断（取消在飞模型）在 ingest 就做了；这里等的是"带着新消息继续"的时机。
            if st.input_gen > st.kicked_input_gen:
                logger.info("[turn] 新消息在攒批，等窗口到点再继续")
                await st.kick_event.wait()
            # 事实层现渲染（工具流水当轮已落库，渲染即含，不再另拼机器件——
            # 曾经的 turn_tool_msgs 暂存链会造成同一轮流水在请求里出现两次）。
            # 裁剪预算扣掉：工具 schema + 锚定残差（真值锚定）。
            gen_at_render = st.input_gen
            overhead, extra_est = self._request_overhead(session_key, tools)
            messages, timeline_cost, trimmed = self._render(session_key, overhead=overhead)
            est_total = timeline_cost + extra_est

            # 压缩（机械，罕见）：撞限 = 装配被迫砍最老 / 超 90% 预算 → 挂起 turn。
            # 压缩总结推切点 + R 退役前最后扫描并发跑（R 后台跑完自动落记忆）；
            # "压缩上下文中/压缩完成"是系统播报，直发不过 M 的模型。
            if not compressed and (trimmed or est_total
                                   > self.token_budget * compaction.PRESSURE_PCT):
                compressed = True
                logger.info("[turn] 上下文撞限，压缩 session=%s", session_key)
                if await self._compact_midturn(session_key, st):
                    continue        # 切点推进 → 重渲染继续，不扣轮数
                logger.warning("[turn] 没什么可压缩，走渲染端兜底截断")

            # 干活静默催报（仅本次请求，不落账本）：用户在等且久没吭声 →
            # 请求尾巴挂一条 user 提示，催它用 <message> 报一句进度。
            ping = self._progress_ping(st)
            if ping is not None:
                messages = messages + [ping]
                est_total += count_chat_payload(self.token_counter, [ping])
                logger.info("[turn] 静默超时，催报进度 session=%s", session_key)
            predicted = self._anchors[session_key].predict(est_total)

            # 请求发出前先落 modellog（400 翻车现场也留证据；未开启时空转）
            effort = self.effort_provider()
            modellog.dump("m", session_key, {"round": rounds, "messages": messages})
            st.model_task = asyncio.get_event_loop().create_task(
                self.llm.chat(messages, tools=tools, reasoning_effort=effort,
                              session=session_key))
            try:
                result = await st.model_task
            except asyncio.CancelledError:
                # 被新消息打断（ingest 所为）：重渲染要等攒批屏障放行（kick 才继续）
                logger.info("[turn] 模型请求被新消息打断，等攒批后重渲染（不扣轮数）")
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
        messages, timeline_cost, _trimmed = self._render(session_key, overhead=overhead)
        messages = messages + [nudge]
        est_total = timeline_cost + extra_est \
            + count_chat_payload(self.token_counter, [nudge])
        predicted = self._anchors[session_key].predict(est_total)
        effort = self.effort_provider()
        modellog.dump("m", session_key, {
            "round": rounds, "phase": "max_tool_rounds", "messages": messages})
        result = await self.llm.chat(messages, reasoning_effort=effort,
                                     session=session_key)
        self._anchor_and_log(session_key, result, est_total, predicted)
        await self._emit_round(session_key, st, result, None, bubbles_sent)
        return gen_at_render

    # ------------------------------------------------------------------
    # 入库定稿 / 气泡投递
    # ------------------------------------------------------------------
    def _default_channel(self, session_key: str) -> str:
        """出口路由兜底频道：上一个用户说话的频道；查不到 → home。"""
        hit = self.ledger.last_inbound(session_key)
        if hit and hit.platform:
            return hit.platform
        return self._home_channel(session_key)

    def _home_channel(self, session_key: str) -> str:
        """home 频道：/home 设置的优先；没设置默认第一条用户消息的频道。"""
        home = self.ledger.get_setting("home")
        if home:
            return home
        first = self.ledger.first_inbound(session_key)
        return first.platform if first else ""

    def home_channel(self) -> str:
        """home 频道（主动消息的默认出口）——主动出站/调度器用。"""
        return self._home_channel(self.timeline_bucket or "main")

    def _outbound_chat(self, session_key: str, channel: str) -> str:
        """出站气泡的 chat 归属：该频道最近一次入站消息的 chat。"""
        hit = self.ledger.last_inbound(session_key, channel=channel)
        return hit.chat_id if hit else ""

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
            # 引用只在同频道内成立（平台消息 id 是平台局部的）
            hit = self.ledger.find_bubble(session_key, ref.message_id,
                                          channel=event.channel)
            if hit is not None:
                recall = recall_tag(hit.bubble_id, hit.sender, hit.timestamp,
                                    hit.content, channel=hit.platform,
                                    owner=getattr(hit, "is_owner", False))
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
                                        ref.timestamp or event.timestamp, ref.text,
                                        channel=event.channel)
        body = (text or "") + attachment_notes(attachments)
        sender = event.sender.name or event.sender.id
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(body, sender=sender, bubble_id=bid,
                              ts=event.timestamp, recall=recall,
                              channel=event.channel, owner=event.is_owner)
        seq = self.ledger.add_turn(session_key=session_key, type=TYPE_USER,
                                   text=final, attachments=atts,
                                   timestamp=event.timestamp)
        kind = ("text" if text.strip()
                else "image" if any(a.kind == "image" for a in atts) else "file")
        self.ledger.add_bubble(
            session_key=session_key, bubble_id=bid, seq_id=seq, direction="in",
            platform=event.channel, chat_id=event.conversation.id, message_id=event.id,
            kind=kind, content=text or "", sender=sender, timestamp=event.timestamp,
            meta={"owner": True} if event.is_owner else None)

    async def _emit_round(self, session_key: str, st: SessionState, result,
                          calls: Optional[List[dict]], already: int) -> tuple:
        """一轮模型输出：定稿入库（assistant 行 + 气泡行）→ 逐个气泡投递。

        返回 (assistant 行 seq|None, 实发条数)。思考草稿随 assistant 行入库
        （回传锚点，一份/轮）；无正文、无工具、**连思考都没有**的轮才不落行——
        有思考就落行（text 可空、气泡 0 条）：决定沉默也是决定，得留痕。"""
        ts = time.time()
        final, bubbles = finalize_assistant(
            result.content, ts=ts,
            alloc=self._alloc_bubble(session_key),
            lookup=self._bubble_lookup(session_key),
            default_channel=self._default_channel(session_key),
            known_channels=self.known_channels)
        if not final.strip() and not calls and not result.reasoning:
            return None, 0
        seq = self.ledger.add_turn(
            session_key=session_key, type=TYPE_ASSISTANT, text=final,
            reasoning=result.reasoning or "", calls=calls, timestamp=ts)
        for b in bubbles:
            self.ledger.add_bubble(
                session_key=session_key, bubble_id=b.bubble_id, seq_id=seq,
                direction="out", platform=b.channel,
                chat_id=self._outbound_chat(session_key, b.channel),
                kind=b.kind, content=b.content, sender="neoagent", timestamp=ts)
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
            quote_to = self._bubble_platform_id(session_key, b.reply_to, b.channel)
            if b.kind == "text":
                result = None
                if st.responder is not None:
                    try:
                        result = await st.responder(b.content, quote_to=quote_to,
                                                    channel=b.channel)
                    except Exception:
                        logger.exception("回复发送失败")
                    self._spoke(st)          # 说上话了：催报计时重置
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
                            bubble_id: Optional[int],
                            channel: str = "") -> Optional[str]:
        """replyTo 气泡 → 平台消息 id（通道真引用）；拿不到就降级纯文本。

        引用不跨平台：目标气泡不在投递频道上时同样降级（平台 id 是平台局部的）。"""
        if bubble_id is None:
            return None
        hit = self.ledger.get_bubble(session_key, int(bubble_id))
        if hit is None or not hit.message_id:
            logger.warning("replyTo=msg#%s 没有平台消息 id，降级纯文本", bubble_id)
            return None
        if channel and hit.platform and hit.platform != channel:
            logger.info("replyTo=msg#%s 在 %s 上、发往 %s，跨频道引用不成立，降级纯文本",
                        bubble_id, hit.platform, channel)
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
            await self._say(session_key, st, f"（{display} 发不出去：{exc}）",
                            channel=b.channel)
            return
        result = None
        if st.responder is not None:
            try:
                result = await st.responder("", quote_to=quote_to, media=str(p),
                                            channel=b.channel)
            except Exception:
                logger.exception("媒体发送失败")
            self._spoke(st)
        self._backfill_bubble(session_key, b.bubble_id, result)
        if not (result is not None and getattr(result, "ok", False)):
            err = getattr(result, "error", None) or "通道不支持"
            await self._say(session_key, st, f"（{display} 发不出去：{err}）",
                            channel=b.channel)

    async def _say(self, session_key: str, st: SessionState, text: str,
                   channel: str = "") -> int:
        """发一条系统生成的文本消息并定稿入库（降级说明用），返回气泡编号。"""
        ts = time.time()
        bid = self.ledger.alloc_bubbles(session_key, 1)[0]
        final = finalize_user(text, sender="neoagent", bubble_id=bid, ts=ts,
                              channel=channel)
        seq = self.ledger.add_turn(session_key=session_key, type=TYPE_ASSISTANT,
                                   text=final, timestamp=ts)
        self.ledger.add_bubble(
            session_key=session_key, bubble_id=bid, seq_id=seq,
            direction="out", platform=channel,
            chat_id=self._outbound_chat(session_key, channel),
            kind="text", content=text, sender="neoagent", timestamp=ts)
        result = None
        if st.responder is not None:
            try:
                result = await st.responder(text, channel=channel)
            except Exception:
                logger.exception("回复发送失败")
            self._spoke(st)
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
        """【记忆】段：记忆文件全文（R 维护、M 只读；子文件按需经 read_file 的
        memory:// 口读，空文件整段省略）。chatagent 无记忆文件（便签自己用工具读）。"""
        if self.memory_file is None:
            return ""
        try:
            return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""
        except OSError as exc:
            logger.warning("记忆文件读取失败：%s", exc)
            return ""

    def _identity_text(self) -> str:
        """【它是谁】段：包内 messenger.md（协议/工作方式）+ 用户 personality.md（人格）。"""
        parts: List[str] = []
        for label, path in (("内置提示词", self.identity_file), ("人格", self.personality_file)):
            if path is None:
                continue
            try:
                parts.append(path.read_text(encoding="utf-8").strip())
            except OSError as exc:
                logger.warning("%s读取失败 %s：%s", label, path, exc)
        return "\n\n".join(p for p in parts if p)

    def _render(self, session_key: str, overhead: int = 0):
        """装配一次请求 → (messages, 时间线估算 token, 是否被迫裁剪)。"""
        identity = self._identity_text()
        if self.workspace is not None:
            identity += f"\n\n（你的工作目录：{self.workspace}，产出物放这里；文件工具的相对路径以它为基准）"
        _, cut_seq = self.ledger.get_recorder_state(session_key)   # 上下文切点（压缩机械推进）
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
        self.context_stats[session_key] = ContextStats(
            tokens=est_total, predicted=predicted or 0,
            budget=self.token_budget, ts=time.time())

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

        output = clip_tool_result(output)   # 超长截尾（M/R 同一上限）

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
