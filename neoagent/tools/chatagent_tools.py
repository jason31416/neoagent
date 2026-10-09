"""chatagent 的工具：memory（便签）+ tell_neoagent（传话筒）。

就这两个——它不是缩水版 neoagent，是带便签和传话筒的群友：
没有文件、命令、搜索、技能。工具面小是特征不是欠账。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..log import green
from .format import ToolOutput, error
from .registry import ToolRegistry

NOTES_MAX = 8000      # 便签超长提醒线（它是便签不是账本）


def register_chatagent_tools(registry: ToolRegistry, *, memory_file: Path,
                             manager: Any, chat_id: str) -> None:
    memory_file = Path(memory_file)

    async def memory(args: Dict[str, Any]) -> ToolOutput:
        action = str(args.get("action") or "").strip()
        if action == "read":
            try:
                text = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
            except OSError as exc:
                return ToolOutput(text=f"便签读不出来：{exc}",
                                  stat="读取失败", fields=[("err", "memory")])
            return ToolOutput(
                text=text.strip() or "（便签还是空的）",
                stat=green("读便签"), fields=[("delta", "+0")])
        if action == "write":
            content = str(args.get("content") or "")
            try:
                memory_file.parent.mkdir(parents=True, exist_ok=True)
                memory_file.write_text(content, encoding="utf-8")
            except OSError as exc:
                return ToolOutput(text=f"便签写不进去：{exc}",
                                  stat="写入失败", fields=[("err", "memory")])
            note = ""
            if len(content) > NOTES_MAX:
                note = f"\n（便签有点长了（{len(content)} 字），记得修剪——它是便签不是账本）"
            return ToolOutput(
                text=f"便签已更新（{len(content)} 字）{note}",
                stat=green("写便签"), fields=[("delta", "+1")])
        return error("bad_param", "action 要是 read 或 write")

    registry.register(
        "memory",
        "读写自己的记忆文件（你的随身便签，跨压缩记住嘱咐、人和正在聊的事）。"
        "write 是整文件替换，写之前一般先 read 看看现状。",
        {"type": "object",
         "properties": {
             "action": {"type": "string", "enum": ["read", "write"],
                        "description": "read=读便签；write=写便签（整文件替换）"},
             "content": {"type": "string",
                         "description": "action=write 时必填：便签的新内容"},
         },
         "required": ["action"]},
        memory)

    async def tell_neoagent(args: Dict[str, Any]) -> ToolOutput:
        message = str(args.get("message") or "").strip()
        if not message:
            return error("bad_param", "message 必填（要跟 neoagent 说什么）")
        context = str(args.get("context") or "")
        manager.chatagent_says(chat_id, message, context)  # 落账 + 触发主 agent；发完即回
        return ToolOutput(
            text="已转达（发完即回、不等回话；neoagent 有回话会作为它的消息稍后出现）",
            stat=green("已上报"), fields=[("delta", "+1")])

    registry.register(
        "tell_neoagent",
        "发送消息给在私聊中的主 agent，用于在私聊中告知主人或在主人明确允许时"
        "请求主 agent 进行信息的查询或一些其他操作。发完即回、不等回话；"
        "它的回话（如果有）会以 neoagent 的消息另行出现，别干等。",
        {"type": "object",
         "properties": {
             "message": {"type": "string",
                         "description": "要跟 neoagent 说的话，一件事一段，问题直接问"},
             "context": {"type": "string",
                         "description": "聊天记录片段（可选），一行一条 `14:02 张三: 内容`，"
                                        "只挑相关的几行"},
         },
         "required": ["message"]},
        tell_neoagent)
