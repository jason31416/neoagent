"""chatagent：飞书群里的"群友"子 agent（每群一个，独立上下文）。

角色：看全部群消息、被 @ 才说话、聊天专用（工具只有 memory + tell_neoagent）。
- **消息路由**：所有群消息落群记录（group_messages）；活跃群的 chatagent
  噪音安静入账（不打断在飞模型），被 @ / 上级来话才触发 turn；
- **两 agent 互发即回**：tell_neoagent（chatagent→主 agent）与 tell_chatagent
  （主 agent→chatagent）都是落账 + 触发一轮 + 立即返回，**不等对方回话**——
  回话由对方自己决定怎么给（群里开口 / 再 tell 回来），各是独立的一轮；
- **压缩**：上下文压力超阈值 → 静默总结调用 → `<compact>` 行落为最新消息、
  最近 10 条原样保留、切点推进、切点以下的图回收（压缩即销毁）；
- **状态**：激活与否进 kv（重启不丢）；休眠 = 实例销毁、零 API、消息只落记录。

身份口径：chatagent 是 neoagent 活在群里的子 agent（提示词开头即此），
主 agent 是私聊里的同一个人——一个产品，一个名字。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ..config import PACKAGE_DIR
from . import compaction
from .compaction import PRESSURE_PCT
from .events import MessageEvent, SendResult
from .frames import finalize_user
from .ledger import TYPE_USER, MessageLedger
from .llm import LLMClient

logger = logging.getLogger(__name__)

# 数值旋钮（代码常数，不过度配置）
CHATAGENT_TOKEN_BUDGET = 200_000     # 群消息量大，独立预算（与主 agent 的分开；群级可配）
CHATAGENT_MAX_TOOL_ROUNDS = 8        # 聊天工具，链短
TOKEN_BUDGET_RANGE = (20_000, 300_000)   # 群配置里 token_budget 的合法范围
TOOL_ROUNDS_RANGE = (1, 50)              # 同上：max_tool_rounds

# skill 白名单引用：skill 名 或 skill 名.工具名
_SKILL_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_]+)?$")


@dataclass
class GroupConfig:
    """每群 chatagent 的独立配置（groups/<chat_id>/config.yml，全键可选）。

    缺文件/缺键 = 现状默认；解析问题进 problems（回退默认照跑，/activate 回执里提）。"""
    persona: str = "persona.md"                        # 人格文件（相对群目录）
    skills: List[str] = field(default_factory=list)    # skill 白名单（空 = 一个不给）
    token_budget: int = CHATAGENT_TOKEN_BUDGET
    max_tool_rounds: int = CHATAGENT_MAX_TOOL_ROUNDS
    problems: List[str] = field(default_factory=list)


def load_group_config(group_dir: Path) -> GroupConfig:
    """读 groups/<chat_id>/config.yml → GroupConfig；坏键回退默认，不带坏配置跑。"""
    cfg = GroupConfig()
    path = group_dir / "config.yml"
    if not path.is_file():
        return cfg
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (YAMLError, OSError) as e:
        cfg.problems.append(f"config.yml 读不了：{e}（整份用默认）")
        return cfg
    if data is None:
        return cfg
    if not isinstance(data, dict):
        cfg.problems.append("config.yml 顶层必须是键值对（整份用默认）")
        return cfg

    unknown = set(data) - {"persona", "skills", "token_budget", "max_tool_rounds"}
    if unknown:
        cfg.problems.append(f"未知配置项：{sorted(unknown)}（已忽略）")

    persona = str(data.get("persona", "") or "").strip()
    if persona:
        if "/" in persona or persona.startswith("."):
            cfg.problems.append(f"persona 只认群目录里的文件名：{persona!r}（用默认）")
        else:
            cfg.persona = persona

    skills = data.get("skills")
    if skills is not None:
        if not isinstance(skills, list):
            cfg.problems.append("skills 必须是列表（用默认：一个不放行）")
        else:
            out = []
            for s in skills:
                ref = str(s).strip()
                if _SKILL_REF_RE.fullmatch(ref):
                    out.append(ref)
                else:
                    cfg.problems.append(f"skill 引用不合法：{s!r}（已忽略）")
            cfg.skills = out

    for key, (lo, hi) in (("token_budget", TOKEN_BUDGET_RANGE),
                          ("max_tool_rounds", TOOL_ROUNDS_RANGE)):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            v = int(raw)
        except (TypeError, ValueError):
            cfg.problems.append(f"{key} 不是整数：{raw!r}（用默认）")
            continue
        if not lo <= v <= hi:
            cfg.problems.append(f"{key}={v} 超出范围 {lo}~{hi}（已截到范围内）")
            v = max(lo, min(hi, v))
        setattr(cfg, key, v)
    return cfg


class ChatAgentManager:
    """所有群的 chatagent 生命周期 + 消息路由 + 两 agent 互发消息。"""

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
        self._cfg_cache: Dict[str, tuple] = {}   # chat_id → (mtime 签名, GroupConfig)
        self._sender: Optional[Callable] = None     # 发群消息出口（run.py 注入）
        self._main_responder: Optional[Callable] = None  # 主 agent 回话出口（run.py 注入）
        self._quote_fetcher: Optional[Callable] = None

    def set_sender(self, sender: Callable) -> None:
        """发群消息出口：await sender(chat_id, text, quote_to=..., media=...)。"""
        self._sender = sender

    def set_quote_fetcher(self, fn: Optional[Callable]) -> None:
        self._quote_fetcher = fn

    def set_main_responder(self, responder: Callable) -> None:
        """主 agent 的回话出口：tell_neoagent 注入后，它的轮要说话说得出去。"""
        self._main_responder = responder

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

    # ------------------------------------------------------------------
    # 每群配置（groups/<chat_id>/config.yml；mtime 缓存，改了自动重载）
    # ------------------------------------------------------------------
    def group_config(self, chat_id: str) -> GroupConfig:
        """本群配置；config.yml / persona.md 的 mtime 变了就重读。"""
        sig = self._cfg_sig(chat_id)
        hit = self._cfg_cache.get(chat_id)
        if hit is not None and hit[0] == sig:
            return hit[1]
        cfg = load_group_config(self.home / "groups" / chat_id)
        self._cfg_cache[chat_id] = (sig, cfg)
        return cfg

    def _cfg_sig(self, chat_id: str) -> tuple:
        gdir = self.home / "groups" / chat_id
        hit = self._cfg_cache.get(chat_id)
        names = ("config.yml", hit[1].persona if hit else "persona.md")
        out = []
        for n in names:
            try:
                out.append((gdir / n).stat().st_mtime_ns)
            except OSError:
                out.append(0)              # 没这个文件 = 全默认
        return tuple(out)

    def _ensure_loop(self, chat_id: str):
        cfg = self.group_config(chat_id)   # 同一配置 = 同一对象（热更比对锚）
        loop = self._loops.get(chat_id)
        if loop is not None:
            if getattr(loop, "_group_cfg", None) is cfg:
                return loop
            busy, _ = loop.turn_state(loop.session_key)
            if busy:
                return loop    # 正忙先用旧配置跑完这轮，下条消息再换
            self._loops.pop(chat_id, None)
            logger.info("[chatagent] %s 配置有变，重建实例（上下文在账本里，不丢）",
                        chat_id)
        from ..tools.chatagent_tools import register_chatagent_tools
        from ..tools.registry import ToolRegistry
        from ..tools.skill_tools import register_skill_tools

        registry = ToolRegistry(part="chatagent")
        notes = self.home / "groups" / chat_id / "memory.md"
        register_chatagent_tools(registry, memory_file=notes,
                                 manager=self, chat_id=chat_id)
        if cfg.skills:                     # 白名单空 = 不给 skill 工具（现状默认）
            register_skill_tools(registry, self.home / "skills",
                                 allow=cfg.skills, with_check=False)
        loop = AgentLoopFactory(self, registry, chat_id, cfg)
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
        # （占位词典拎出来写：f-string 表达式跨行是 3.12+ 语法，下限是 3.10）
        atts = event.attachments or []
        kind = "text" if (event.text or "").strip() else (
            atts[0].kind if atts else "text")
        word = {"image": "图", "file": "文件",
                "audio": "语音", "video": "视频"}.get(kind, "附件")
        text = (event.text or "").strip() or f"[{word}]"
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
                cfg = self.group_config(chat_id)
                receipt = ("已激活，@我聊天"
                           f"（本群配置：{self.home / 'groups' / chat_id / 'config.yml'}）")
                if cfg.problems:
                    receipt += "\n配置有问题：" + "；".join(cfg.problems)
            else:
                self.deactivate(chat_id)
                receipt = "已休眠（记忆和记录都留着，/activate 再来）"
        else:
            receipt = "群里只认 /activate、/deactivate、/id（其他指令私聊说）"

        await self._say_group(chat_id, receipt, sender="neoagent")

    # ------------------------------------------------------------------
    # 两 agent 互发消息（落账 + 触发一轮 + 立即返回，不等回话）
    # ------------------------------------------------------------------
    def chatagent_says(self, chat_id: str, message: str,
                       context: str = "") -> None:
        """tell_neoagent 的实现：转述进主桶并触发主 agent 一轮，发完即回。

        不等主 agent 回话——它有话要说自己会说（回主人 / 再 tell_chatagent），
        什么时候说、说不说都由它定。"""
        body = message
        if context.strip():
            body += f"\n<context>\n{context.strip()}\n</context>"
        self._inject_main(chat_id, body)
        self.main.kick(self.main.timeline_bucket or "main")

    def main_says(self, chat_id: str, message: str) -> None:
        """tell_chatagent 的实现：上级来话进它的上下文并触发它一轮，发完即回。

        不等它回话——它要开口就在群里 <message>，要答你就 tell_neoagent。"""
        if not self.is_active(chat_id):
            return                      # 调用方按 not_found 口径拦过，这里只兜底
        loop = self._ensure_loop(chat_id)
        key = loop.session_key
        self._inject_chatagent(loop, chat_id, message)
        loop.kick(key)
        asyncio.get_event_loop().create_task(self._post_turn(chat_id))

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
        st = self.main.states_of(key)
        if st.responder is None and self._main_responder is not None:
            st.responder = self._main_responder   # 重启后还没对话也发得出回话
        st.input_gen += 1

    def _inject_chatagent(self, loop, chat_id: str, body: str) -> None:
        """上级来话进 chatagent 的上下文（sender=neoagent），顺手绑好发声出口。"""
        key = loop.session_key
        st = loop.states_of(key)
        st.responder = self._responder(chat_id)   # 它 <message> 说的照常发回群里
        ts = time.time()
        bid = self.ledger.alloc_bubbles(key, 1)[0]
        final = finalize_user(body, sender="neoagent", bubble_id=bid, ts=ts)
        seq = self.ledger.add_turn(session_key=key, type=TYPE_USER, text=final,
                                   timestamp=ts)
        self.ledger.add_bubble(
            session_key=key, bubble_id=bid, seq_id=seq, direction="in",
            platform="", chat_id=chat_id, kind="text", content=body,
            sender="neoagent", timestamp=ts)
        st.input_gen += 1

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
        """静默压缩：走公共压缩件（总结 + <compact> 备忘行 + 切点推进 + 图回收）。"""
        loop = self._loops.get(chat_id)
        if loop is None:
            return
        key = loop.session_key
        try:
            result = await compaction.compact(
                self.ledger, self.llm, key, prompt=compaction.COMPACT_PROMPT,
                counter=self.token_counter, vision=self.vision, session=key)
        except Exception as exc:
            logger.warning("[chatagent] 总结调用失败，跳过压缩 %s：%s", chat_id, exc)
            return
        if result is None:
            return
        logger.info("[chatagent] 压缩 %s：留 %d 条 + 备忘，回收图 %d",
                    chat_id, result.kept, result.blobs_gone)


def _strip_mentions(text: str) -> str:
    """去掉行首的 @xxx token（指令常写成「@neoagent /activate」）。"""
    out = (text or "").strip()
    while out.startswith("@"):
        sp = out.find(" ")
        out = out[sp + 1:].strip() if sp >= 0 else ""
    return out


def AgentLoopFactory(manager: ChatAgentManager, registry, chat_id: str,
                     cfg: Optional[GroupConfig] = None):
    """按群造一个 chatagent 的 AgentLoop（懒实例化；休眠即销毁）。
    cfg：本群配置（人格/预算/工具轮数）；缺省用默认。"""
    from .agent_loop import AgentLoop

    cfg = cfg or GroupConfig()
    gdir = manager.home / "groups" / chat_id
    persona = gdir / cfg.persona
    loop = AgentLoop(
        manager.llm, registry, manager.ledger, None,
        identity_file=PACKAGE_DIR / "prompts" / "chatagent.md",
        personality_file=persona if persona.is_file() else None,
        vision=manager.vision, transcriber=manager.transcriber,
        token_counter=manager.token_counter,
        max_tool_rounds=cfg.max_tool_rounds,
        token_budget=cfg.token_budget,
        media_roots=[],                     # 聊天工具：不发媒体
        known_channels=manager.known_channels,
        timeline_bucket=f"group:{chat_id}")
    loop.session_key = f"group:{chat_id}"   # 本模块内的便捷名（会话 = 群桶）
    loop._group_cfg = cfg                   # 配置热更的比对锚（对象同一性）
    return loop
