"""主 agent 的群工具：list_groups / read_group_chat / tell_chatagent。

给主 agent 看的群聊一切走**紧凑格式**（`14:02 短码: 内容`，owner 加 [owner]，
图出 [图]）；chatagent 自己的上下文才是 <message> 定稿——
存储一份定稿，读侧渲染紧凑，两套格式各归各。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List

from ..core.frames import short_id
from ..log import green
from .format import ToolOutput, error
from .registry import ToolRegistry

LOG_LIMIT = 200        # 单次返回上限：超出省中间留头尾


def render_log_line(m: Any, *, with_date: bool) -> str:
    """群记录一行：`14:02 [owner] 短码: 内容`（跨天带日期前缀）。"""
    ts = datetime.fromtimestamp(m.timestamp)
    stamp = ts.strftime("%Y-%m-%d %H:%M") if with_date else ts.strftime("%H:%M")
    owner = " [owner]" if m.is_owner else ""
    return f"{stamp}{owner} {short_id(m.sender)}: {m.text}"


def render_log(rows: List[Any], *, omitted: int, with_date: bool) -> str:
    lines = [render_log_line(m, with_date=with_date) for m in rows]
    if omitted > 0:
        half = len(lines) // 2
        lines.insert(half, f"……（中间省略 {omitted} 条）……")
    return "\n".join(lines)


def _parse_ts(value: Any, field: str) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(field)


def register_group_tools(registry: ToolRegistry, *, ledger: Any,
                         manager: Any) -> None:

    async def list_groups(args: Dict[str, Any]) -> ToolOutput:
        chats = ledger.group_log_chats()
        if not chats:
            return ToolOutput(text="还没有群聊记录", stat="0 个群",
                              fields=[("delta", "+0")])
        lines = [f"{cid}    {ledger.get_setting(f'group_title:{cid}') or '（未知名）'}"
                 f"{'    [活跃]' if manager.is_active(cid) else ''}"
                 for cid in chats]
        return ToolOutput(
            text="\n".join(lines),
            stat=f"{green('+' + str(len(chats)))} 个群",
            fields=[("delta", f"+{len(chats)}")])

    registry.register(
        "list_groups",
        "列出所有有聊天记录的群（群 id + 群名），拿到群 id 再去查记录。",
        {"type": "object", "properties": {}},
        list_groups)

    async def read_group_chat(args: Dict[str, Any]) -> ToolOutput:
        chat_id = str(args.get("group") or "").strip()
        if not chat_id:
            return error("bad_param", "group 必填（用 list_groups 拿群 id）")
        if chat_id not in ledger.group_log_chats():
            return error("not_found", f"没有这个群的记录：{chat_id}（用 list_groups 查）")
        try:
            start = _parse_ts(args.get("start"), "start")
            end = _parse_ts(args.get("end"), "end")
        except ValueError:
            return error("bad_param", "时间格式：YYYY-MM-DD HH:MM（如 2026-03-14 09:00）")
        if not start and not end:
            start = time.time() - 86400       # 缺省看最近一天
        query = str(args.get("query") or "").strip()

        total = ledger.group_log_count(chat_id, start=start, end=end, query=query)
        if total == 0:
            return ToolOutput(text="这段时间没有记录（换个时间段或去掉 query）",
                              stat="0 条消息", fields=[("delta", "+0")])
        if total <= LOG_LIMIT:
            rows = ledger.group_log_range(chat_id, start=start, end=end,
                                          query=query, limit=LOG_LIMIT)
            omitted = 0
        else:
            half = LOG_LIMIT // 2
            head = ledger.group_log_range(chat_id, start=start, end=end,
                                          query=query, limit=half)
            tail = ledger.group_log_range(chat_id, start=start, end=end,
                                          query=query, limit=half, desc=True)
            rows = head + list(reversed(tail))
            omitted = total - len(rows)
        dates = {datetime.fromtimestamp(m.timestamp).date() for m in rows}
        text = render_log(rows, omitted=omitted, with_date=len(dates) > 1)
        return ToolOutput(
            text=text,
            stat=f"{green('+' + str(total))} 条消息（{ledger.get_setting(f'group_title:{chat_id}') or chat_id}）",
            fields=[("delta", f"+{total}")])

    registry.register(
        "read_group_chat",
        "读某个群在一段时间内的聊天记录（文字，保留期内；图片只出 [图] 占位）。",
        {"type": "object",
         "properties": {
             "group": {"type": "string", "description": "群 id（用 list_groups 拿）"},
             "start": {"type": "string", "description": "起点，如 2026-03-14 09:00；缺省最近一天"},
             "end": {"type": "string", "description": "终点，同格式；缺省到现在"},
             "query": {"type": "string", "description": "关键词过滤，可选"},
         },
         "required": ["group"]},
        read_group_chat)

    async def tell_chatagent(args: Dict[str, Any]) -> ToolOutput:
        chat_id = str(args.get("group") or "").strip()
        message = str(args.get("message") or "").strip()
        if not chat_id or not message:
            return error("bad_param", "group 和 message 都必填")
        if not manager.is_active(chat_id):
            return error("not_found",
                         f"这个群没有活跃的 chatagent：{chat_id}"
                         "（没 /activate 过或已休眠）")
        reply = await manager.main_says(chat_id, message)
        return ToolOutput(
            text=reply or "已记下",
            stat=green("已转达"), fields=[("delta", "+1")])

    registry.register(
        "tell_chatagent",
        "发消息给某个群的子 agent，立即触发它一轮。用于传达或布置；"
        "它一般把消息记进记忆，除非你要求它在群里说话。它的回话会带回来。",
        {"type": "object",
         "properties": {
             "group": {"type": "string", "description": "群 id（用 list_groups 拿）"},
             "message": {"type": "string", "description": "要对它说的话"},
         },
         "required": ["group", "message"]},
        tell_chatagent)
