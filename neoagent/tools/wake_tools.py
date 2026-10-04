"""M 的唤醒工具：schedule_wake / list_wakes / cancel_wake。

M 用它们给未来的自己排唤醒（闹钟/自查/惦记），落 wakes 表（重启不丢），
到点由 WakeScheduler 注入 <wake> 块——说不说仍由 M 决定。

要求都写在描述里（note 便利贴口吻、时间格式、cron 语义），不在人格里立规矩：
工具是 M 调用那一刻看得最全的地方。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict

from ..core.proactive import CRON_HELP, next_fire, wake_line
from .format import error
from .registry import ToolRegistry


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def register_wake_tools(registry: ToolRegistry, ledger, session_key: str) -> None:
    """注册进 M 的工具面。session_key = 时间线桶（产品层一条连续记录）。"""

    async def schedule_wake(args: Dict[str, Any]) -> str:
        note = str(args.get("note") or "").strip()
        if not note:
            return error("bad_param", "缺少 note（写给未来自己的便利贴）")
        at_raw = str(args.get("at") or "").strip()
        every = " ".join(str(args.get("every") or "").split())
        now = time.time()
        if every:
            try:
                nxt = next_fire(every, now)
            except ValueError as exc:
                return error("bad_param", f"cron 表达式不对：{exc}\n{CRON_HELP}")
            if nxt is None:
                return error("bad_param",
                             f"这个 cron 5 年内不会有触发时刻：{every!r}\n{CRON_HELP}")
            wid = ledger.add_wake(session_key=session_key, at=nxt,
                                  note=note, every=every)
            return f"已排 w{wid}：{every}（下次触发 {_fmt_ts(nxt)}）"
        if not at_raw:
            return error("bad_param",
                         "at 与 every 至少给一个：一次性给 at，重复给 every")
        try:
            at = datetime.fromisoformat(at_raw).timestamp()
        except ValueError:
            return error("bad_param",
                         f"at 读不懂：{at_raw!r}（格式 2026-02-24T08:30，本机时间；"
                         "自然语言时间先自己换算好）")
        if at <= now:
            return error("bad_param",
                         f"at（{_fmt_ts(at)}）已经过去了（现在 {_fmt_ts(now)}）。"
                         "想提醒过去的事不用排闹钟，直接说就行")
        wid = ledger.add_wake(session_key=session_key, at=at, note=note)
        return f"已排 w{wid}：{_fmt_ts(at)}（一次性）"

    async def list_wakes(args: Dict[str, Any]) -> str:
        items = ledger.list_wakes(session_key)
        return "\n".join(wake_line(w) for w in items) if items else "唤醒清单是空的"

    async def cancel_wake(args: Dict[str, Any]) -> str:
        raw = str(args.get("wake_id") or "").strip().lstrip("wW")
        if not raw.isdigit():
            return error("bad_param",
                         f"wake_id 读不懂：{args.get('wake_id')!r}（填 w13 或 13）")
        result = ledger.cancel_wake(int(raw))
        return f"w{raw} 已取消" if result == "cancelled" else result

    registry.register(
        "schedule_wake",
        "给未来的自己排一次唤醒（闹钟/自查/惦记）。到点时你会收到一条 <wake> 块消息，"
        "由你决定说不说、说什么。\n"
        "什么时候用：\n"
        "- 用户要提醒：\"明早 8 点提醒我带作业\" → at=明早 8 点；\n"
        "- 重复提醒：\"每天 9 点报下日程\" → every=cron；\n"
        "- 用户说\"一会再说 / 回头再看 / 晚点弄 / 过两小时看下…\" → 排个 once，"
        "note 写清到点该干什么（如\"看下 xx 脚本跑完没，先用工具查一眼，再决定报不报\"）。\n"
        "note 是你写给未来自己的便利贴，到点时它是唯一的线索：写你自己的备忘"
        "（好：\"明早面试带作业，简历在桌面\"），不要写成用户的口吻或转述用户的话"
        "（坏：\"提醒我带作业\"、\"用户让我提醒他\"）。\n"
        "at 与 every 二选一（都给则以 every 为准）：\n"
        "- at：一次性，本机时间的绝对时刻，格式 2026-02-24T08:30——\"明早 8 点\"这类"
        "自然语言由你换算好再填，别把自然语言原样塞进来；\n"
        f"- every：重复，cron 表达式。{CRON_HELP}\n"
        "返回带下次触发时间，务必转告用户，让他能当场纠正。"
        "改期 = cancel_wake 旧的 + schedule_wake 新的。",
        {"type": "object",
         "properties": {
             "note": {"type": "string",
                      "description": "给未来自己的便利贴：自足、口语、不是用户口吻"},
             "at": {"type": "string",
                    "description": "一次性触发时刻（2026-02-24T08:30，本机时间）"},
             "every": {"type": "string",
                       "description": "重复触发的 cron 表达式（5 字段：分 时 日 月 周）"},
         },
         "required": ["note"]},
        schedule_wake)

    registry.register(
        "list_wakes",
        "看你的唤醒清单（id、时刻、cron、note、状态）。要改期/取消先来这查 id。",
        {"type": "object", "properties": {}},
        list_wakes)

    registry.register(
        "cancel_wake",
        "取消一个还没触发的唤醒。用户说\"不用提醒了\"\"取消闹钟\"时用。",
        {"type": "object",
         "properties": {
             "wake_id": {"type": "string", "description": "唤醒 id（w13 或 13）"},
         },
         "required": ["wake_id"]},
        cancel_wake)
