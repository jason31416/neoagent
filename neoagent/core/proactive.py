"""主动性（proactive）：让 neoagent 自己决定何时请求自己。

两支唤醒源共用一条注入路径（SettleGate.push_wake → <wake> 块入账 → 放行一轮，
M 醒来自己决定说不说）：

- **闹钟**（本文件 WakeScheduler）：M 用 schedule_wake 排的（once / cron 重复），
  落 wakes 表、重启不丢；到点注入。停机期间过点的项在出口就绪后合并成
  "补漏清单"（一条消息并列多个 <wake> 块），M 一轮处置；
- **哨兵**（ProactiveProbe，Jev 便宜探测）：定期看一眼"现在值不值得主动开口"，
  值得才叫醒 M（未配 jev 则整个不存在）。

cron 解析（parse_cron / next_fire）只拦**确定的语法错**（字段数/越界/非法字符），
语义不替 M 做主——"9 * * * *"万一是真想每小时 09 分。纠错靠工具回显下次触发时间。

设计见 docs/设计-主动性.md。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .frames import wake_block
from .ledger import TYPE_TOOL_RESULT, TYPE_USER, TYPE_WAKE

logger = logging.getLogger(__name__)

# cron 字段说明（写进工具描述/错误回报——"说清楚每位是什么意思"）
CRON_HELP = (
    "cron 表达式是 5 个字段，空格分隔：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6，0=周日)。"
    "每个字段可写：*（任意）、数字、a-b（范围）、*/n（每隔 n）、a-b/n、逗号列表。"
    "例：\"0 9 * * *\"=每天 9:00；\"0 9 * * 1-5\"=工作日 9:00；\"0 10 1 * *\"=每月 1 号 10:00；"
    "\"*/15 9-18 * * *\"=9-18 点每 15 分钟。"
    "日与周都限定时，任一满足即触发。"
)

# 字段名 → (下界, 上界)；周多收 7（=周日）
_FIELD_RANGES: Tuple[Tuple[str, int, int], ...] = (
    ("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31),
    ("month", 1, 12), ("dow", 0, 7),
)


def _parse_field(field: str, lo: int, hi: int, name: str) -> set:
    out: set = set()
    for part in (field or "").split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"{name} 字段的步长 {step_s!r} 不是正整数")
            step = int(step_s)
            part = base or "*"
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, _, b_s = part.partition("-")
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError(f"{name} 字段的范围 {part!r} 不是 a-b 这样的数字范围")
            a, b = int(a_s), int(b_s)
            if a > b:
                raise ValueError(f"{name} 字段的范围 {part!r} 起点大于终点")
        else:
            if not part.isdigit():
                raise ValueError(f"{name} 字段的值 {part!r} 不是数字、*、a-b 或 */n")
            a = b = int(part)
        if a < lo or b > hi:
            raise ValueError(f"{name} 字段的值 {part!r} 超出范围 {lo}-{hi}")
        out.update(range(a, b + 1, step))
    if not out:
        raise ValueError(f"{name} 字段为空")
    return out


def parse_cron(expr: str) -> Tuple[set, set, set, set, set, bool, bool]:
    """解析 5 字段 cron → (分, 时, 日, 月, 周, 日任意?, 周任意?)。

    只拦确定的语法错（ValueError 带人话说明）；语义不判断。
    周日两写法（0 或 7）归一成 0。
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式要 5 个字段，收到 {len(fields)} 个")
    mins = _parse_field(fields[0], 0, 59, "minute")
    hours = _parse_field(fields[1], 0, 23, "hour")
    doms = _parse_field(fields[2], 1, 31, "day")
    months = _parse_field(fields[3], 1, 12, "month")
    dows = _parse_field(fields[4], 0, 7, "dow")
    dows = {0 if d == 7 else d for d in dows}
    return mins, hours, doms, months, dows, fields[2] == "*", fields[4] == "*"


def next_fire(expr: str, after: float) -> Optional[float]:
    """cron 在 after 之后的下一次触发时刻（本机时间戳）；5 年内没有就 None。

    日/周的组合语义按通行 cron：都限定时任一满足即触发。
    """
    mins, hours, doms, months, dows, dom_any, dow_any = parse_cron(expr)
    t = datetime.fromtimestamp(after).replace(second=0, microsecond=0)
    t += timedelta(minutes=1)
    limit = t.replace(year=t.year + 5)
    while t < limit:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)) \
                .replace(day=1, hour=0, minute=0)
            continue
        wd = (t.weekday() + 1) % 7          # cron 周日=0
        if dom_any and dow_any:
            day_ok = True
        elif dom_any:
            day_ok = wd in dows
        elif dow_any:
            day_ok = t.day in doms
        else:
            day_ok = (t.day in doms) or (wd in dows)
        if not day_ok:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hours:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in mins:
            t += timedelta(minutes=1)
            continue
        return t.timestamp()
    return None


