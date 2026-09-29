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

from ..tools.memory_tools import build_memory_registry, record_payload
from .ledger import (TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_CALL, TYPE_TOOL_RESULT,
                     TYPE_USER, MessageLedger)
from .llm import LLMClient
from ..providers.tokens import HeuristicTokenCounter, TokenCounter

logger = logging.getLogger("neoagent.recorder")

RECORD_NAME = "record_memory"      # M 的提案工具名（流水中唯一保留的工具痕迹）

_SYSTEM = """# 你是 neoagent 的记忆整理模块（Recorder）

你没有声音，不和用户对话。你有两个工作对象：
① data/memory/ 下的记忆文件（你是唯一写者）；② 一段待整理的对话流水。

## 每次跑大致做完这几件事
1. 处理流水里的 <record> 委托，逐条落实到记忆文件；
2. 从流水里抽取"这段对话结束后仍然成立的事实"写入记忆；
   "当前在忙什么"这类工作状态一般不记——那属于上下文。
3. 判断上下文切点（set_cut_point）：切点之前的上下文已无关，可以退役。
   切点之前如果还有你觉得以后会用到的内容，一般先写进记忆再切；
   拿不准就不切。切点只进不退。
4. 维护记忆文件：修失效、并重复、把长时间用不到的移进 old/、删垃圾。

## 记忆的样子
- memory.md 直接进 M 的上下文，尽量自足——日常不读子文件也能过；
  细节多、主题明确的内容放子文件，memory.md 的指针节留一行（路径 + 一句话）。
  什么时候开子文件（参考）：具体项目 / 应用与服务 / 长期事务 / 人物。
  有明确主题、会持续增补、细节多到撑爆正文才开；零散事实留在正文。
- 条目写成干净的事实：不写证据编号、不写复核计划；
  日期只在本身是事实的一部分时自然出现。
- old/ 放"一段时间内大概率不需要读取或修正"的内容，一般是把子文件移进去；
  在 memory.md 的归档节留索引。
- 你可以自由修改、重排、删除任何记忆内容——以"现在什么是对的"为准，
  用户说的话也可能变。写坏了可以从对话流水重新整理出来，不必过度保守。

## 输出
改记忆和切点都用工具，不要把记忆内容写在回复里；回复只是简短的工作简报。
"""

_CYCLE_NOTES = {
    "fast": """## 本次是快周期整理
侧重：查漏补缺、修正记忆；把 M 踩过的坑里值得记的归纳下来；
一般以增加为主、删除为辅。上下文切点的判断也在这次做（拿不准就先不动）。""",
    "slow": """## 本次是慢周期整理
侧重：更大幅度的整理——用户习惯、M 的经验这类需要长期推演的归纳；
对过时或暂时用不到的记忆做归档/清理；对记忆内容做合并与重排。
上下文切点一般不在这次判断。""",
}

# 流水视图里图片不附本体，只留存在标记（值得记的点一般已被 M 说出来）
_IMG_NOTE = "（附图 {n} 张）"
_FILE_NOTE = "（附文件 {names}）"
_TIME_FMT = "%Y-%m-%d %H:%M"


