"""R（Recorder）——记忆整理模块。

职责（设计-R记忆海马体.md）：
- 记忆的唯一写者：维护 data/memory/（memory.md + 子文件 + old/ 归档）；
- 上下文退役的判断者：set_cut_point（只进不退）；
- 双周期：快周期（查漏补缺/修正/判断切点）、慢周期（长期归纳/归档清理/合并重排），
  差异只在提示词一节，工具与预算完全一致。

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
from .render import (LIVE_ROWS_LIMIT, PRESSURE_PCT, ContextStats, rows_cost,
                     suggest_cut)
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
一般以增加为主、删除为辅。上下文的丢弃判断可以在这次做（拿不准就先不动）。
快周期的常态是什么也不做，没有值得记或者只是“或许值得记”的就不要硬记。""",
    "slow": """## 本次是慢周期整理
侧重：更大幅度的整理——用户习惯、neoagent 的经验这类需要长期推演的归纳；
对过时或暂时用不到的记忆做归档/清理；对记忆内容做合并与重排。
上下文切点一般不在这次判断。""",
}

_COMPACT_NOTE = """## 本次是用户强制压缩（/compact）
目标：把上下文切点推进到建议保留点——先把该记的记忆记掉，再退役；
能推就推（set_cut_point 只进不退，拿不准可以少清，但别一点不清）。"""