def wake_line(w) -> str:
    """清单里的一行（/wakes 指令与 list_wakes 工具共用）。"""
    rep = f"（{w.every}）" if w.every else "（once）"
    return (f"w{w.id} [{w.status}] {datetime.fromtimestamp(w.at):%Y-%m-%d %H:%M}"
            f" {rep} {w.note}")


def in_quiet_hours(spec: str, now: Optional[float] = None) -> bool:
    """"23:30-08:00" 这类免打扰时段判断（跨午夜也对）；空/写错 = 不免打扰。"""
    spec = (spec or "").strip()
    if not spec:
        return False
    try:
        a, b = spec.split("-", 1)
        ha, ma = a.strip().split(":")
        hb, mb = b.strip().split(":")
        start = int(ha) * 60 + int(ma)
        end = int(hb) * 60 + int(mb)
    except (ValueError, AttributeError):
        return False
    cur = datetime.fromtimestamp(now if now is not None else time.time())
    mins = cur.hour * 60 + cur.minute
    if start > end:                       # 跨午夜（23:30-08:00）
        return mins >= start or mins < end
    return start <= mins < end


class WakeScheduler:
    """闹钟调度：到点注入 <wake>（M 决定说不说）；重启不丢、过点合并补漏。

    - 出口没就绪（home 频道还没连上）不注入——到点的项留着，就绪后合并补发；
    - 同一 tick 里到点的项合成**一条消息并列多块**（补漏清单与平时触发同款格式）；
    - 重复项（every=cron）触发后按 cron 续排；停机积压的过次不逐一补，
      块上 at=最近一次该响的时刻，next_at=下一次未来时刻。
    """

    def __init__(self, ledger, gate, *, bucket: str,
                 outbound_ready: Optional[Callable[[], bool]] = None,
                 responder: Optional[Callable] = None,
                 probe=None, tick_seconds: float = 10.0) -> None:
        self.ledger = ledger
        self.gate = gate
        self.bucket = bucket
        self.outbound_ready = outbound_ready or (lambda: True)
        self.responder = responder          # 主动出站口（重启后 st.responder 没绑也发得出）
        self.probe = probe                  # 哨兵（可空）：没配 jev 就没有
        self.tick = float(tick_seconds)

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            try:
                self.fire_due()
                if self.probe is not None:
                    await self.probe.maybe_probe()
            except Exception:
                logger.exception("[wake] 调度异常")

    def fire_due(self) -> int:
        """把所有到点的唤醒注入出去；返回注入的消息条数（0=没活/没就绪）。"""
        if not self.outbound_ready():
            return 0
        due = self.ledger.due_wakes()
        if not due:
            return 0
        now = time.time()
        by_bucket: Dict[str, List] = {}
        for w in due:
            by_bucket.setdefault(w.session_key or self.bucket, []).append(w)
        sent = 0
        for key, items in by_bucket.items():
            blocks: List[str] = []
            for w in items:
                at, nxt = self._advance(w, now)
                blocks.append(wake_block(w.id, ts=now, source=w.source or "alarm",
                                         at=at, note=w.note, every=w.every))
                self.ledger.mark_fired(w.id, fired_at=now,
                                       next_at=nxt)   # nxt=None → 转 fired 回收
            logger.info("[wake] 注入 %d 个唤醒（%s）", len(blocks),
                        ",".join(f"w{w.id}" for w in items))
            self.gate.push_wake(key, "".join(blocks), responder=self.responder)
            sent += 1
        return sent

    def _advance(self, w, now: float) -> Tuple[float, Optional[float]]:
        """重复项把过点的次数跳掉：at=最近一次该响的时刻，next=下一次未来时刻。"""
        if not w.every:
            return w.at, None
        try:
            occ, nxt = w.at, next_fire(w.every, w.at)
            while nxt is not None and nxt <= now:
                occ, nxt = nxt, next_fire(w.every, nxt)
            return occ, nxt
        except ValueError as exc:
            logger.warning("[wake] w%d 的 cron %r 坏了：%s（按一次性处理）",
                           w.id, w.every, exc)
            return w.at, None


# ----------------------------------------------------------------------
# 哨兵（Jev 自主探测）
# ----------------------------------------------------------------------
# 判定题（单 choice，例子校准）。注意 Jev **不生成文字**——判定档位就是它的
# 全部输出，"该说什么"由 M 醒来看上下文自己定（产品文档：条件成立≠该开口）。
_PROBE_QUESTION = {
    "should_poke": {
        "type": "choice",
        "instructions": (
            "判断现在值不值得叫醒 agent 进行操作。\n"
            "- 不值得唤醒：没什么悬着的事；刚聊完正常收尾；用户明确说过不用管"
            "agent 最近为此主动提过、用户没接（一两次没下文就撒手）。\n"
            "- 值得提醒：用户提过\"一会再说/回头再看\"之类；记忆中悬着的事，一直没下文，"
            "而且闹钟清单里没有闹钟在管它。\n"
            "- 值得关心跟进：用户透露的状态值得关心一句"),
        "criteria": {"不值得": None, "值得提醒": None, "值得关心跟进": None},
    }
}

