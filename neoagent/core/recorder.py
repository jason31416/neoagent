"""R（Recorder）——记忆整理模块。

职责（设计-R记忆海马体.md）：
- 记忆的唯一写者：维护 data/memory/（memory.md + 子文件 + old/ 归档）；
- 双周期：快周期（查漏补缺/修正）、慢周期（长期归纳/归档清理/合并重排），
  差异只在提示词一节，工具与预算完全一致。

上下文压缩不归 R 管：切点由 core/compaction.py 机械推进（撞限/强制才动）。
R 与压缩并发时各用各的口径——压缩那边随便推切点，R 这边整跑钉着**开跑时刻**的
切点快照（run_retiring，由调用方在触发时刻取好传入），把要退役的整段该记的记掉
（"先落记忆再退役"）。

上下文 = system + 一条 user（`#` 元数据注释 + <file> 记忆文件全量 + 流水）；
流水与 M 同消息协议，工具调用与结果一律略去，唯 record_memory 委托内联 <record>。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

from ..config import PROJECT_ROOT
from ..tools.memory_tools import build_memory_registry, record_payload
from ..tools.registry import ToolOutput, clip_tool_result
from . import modellog
from .ledger import TYPE_ASSISTANT, TYPE_TOOL_RESULT, MessageLedger
from .llm import LLMClient
from .render import LIVE_ROWS_LIMIT, ContextStats, rows_cost
from ..providers.tokens import HeuristicTokenCounter, TokenCounter

logger = logging.getLogger("neoagent.recorder")

RECORD_NAME = "record_memory"      # M 的提案工具名（流水中唯一保留的工具痕迹）

def _load_system() -> str:
    """R 的系统提示词：包内 prompts/recorder.md（随代码仓库走，不开放用户配置）。
    周期差异片段见 _CYCLE_NOTES，运行时拼接。"""
    return (PROJECT_ROOT / "neoagent" / "prompts" / "recorder.md").read_text(
        encoding="utf-8").strip()

_CYCLE_NOTES = {
    "fast": """## 本次是快周期整理
侧重：修正记忆；
一般以增加为主、删除为辅。
快周期的常态是什么也不做，没有值得记或者只是“或许值得记”的就不要硬记。""",
    "slow": """## 本次是慢周期整理