class Recorder:
    """一次整理跑 = 调 run()。多会话：游标按 session_key 各记，记忆文件全局共享。"""

    def __init__(self, llm: LLMClient, ledger: MessageLedger, memory_dir: Path, *,
                 token_counter: Optional[TokenCounter] = None,
                 token_budget: int = 250_000,
                 max_tool_rounds: int = 12,
                 effort_provider: Optional[Callable[[], str]] = None,
                 slow_lookback_days: int = 7) -> None:
        self.llm = llm
        self.ledger = ledger
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.token_counter = token_counter or HeuristicTokenCounter()
        self.token_budget = int(token_budget)      # 与 M 共用同一配置值
        self.max_tool_rounds = int(max_tool_rounds)
        self.effort_provider = effort_provider or (lambda: "medium")
        self.slow_lookback_days = int(slow_lookback_days)
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
    async def run(self, session_key: str, cycle: str = "fast") -> str:
        """跑一次整理，返回工作简报（只进日志）。"""
        async with self._lock:
            return await self._run_locked(session_key, cycle)

    async def _run_locked(self, session_key: str, cycle: str) -> str:
        rows = self._window(session_key, cycle)
        if not rows and cycle == "fast":
            return "（无待整理流水）"

        registry = build_memory_registry(self.ledger, session_key, self.memory_dir)
        msgs = [
            {"role": "system", "content": _SYSTEM + "\n" + _CYCLE_NOTES.get(cycle, "")},
            {"role": "user", "content": self._context(session_key, cycle, rows)},
        ]

        report = ""
        last_usage: Optional[dict] = None
        for _ in range(self.max_tool_rounds):
            result = await self.llm.chat(msgs, tools=registry.schemas(),
                                         reasoning_effort=self.effort_provider())
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
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": str(out)[:2000]})
        else:
            logger.warning("[recorder] 整理达到工具轮上限 %d，提前收束 session=%s",
                           self.max_tool_rounds, session_key)

        # 推进增量游标（委托随之算已消化——"委托不丢"靠切点不越未消化行保证）
        if rows:
            self.ledger.set_recorder_state(session_key, r_seen_seq=rows[-1].seq_id)
        logger.info("[recorder] %s整理完成 session=%s 流水 %s～%s%s",
                    "快" if cycle == "fast" else "慢", session_key,
                    f"msg#{rows[0].seq_id}" if rows else "（空）",
                    f"msg#{rows[-1].seq_id}" if rows else "（空）",
                    f" prompt={last_usage.get('prompt_tokens')}" if last_usage else "")
        return report or "（静默收工）"

    # ------------------------------------------------------------------
    # 窗口与上下文构造
    # ------------------------------------------------------------------
    def _window(self, session_key: str, cycle: str) -> list:
        """快周期 = 切点～现在（= M 当前跨度）；慢周期 = 回看起点～现在。"""
        _, cut = self.ledger.get_recorder_state(session_key)
        if cycle == "fast":
            rows = self.ledger.since(session_key, max(cut - 1, 0))
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

    def _context(self, session_key: str, cycle: str, rows: list) -> str:
        r_seen, cut = self.ledger.get_recorder_state(session_key)
        now = time.strftime(_TIME_FMT)
        records = [r for r in rows
                   if r.type == TYPE_TOOL_CALL and r.seq_id > r_seen
                   and any(getattr(c, "name", "") == RECORD_NAME for c in r.calls)]
        head = [
            f"# 记忆整理 · {'快' if cycle == 'fast' else '慢'}周期 · {now}",
            f"# 切点 msg#{cut} · 已整理到 msg#{r_seen} · 未处理委托 {len(records)} 条"
            + (f"（{', '.join(f'msg#{r.seq_id}' for r in records)}）" if records else ""),
        ]
        if rows:
            head.append(f"# 流水 msg#{rows[0].seq_id}～msg#{rows[-1].seq_id}"
                        "（工具调用与结果已略；图片不附本体）")

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
        """一行账 → 流水视图的一条（同 M 的消息协议；工具行只剩 record 委托）。"""
        if r.type == TYPE_TOOL_RESULT:
            return ""
        ts = time.strftime(_TIME_FMT, time.localtime(r.timestamp))
        if r.type == TYPE_TOOL_CALL:
            lines = []
            for c in r.calls:
                if getattr(c, "name", "") == RECORD_NAME:
                    payload = record_payload({"arguments": getattr(c, "arguments", "")})
                    lines.append(f'<record seq="msg#{r.seq_id}" time="{ts}">'
                                 f'{_esc(payload)}</record>')
            return "\n".join(lines)
        if r.type not in (TYPE_USER, TYPE_ASSISTANT, TYPE_INTERNAL):
            return ""
        body = r.text
        imgs = sum(1 for a in r.attachments if a.kind == "image")
        files = [a.filename for a in r.attachments if a.kind != "image" and a.kind != "audio"]
        if imgs:
            body += _IMG_NOTE.format(n=imgs)
        if files:
            body += _FILE_NOTE.format(names=", ".join(files))
        reply = f' replyTo="msg#{r.reply_to.seq_id}"' if r.reply_to and r.reply_to.seq_id else ""
        return (f'<message sender="{_esc(r.sender_name)}" id="msg#{r.seq_id}"'
                f' time="{ts}"{reply}>{_esc(body)}</message>')


def _esc(s: str) -> str:
    """属性/正文转义（与 M 侧协议一致：引号与标签边界）。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class RecorderScheduler:
    """停顿（快周期）/ 低频（慢周期）调度。全程静默，不打扰用户。

    停顿判定无状态：游标后有新增、且最新一行已静默超过 debounce → 对话停顿了。
    慢周期到点也给无新增的会话跑（重推/归档/合并是它的活，不依赖新增）。
    """

    def __init__(self, recorder: Recorder, ledger: MessageLedger, *,
                 debounce_seconds: int = 150, slow_interval_hours: int = 24,
                 tick_seconds: float = 15.0) -> None:
        self.recorder = recorder
        self.ledger = ledger
        self.debounce = float(debounce_seconds)
        self.slow_interval = float(slow_interval_hours) * 3600
        self.tick = float(tick_seconds)

    async def run_forever(self) -> None:
        last_slow = time.time()
        while True:
            try:
                await asyncio.sleep(self.tick)
                now = time.time()
                slow_due = now - last_slow >= self.slow_interval
                for key in self.ledger.session_keys():
                    if self._quiet(key, now) and self.recorder.pending(key):
                        await self.recorder.run(key, "fast")
                    if slow_due:
                        await self.recorder.run(key, "slow")
                if slow_due:
                    last_slow = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[recorder] 调度循环异常（继续）")

    def _quiet(self, session_key: str, now: float) -> bool:
        recent = self.ledger.tail(session_key, limit=1)
        return bool(recent) and recent[0].timestamp < now - self.debounce
