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
from typing import Callable, Dict, List, Optional, Tuple

from .frames import wake_block

logger = logging.getLogger(__name__)

# cron 字段说明（写进工具描述/错误回报——"说清楚每位是什么意思"）
CRON_HELP = (
    "cron 表达式是 5 个字段，空格分隔：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6，0=周日)。"
    "每个字段可写：*（任意）、数字、a-b（范围）、*/n（每隔 n）、a-b/n、逗号列表。"
    "例：\"0 9 * * *\"=每天 9:00；\"0 9 * * 1-5\"=工作日 9:00；\"0 10 1 * *\"=每月 1 号 10:00；"
    "\"*/15 9-18 * * *\"=9-18 点每 15 分钟。"
    "注意：\"9 * * * *\"是每小时的第 9 分钟，不是 9 点；日与周都限定时，任一满足即触发。"
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
                 tick_seconds: float = 10.0) -> None:
        self.ledger = ledger
        self.gate = gate
        self.bucket = bucket
        self.outbound_ready = outbound_ready or (lambda: True)
        self.responder = responder          # 主动出站口（重启后 st.responder 没绑也发得出）
        self.tick = float(tick_seconds)

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            try:
                self.fire_due()
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