侧重：更大幅度的整理——用户习惯、neoagent 的经验这类需要长期推演的归纳；
对过时或暂时用不到的记忆做归档/清理；对记忆内容做合并与重排。""",
}

_RETIRE_SCAN_NOTE = """## 本次是压缩同跑的退役前扫描
上下文马上要压缩退役（切点会推进），这是退役前对整段的最后一次记忆扫描：
把该记的都记掉，宁多勿漏。上下文丢弃不归你管，不用判断切点。"""

_TIME_FMT = "%Y-%m-%d %H:%M"


def row_line(r) -> str:
    """一行 → 流水视图的一条（主表定稿原样，和 M 看到的完全一致）。
    工具调用与结果剥除，唯 record_memory 委托随轮内联。"""
    if r.type == TYPE_TOOL_RESULT:
        return ""
    lines = []
    if r.text:
        lines.append(r.text)          # <message>/<recall>/<compact> 等标签都已固化在文里
    if r.type == TYPE_ASSISTANT:      # record_memory 委托随轮内联（工具行不进流水）
        ts = time.strftime(_TIME_FMT, time.localtime(r.timestamp))
        for c in r.calls:
            if getattr(c, "name", "") == RECORD_NAME:
                payload = record_payload({"arguments": getattr(c, "arguments", "")})
                lines.append(f'<record time="{ts}">{_esc(payload)}</record>')
    return "\n".join(lines)


class Recorder:
    """一次整理跑 = 调 run()。多会话：游标按 session_key 各记，记忆文件全局共享。"""

    def __init__(self, llm: LLMClient, ledger: MessageLedger, memory_dir: Path, *,
                 token_counter: Optional[TokenCounter] = None,
                 token_budget: int = 250_000,
                 max_tool_rounds: int = 90,
                 effort_provider: Optional[Callable[[], str]] = None,
                 slow_lookback_days: int = 7,
                 stats_provider: Optional[Callable[[str], Optional[ContextStats]]] = None) -> None:
        self.llm = llm
        self.ledger = ledger
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.token_counter = token_counter or HeuristicTokenCounter()
        self.token_budget = int(token_budget)      # 与 M 共用同一配置值
        self.max_tool_rounds = int(max_tool_rounds)
        self.effort_provider = effort_provider or (lambda: "medium")
        self.slow_lookback_days = int(slow_lookback_days)
        self.stats_provider = stats_provider        # 渲染用量（agent_loop 落；缺则现算兜底）
        self._system = _load_system()          # 提示词随包（不开放用户配置）
        self._lock = asyncio.Lock()            # 整理跑互斥（停顿调度/压缩同跑不并发）

    # ------------------------------------------------------------------
    # 调度器接口
    # ------------------------------------------------------------------
    def pending(self, session_key: str) -> bool:
        """有没有待整理的新增（停顿判定用）。"""
        r_seen, _ = self.ledger.get_recorder_state(session_key)
        return bool(self.ledger.since(session_key, r_seen, limit=1))

    # ------------------------------------------------------------------
    # 一次整理跑
    # ------------------------------------------------------------------
    async def run(self, session_key: str, cycle: str = "fast", *,
                  cut_snapshot: Optional[int] = None,
                  note: str = "") -> str:
        """跑一次整理，返回工作简报（只进日志/压缩简报）。

        cut_snapshot：切点快照（压缩同跑用）——整跑用它取"要退役的整段"，
        不回读账本里的活切点（压缩那边会并发推进，回读就把段读腰斩了）。
        """
        async with self._lock:
            return await self._run_locked(session_key, cycle,
                                          cut_snapshot=cut_snapshot, note=note)

    async def run_retiring(self, session_key: str, cut0: int) -> str:
        """压缩同跑：退役前对 [cut0, 现在] 整段做最后一次记忆扫描。

        cut0 必须是调用方在**触发时刻**取的快照——那时的切点才是退役段的起点；
        即使排队轮到本跑时切点已被压缩推走，整跑也钉着 cut0（记忆不漏）。"""
        return await self.run(session_key, "fast", cut_snapshot=cut0,
                              note=_RETIRE_SCAN_NOTE)

    async def _run_locked(self, session_key: str, cycle: str,
                          cut_snapshot: Optional[int] = None,
                          note: str = "") -> str:
        rows = self._window(session_key, cycle, cut_snapshot)
        if not rows and cycle == "fast":
            return "（无待整理流水）"

        registry = build_memory_registry(self.memory_dir)
        r_seen, cut = self.ledger.get_recorder_state(session_key)
        cut0 = int(cut_snapshot) if cut_snapshot is not None else cut   # 钉住：整跑用它
        notes = _CYCLE_NOTES.get(cycle, "")
        if note:
            notes = notes + "\n" + note if notes else note
        msgs = [
            {"role": "system", "content": self._system + ("\n" + notes if notes else "")},
            {"role": "user", "content": self._context(session_key, cycle, rows,
                                                     cut=cut0)},
        ]

        report = ""
        last_usage: Optional[dict] = None
        for rnd in range(self.max_tool_rounds):
            # 请求发出前落 modellog：R 的工具流水不进账本，它的请求现场只在这里有
            effort = self.effort_provider()
            modellog.dump("r", session_key, {
                "cycle": cycle, "round": rnd, "messages": msgs})
            result = await self.llm.chat(msgs, tools=registry.schemas(),
                                         reasoning_effort=effort,
                                         session=session_key)
            report = result.content or report
            last_usage = result.usage or last_usage
            if not result.tool_calls:
                break
            # reasoning_content 必带（真草稿或 " " 垫片）：DeepSeek 思考模式回放
            # 带 tool_calls 的消息强制要求该字段，缺了 400——不能指望"id 豁免"。
            msgs.append({
                "role": "assistant", "content": result.content,
                "reasoning_content": result.reasoning or " ",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in result.tool_calls
                ],
            })
            for tc in result.tool_calls:
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except (TypeError, ValueError):
                    args = {}
                out = await registry.call(tc.name, args)
                text = out.text if isinstance(out, ToolOutput) else str(out)
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": clip_tool_result(text)})
        else:
            logger.warning("[recorder] 整理达到工具轮上限 %d，提前收束 session=%s",
                           self.max_tool_rounds, session_key)

        # 推进增量游标（=本次窗口的末行，只进不退）；委托随之算已消化。
        # 记忆不漏的底：常规快周期窗口从 r_seen 起，压缩退役的段也会被 run_retiring
        # 扫到——就算某次被跳过/失败，下次常规整理仍会补上。
        if rows:
            self.ledger.set_recorder_state(
                session_key, r_seen_seq=max(r_seen, rows[-1].seq_id))
        logger.info("[recorder] %s整理完成 session=%s 流水 %s～%s%s",
                    "快" if cycle == "fast" else "慢", session_key,
                    f"msg#{rows[0].seq_id}" if rows else "（空）",
                    f"msg#{rows[-1].seq_id}" if rows else "（空）",
                    f" prompt={last_usage.get('prompt_tokens')}" if last_usage else "")
        return report or "（静默收工）"

    # ------------------------------------------------------------------
    # 窗口与上下文构造
    # ------------------------------------------------------------------
    def _window(self, session_key: str, cycle: str,
                cut_snapshot: Optional[int] = None) -> list:
        """取本次整理的流水窗口。

        快周期（常规）= 未消化段（r_seen 之后）——切点很少动，从切点读起会每次都
        重读大段已消化的行；
        快周期（压缩同跑，cut_snapshot）= 切点快照～现在（要退役的整段，最后扫描）；
        慢周期 = 回看起点～现在。
        预算封顶：从最旧端取装得下的前缀——不丢行，没装下的下轮续上。"""
        if cycle == "fast":
            if cut_snapshot is not None:
                start = max(int(cut_snapshot) - 1, 0)     # 含切点行（seq >= cut0）
            else:
                start, _ = self.ledger.get_recorder_state(session_key)   # seq > r_seen
            rows = self.ledger.since(session_key, start, limit=LIVE_ROWS_LIMIT)
        else:
            ts_min = time.time() - self.slow_lookback_days * 86400
            rows = self.ledger.since_time(session_key, ts_min)
        out: list = []
        est = 2000
        for r in rows:
            c = self.token_counter.count(self._row_line(r))
            if out and est + c > self.token_budget * 0.9:
                break
            out.append(r)
            est += c
        return out

    def _context(self, session_key: str, cycle: str, rows: list,
                 cut: Optional[int] = None) -> str:
        r_seen, live_cut = self.ledger.get_recorder_state(session_key)
        cut0 = live_cut if cut is None else int(cut)     # 压缩同跑传快照（钉住不回读）
        now = time.strftime(_TIME_FMT)
        records = [r for r in rows
                   if r.type == TYPE_ASSISTANT and r.seq_id > r_seen
                   and any(getattr(c, "name", "") == RECORD_NAME for c in r.calls)]
        head = [
            f"# 记忆整理 · {'快' if cycle == 'fast' else '慢'}周期 · {now}",
            self._usage_line(session_key),
            f"# 切点 seq#{cut0}（压缩流程推进）· 已整理到 seq#{r_seen} · 未处理委托 {len(records)} 条"
            + (f"（{', '.join(f'seq#{r.seq_id}' for r in records)}）" if records else ""),
        ]
        if rows:
            head.append(f"# 流水 seq#{rows[0].seq_id}～seq#{rows[-1].seq_id}"
                        "（工具结果已略；图片不附本体）")

        parts = ["\n".join(head)]
        for rel, content in self._memory_files():
            body = content
            if "</file>" in body:                       # 结束符防截断（CDATA 兜住）
                body = "<![CDATA[" + body.replace("]]>", "]]\\>") + "]]>"
            parts.append(f'<file src="{rel}">\n{body}\n</file>')
        for r in rows:
            line = self._row_line(r)
            if line:
                parts.append(line)
        return "\n\n".join(parts)

    def _usage(self, session_key: str):
        """(tokens, predicted)：tokens 优先读 agent_loop 落的渲染用量，缺数据
        （如刚重启没渲染过）按活行成本现算兜底。"""
        stats = self.stats_provider(session_key) if self.stats_provider else None
        if stats is not None and stats.tokens:
            return stats.tokens, stats.predicted
        _, cut = self.ledger.get_recorder_state(session_key)
        rows = self.ledger.since(session_key, max(cut - 1, 0),
                                 limit=LIVE_ROWS_LIMIT)
        return rows_cost(rows, self.token_counter), 0

    def _usage_line(self, session_key: str) -> str:
        """常驻数字行：当前上下文 token 数 / 预算 / 占比（估算，锚定预测另标）。"""
        tokens, predicted = self._usage(session_key)
        budget = self.token_budget
        pct = tokens / budget if budget else 0.0
        line = f"# 上下文用量：{tokens:,}/{budget:,} token（{pct:.0%}"
        if predicted:
            line += f"；锚定预测 {predicted:,}"
        return line + "）"

    def _memory_files(self) -> List[tuple]:
        """记忆文件全量（old/ 除外），(相对路径, 内容)。只收文本（记忆库是文本世界）。"""
        out = []
        for p in sorted(self.memory_dir.rglob("*")):
            if not p.is_file() or "old" in p.relative_to(self.memory_dir).parts:
                continue
            if p.name.startswith("."):
                continue
            try:
                head = p.open("rb").read(8192)
                if b"\x00" in head:            # 二进制不喂（库里可能混进别的文件）
                    continue
                out.append((str(p.relative_to(self.memory_dir)),
                            p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        return out

    _row_line = staticmethod(row_line)


class RecorderScheduler:
    """停顿（快周期）/ 低频（慢周期）调度。全程静默，不打扰用户。

    快周期锚点 = **任务执行收工时刻**（turn 结束）：收工后静默超过 debounce 才整理，
    任务跑着绝不插队（锚"末条消息"会被长任务的间隙误触发，活干一半就整理）。
    本进程没跑过 turn 的会话退回"末条行静默"（重启后欠的整理也有人管）。
    慢周期到点也给无新增的会话跑（重推/归档/合并是它的活，不依赖新增）。
    """

    def __init__(self, recorder: Recorder, ledger: MessageLedger, *,
                 debounce_seconds: int = 150, slow_interval: int = 86400,
                 tick_seconds: float = 15.0, turn_state=None) -> None:
        self.recorder = recorder
        self.ledger = ledger
        self.turn_state = turn_state      # AgentLoop.turn_state：(是否正忙, 收工时刻)
        self.debounce = float(debounce_seconds)
        self.slow_interval = float(slow_interval)
        self.tick = float(tick_seconds)

    async def run_forever(self) -> None:
        last_slow = time.time()
        while True:
            try:
                await asyncio.sleep(self.tick)
                now = time.time()
                slow_due = now - last_slow >= self.slow_interval
                for key in self.ledger.session_keys():
                    if key.startswith("group:"):
                        continue   # 群桶是 chatagent 的（自己静默压缩），R 不插手：
                        # 别把群聊内容写进主记忆
                    if self._settled(key, now) and self.recorder.pending(key):
                        await self.recorder.run(key, "fast")
                    if slow_due:
                        await self.recorder.run(key, "slow")
                if slow_due:
                    last_slow = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[recorder] 调度循环异常（继续）")

    def _settled(self, session_key: str, now: float) -> bool:
        """任务收工后静默够久 → 该整理了。正忙绝不动。

        没有 turn_state（或本进程没跑过 turn）→ 退回"末条行静默"。"""
        if self.turn_state is not None:
            busy, done_at = self.turn_state(session_key)
            if busy:
                return False
            if done_at is not None:
                return done_at <= now - self.debounce
        recent = self.ledger.tail(session_key, limit=1)
        return bool(recent) and recent[0].timestamp < now - self.debounce


def _esc(s: str) -> str:
    """属性/正文转义（与 M 侧协议一致：引号与标签边界）。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