_POKE_LABELS = ("值得提醒", "值得关心跟进")
_PROBE_WINDOW_ROWS = 120        # 流水窗口（行）
_PROBE_LOOKBACK_DAYS = 3       # 统计回看（天）


class ProactiveProbe:
    """哨兵：周期性"看一眼值不值得叫醒 M"）。

    - 节奏自适应：刚活跃隔 probe_active_min，静默越久越稀（线性到 probe_idle_max）；
      忙时/免打扰时段不探；
    - 材料喂全：最近流水（M 看到的原样）+ 记忆 + **pending 唤醒清单** + 机械统计
      （近期叫醒次数、其后用户回没回）——"已经有人管/已经提过没下文"是数据不是规矩；
    - 判定值得 → wakes 表记一条 source="probe"，走闹钟同一条注入路
      （叫醒动作本身落账，沉默与否都留痕）。
    """

    def __init__(self, agent, jev, cfg_proactive, *, bucket: str) -> None:
        self.agent = agent
        self.ledger = agent.ledger
        self.jev = jev
        self.cfg = cfg_proactive
        self.bucket = bucket
        self.triggered = 0                 # 本次进程叫醒次数
        self._next_at = time.time() + float(cfg_proactive.probe_active_min)

    async def maybe_probe(self) -> bool:
        """到点就探一次；返回是否叫醒了 M。任何失败都静默跳过。"""
        now = time.time()
        if self.jev is None or now < self._next_at:
            return False
        if in_quiet_hours(self.cfg.quiet_hours, now):
            self._next_at = now + min(float(self.cfg.probe_active_min), 600.0)
            return False
        busy, done_at = self.agent.turn_state(self.bucket)
        if busy:
            self._next_at = now + 60        # 正忙就不添乱，过会儿再看
            return False
        silence = max(0.0, now - (done_at or self._last_ts(now) or now))
        frac = min(silence / 86400.0, 1.0)
        self._next_at = now + (float(self.cfg.probe_active_min)
                               + (float(self.cfg.probe_idle_max)
                                  - float(self.cfg.probe_active_min)) * frac)
        answers = await self.jev.ask(self._state(now, silence), _PROBE_QUESTION)
        a = (answers or {}).get("should_poke") or {}
        choice = str(a.get("choice") or "")
        conf = float(a.get("confidence") or 0)
        if choice not in _POKE_LABELS or conf < 0.5:
            logger.info("[probe] 不叫醒（%s conf=%.2f）", choice or "无判定", conf)
            return False
        wid = self.ledger.add_wake(session_key=self.bucket, at=now,
                                   note=f"哨兵判定：{choice}", source="probe")
        self.triggered += 1
        logger.info("[probe] 值得叫醒（%s conf=%.2f）→ w%d", choice, conf, wid)
        return True

    # -------------------------------------------------------------- 材料

    def _last_ts(self, now: float) -> float:
        rows = self.ledger.visible(self.bucket, 1)
        return rows[-1].timestamp if rows else now

    def _state(self, now: float, silence: float) -> str:
        parts = [f"# 现在时间 {datetime.fromtimestamp(now):%Y-%m-%d %H:%M}"]
        mem = self._memory()
        if mem:
            parts.append(f"# 记忆\n{mem}")
        pend = [wake_line(w) for w in self.ledger.list_wakes(self.bucket, status="pending")]
        parts.append("# 已有闹钟（下列内容你不需要进行唤醒）\n"
                     + ("\n".join(pend) if pend else "（空）"))
        hist = self.ledger.since_time(self.bucket, now - _PROBE_LOOKBACK_DAYS * 86400,
                                      limit=500)
        pokes = [r for r in hist if r.type == TYPE_WAKE and 'source="probe"' in (r.text or "")]
        replied = sum(1 for p in pokes
                      if any(r.type == TYPE_USER and r.seq_id > p.seq_id for r in hist))
        # parts.append(f"# 近{_PROBE_LOOKBACK_DAYS}天哨兵叫醒过 {len(pokes)} 次，"
        #              f"其后用户说过话的 {replied} 次（扑空多了就撒手）")
        rows = self.ledger.visible(self.bucket, _PROBE_WINDOW_ROWS)
        lines = []
        for r in rows:
            if r.type == TYPE_TOOL_RESULT:
                continue
            ts = datetime.fromtimestamp(r.timestamp).strftime("%m-%d %H:%M")
            kind = r.type
            body = (r.text or "").strip()[:240]
            if body:
                lines.append(f"[{ts}] {kind}：{body}")
        parts.append("# 最近消息（与 neoagent 看到的一致；工具略）\n"
                     + "\n".join(lines))
        return "\n\n".join(parts)

    def _memory(self) -> str:
        try:
            return Path(self.agent.memory_file).read_text(
                encoding="utf-8", errors="replace")[:2500]
        except OSError:
            return ""
