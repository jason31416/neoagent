"""记忆工具：R 的工具组 + M 的 record_memory 提案工具。

- R 的工具组 = file_tools 的**同一套文件实现**（沙箱 = memory_dir，"R 只写记忆"
  硬线的落点；相对路径以记忆库为基准）+ delete/move（只给 R——它没法执行命令）
  + set_cut_point（R 专属）；
- M 的 record_memory 只入账（tool_call 行由 agent_loop 落），无逻辑体；
  M 读记忆走 read_file 的 memory:// 只读口（见 file_tools）。

设计见 设计-R记忆海马体.md 第三/八节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .file_tools import FileScope, FileKit
from .format import error, root
from .registry import ToolRegistry

_RECORD_ACK = "已记下（为保证缓存，将在下次整理时修正记忆，近期以对话为准）"


# ----------------------------------------------------------------------
# R 的工具组
# ----------------------------------------------------------------------
def build_memory_registry(ledger, session_key: str, memory_dir: Path) -> ToolRegistry:
    """一次整理跑一套（cut_point 绑定会话）；memory_dir 是沙箱根。"""
    registry = ToolRegistry(part="recorder")   # R 的工具组，日志里标 [recorder]
    FileKit(FileScope(roots=[str(memory_dir)], base=memory_dir, images=False,
                      base_label="记忆库")).register(registry, delete_move=True)

    async def set_cut_point(args: Dict[str, Any]) -> str:
        """声明"msg_seq 之前的上下文可以退役"。单调只进不退（账本层强制）。

        msg_seq 是 <message> 标签上的气泡编号 msg#N；切点落点自动吸附到它所在
        对话段（协议组）的边界——不会把 tool 链从中间截断。"""
        try:
            seq = int(args.get("msg_seq", 0))
        except (TypeError, ValueError):
            return error("bad_param", "msg_seq 必须是消息编号（<message> 标签上的 msg#N）")
        if seq <= 0:
            return error("bad_param", "msg_seq 无效")
        hit = ledger.get_bubble(session_key, seq)
        if hit is None:
            return error("bad_param", f"msg#{seq} 不存在（用 <message> 标签上的编号）")
        _, cur = ledger.get_recorder_state(session_key)
        if hit.seq_id <= cur:
            return error("bad_param", f"切点未变（只进不退）：msg#{seq} 已在切点之前")
        ledger.set_recorder_state(session_key, cut_seq=hit.seq_id)   # 自动吸附组边界
        return root("set_cut_point", {"msg_seq": seq, "cut": hit.seq_id},
                    f"切点已推进：msg#{seq} 保留，之前可退役（落点 seq#{hit.seq_id}）")

    registry.register("set_cut_point",
                      "声明 msg_seq 之前的上下文已无关、可以退役（只进不退；拿不准就先不调）",
                      {"type": "object",
                       "properties": {"msg_seq": {"type": "integer",
                                                  "description": "保留起点（<message> 标签上的 msg#N）：这条之前的不再进上下文"}},
                       "required": ["msg_seq"]}, set_cut_point)
    return registry


# ----------------------------------------------------------------------
# M 的提案工具：record_memory
# ----------------------------------------------------------------------
async def record_memory(args: Dict[str, Any]) -> str:
    """无逻辑体：tool_call 行由 agent_loop 落账，Recorder 下次整理时消化。"""
    if not str(args.get("content", "")).strip():
        return "缺少 content 参数（要记住的内容）"
    return _RECORD_ACK


def register_record_memory(registry: ToolRegistry) -> None:
    """注册进 M 的工具面（提案通道：M 提案、Recorder 定稿）。"""
    registry.register(
        "record_memory",
        "向记忆模块提出要记住的内容。",
        {"type": "object",
         "properties": {"content": {"type": "string",
                                    "description": "要记住的内容（一段话，说清事实即可）"}},
         "required": ["content"]},
        record_memory)


def record_payload(call: dict) -> str:
    """从 tool_call 行的 call 项里取 record_memory 的提案内容（Recorder 视图用）。"""
    try:
        args = json.loads(call.get("arguments") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(args.get("content", ""))