_TIME_FMT = "%Y-%m-%d %H:%M"


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
        self._lock = asyncio.Lock()            # 整理跑互斥（停顿调度/紧急压缩不并发）

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
    async def run(self, session_key: str, cycle: str = "fast",
                  compact: bool = False) -> str:
        """跑一次整理，返回工作简报（只进日志）。
        compact=True：用户强制压缩（/compact）——建议清理点必给，且明确要求推进切点。"""
        async with self._lock:
            return await self._run_locked(session_key, cycle, compact)

    async def compact(self, session_key: str) -> str:
        """用户强制压缩（/compact）：跑一次压缩整理，回报切点进展（简报给人看）。"""
        _, cut_before = self.ledger.get_recorder_state(session_key)
        await self.run(session_key, "fast", compact=True)
        _, cut_after = self.ledger.get_recorder_state(session_key)
        advice = suggest_cut(self.ledger, session_key,
                             budget=self.token_budget, counter=self.token_counter)
        if cut_after > cut_before:
            dropped = [r for r in self.ledger.since(session_key, cut_before - 1,
                                                    limit=LIVE_ROWS_LIMIT)
                       if r.seq_id < cut_after]
            freed = rows_cost(dropped, self.token_counter)
            return (f"压缩完成：切点 seq#{cut_before}→seq#{cut_after}，"
                    f"释放约 {freed:,} token（现活行约 {advice.total:,}）")
        if not advice.needed:
            return f"无需压缩（当前活行约 {advice.total:,} token，占比不高）"
        return (f"压缩未推进切点（R 拿不准，欠一次压缩；现活行约 {advice.total:,} token）")

    async def _run_locked(self, session_key: str, cycle: str,
                          compact: bool = False) -> str:
        rows = self._window(session_key, cycle)
        if not rows and cycle == "fast":
            return "（无待整理流水）"

        registry = build_memory_registry(self.ledger, session_key, self.memory_dir)
        _, cut_before = self.ledger.get_recorder_state(session_key)
        note = _CYCLE_NOTES.get(cycle, "") + ("\n" + _COMPACT_NOTE if compact else "")
        msgs = [
            {"role": "system", "content": self._system + "\n" + note},
            {"role": "user", "content": self._context(session_key, cycle, rows,
                                                     compact=compact)},
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

        # 推进增量游标（委托随之算已消化——"委托不丢"靠切点不越未消化行保证）
        if rows:
            self.ledger.set_recorder_state(session_key, r_seen_seq=rows[-1].seq_id)
        _, cut_after = self.ledger.get_recorder_state(session_key)
        cut_note = (f"切点 seq#{cut_before}→seq#{cut_after}" if cut_after != cut_before
                    else f"切点 seq#{cut_after} 未动")
        logger.info("[recorder] %s整理完成 session=%s 流水 %s～%s %s%s",
                    "快" if cycle == "fast" else "慢", session_key,
                    f"msg#{rows[0].seq_id}" if rows else "（空）",
                    f"msg#{rows[-1].seq_id}" if rows else "（空）",
                    cut_note,
                    f" prompt={last_usage.get('prompt_tokens')}" if last_usage else "")
        return report or "（静默收工）"

    # ------------------------------------------------------------------
    # 窗口与上下文构造
    # ------------------------------------------------------------------
    def _window(self, session_key: str, cycle: str) -> list:
        """快周期 = 切点～现在（= M 当前跨度）；慢周期 = 回看起点～现在。
        快周期窗口 = 切点之后**全部**流水（不设行数帽）。"""
        _, cut = self.ledger.get_recorder_state(session_key)
        if cycle == "fast":
            rows = self.ledger.since(session_key, max(cut - 1, 0),
                                     limit=LIVE_ROWS_LIMIT)
        else:
            ts_min = time.time() - self.slow_lookback_days * 86400
            rows = self.ledger.since_time(session_key, ts_min)
        # 预算封顶（视图已砍工具内容，一般远够）：超了丢最老的
        while len(rows) > 2:
            est = sum(self.token_counter.count(self._row_line(r)) for r in rows) + 2000
            if est <= self.token_budget * 0.9:
                break
            rows = rows[len(rows) // 10 or 1:]
        return rows

    def _context(self, session_key: str, cycle: str, rows: list,
                 compact: bool = False) -> str:
        r_seen, cut = self.ledger.get_recorder_state(session_key)
        now = time.strftime(_TIME_FMT)
        records = [r for r in rows
                   if r.type == TYPE_ASSISTANT and r.seq_id > r_seen
                   and any(getattr(c, "name", "") == RECORD_NAME for c in r.calls)]
        head = [
            f"# 记忆整理 · {'快' if cycle == 'fast' else '慢'}周期 · {now}",
            self._usage_line(session_key),
            f"# 切点 seq#{cut} · 已整理到 seq#{r_seen} · 未处理委托 {len(records)} 条"
            + (f"（{', '.join(f'seq#{r.seq_id}' for r in records)}）" if records else ""),
        ]
        advice_line = self._advice_line(session_key, compact)
        if advice_line:
            head.append(advice_line)
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
        """(tokens, predicted, budget, advice)：tokens 优先读 agent_loop 落的渲染用量，
        缺数据（如刚重启没渲染过）按活行成本现算兜底；预算统一用与 M 共用的配置值
        （pct 与建议点同分母，不拿 stats 里的重复预算当运算输入）。"""
        advice = suggest_cut(self.ledger, session_key,
                             budget=self.token_budget, counter=self.token_counter)
        stats = self.stats_provider(session_key) if self.stats_provider else None
        if stats is not None and stats.tokens:
            return stats.tokens, stats.predicted, self.token_budget, advice
        return advice.total, 0, self.token_budget, advice

    def _usage_line(self, session_key: str) -> str:
        """常驻数字行：当前上下文 token 数 / 预算 / 占比（估算，锚定预测另标）。"""
        tokens, predicted, budget, _ = self._usage(session_key)
        pct = tokens / budget if budget else 0.0
        line = f"# 上下文用量：{tokens:,}/{budget:,} token（{pct:.0%}"
        if predicted:
            line += f"；锚定预测 {predicted:,}"
        return line + "）"

    def _advice_line(self, session_key: str, compact: bool) -> str:
        """条件提示：占比严格超 90% 或强制压缩才提清理（平时只给数字，不没事找事）。"""
        tokens, _, budget, advice = self._usage(session_key)
        pct = tokens / budget if budget else 0.0
        if not compact and pct <= PRESSURE_PCT:
            return ""
        if not advice.needed:
            return "# 强制压缩：当前占比不高，无可清（拿不准就不动）" if compact else ""
        ts_str = time.strftime(_TIME_FMT, time.localtime(advice.ts))
        what = "强制压缩" if compact else "上下文快满了"
        return (f"# {what}（{pct:.0%}）：尽量丢弃 {ts_str} 以前的上下文"
                f"（建议保留最新约 {advice.keep_pct:.0%} 预算；{advice.reason}）")

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

    def _row_line(self, r) -> str:
        """一行 → 流水视图的一条（主表定稿原样，和 M 看到的完全一致）。"""
        if r.type == TYPE_TOOL_RESULT:
            return ""
        lines = []
        if r.text:
            lines.append(r.text)          # <message>/<recall> 等标签都已固化在文里
        if r.type == TYPE_ASSISTANT:      # record_memory 委托随轮内联（工具行不进流水）
            ts = time.strftime(_TIME_FMT, time.localtime(r.timestamp))
            for c in r.calls:
                if getattr(c, "name", "") == RECORD_NAME:
                    payload = record_payload({"arguments": getattr(c, "arguments", "")})
                    lines.append(f'<record time="{ts}">{_esc(payload)}</record>')
        return "\n".join(lines)


def _esc(s: str) -> str:
    """属性/正文转义（与 M 侧协议一致：引号与标签边界）。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


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
                        # 别把群聊内容写进主记忆、别跟它的压缩抢切点
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
