"""记忆工具（R / Recorder 专用）+ M 的 record_memory 提案工具。

- R 的工具组（read_file/write_file/patch_file/delete_file/move_file/set_cut_point）
  注册在**独立的 ToolRegistry** 上，路径沙箱 = memory_dir（"R 只写记忆"硬线的落点）；
- M 的 record_memory 只入账（tool_call 行由 agent_loop 落），无逻辑体。

设计见 设计-R记忆海马体.md 第三/八节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .file_tools import _resolve_in_roots
from .registry import ToolOutput, ToolRegistry

_RECORD_ACK = "已记下（将在下次整理时入档，近期以对话为准）"


# ----------------------------------------------------------------------
# R 的工具组
# ----------------------------------------------------------------------
def build_memory_registry(ledger, session_key: str, memory_dir: Path) -> ToolRegistry:
    """一次整理跑一套（cut_point 绑定会话）；memory_dir 是沙箱根。"""
    roots = [str(memory_dir)]

    def _p(path_str: str) -> Path:
        # 相对路径以 memory_dir 为基准（R 说 memory.md / old/x.md 就是这里）
        return _resolve_in_roots(path_str, roots, base=memory_dir)

    async def read_file(args: Dict[str, Any]):
        try:
            path = _p(str(args.get("path", "")))
        except PermissionError as e:
            return str(e)
        if not path.is_file():
            return f"文件不存在：{args.get('path')}"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"读取失败：{e}"
        return ToolOutput(text=text, stat=f"读 {len(text.splitlines())} 行")

    async def write_file(args: Dict[str, Any]):
        try:
            path = _p(str(args.get("path", "")))
        except PermissionError as e:
            return str(e)
        content = args.get("content")
        if content is None:
            return "缺少 content 参数"
        text = str(content)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            return f"写入失败：{e}"
        return ToolOutput(text=f"已写入 {path.name}（{len(text)} 字符）",
                          stat=f"+{len(text.splitlines()) or 1} 行")

    async def patch_file(args: Dict[str, Any]):
        try:
            path = _p(str(args.get("path", "")))
        except PermissionError as e:
            return str(e)
        old, new = args.get("old"), args.get("new")
        if old is None or new is None:
            return "缺少 old / new 参数"
        if not path.is_file():
            return f"文件不存在：{args.get('path')}"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return f"读取失败：{e}"
        n = text.count(str(old))
        if n == 0:
            return "old 在文件里没有匹配（先 read_file 核对原文）"
        if n > 1:
            return f"old 匹配到 {n} 处，不唯一（请扩大 old 的范围使其唯一）"
        path.write_text(text.replace(str(old), str(new), 1), encoding="utf-8")
        return ToolOutput(text=f"已更新 {path.name}",
                          stat=f"+{len(str(new).splitlines())} -{len(str(old).splitlines())} 行")

    async def delete_file(args: Dict[str, Any]) -> str:
        try:
            path = _p(str(args.get("path", "")))
        except PermissionError as e:
            return str(e)
        if path.is_dir():
            return "delete 只删文件；目录请用 move 归档"
        if not path.is_file():
            return f"文件不存在：{args.get('path')}"
        path.unlink()
        return f"已删除 {path.name}"

    async def move_file(args: Dict[str, Any]) -> str:
        try:
            src = _p(str(args.get("src", "")))
            dst = _p(str(args.get("dst", "")))
        except PermissionError as e:
            return str(e)
        if not src.is_file():
            return f"源文件不存在：{args.get('src')}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"已移动 {src.name} → {dst.relative_to(memory_dir) if dst.is_relative_to(memory_dir) else dst}"

    async def set_cut_point(args: Dict[str, Any]) -> str:
        """声明"msg_seq 之前的上下文可以退役"。单调只进不退（账本层强制）。"""
        try:
            seq = int(args.get("msg_seq", 0))
        except (TypeError, ValueError):
            return "msg_seq 必须是消息序号（数字）"
        if seq <= 0:
            return "msg_seq 无效"
        _, cur = ledger.get_recorder_state(session_key)
        if seq <= cur:
            return f"切点未变（只进不退）：当前 msg#{cur}"
        ledger.set_recorder_state(session_key, cut_seq=seq)
        return f"切点已推进到 msg#{seq}"

    registry = ToolRegistry(part="recorder")   # R 的工具组，日志里标 [recorder]
    registry.register("read_file", "读取记忆文件全文（相对路径以记忆库为基准）",
                      {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "文件路径（如 memory.md、old/x.md）"}},
                       "required": ["path"]}, read_file)
    registry.register("write_file", "整文件写入/新建记忆文件（会覆盖原内容）",
                      {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string", "description": "完整文件内容"}},
                       "required": ["path", "content"]}, write_file)
    registry.register("patch_file", "精确替换文件中的一段文本（old 必须唯一匹配；改条目的首选）",
                      {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "old": {"type": "string", "description": "原文（须唯一）"},
                                      "new": {"type": "string", "description": "替换后"}},
                       "required": ["path", "old", "new"]}, patch_file)
    registry.register("delete_file", "删除文件（真删；长时间用不到的一般用 move 移进 old/）",
                      {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}, delete_file)
    registry.register("move_file", "移动/归档文件（如移进 old/）；src/dst 都在记忆库内",
                      {"type": "object",
                       "properties": {"src": {"type": "string"},
                                      "dst": {"type": "string"}},
                       "required": ["src", "dst"]}, move_file)
    registry.register("set_cut_point",
                      "声明 msg_seq 之前的上下文已无关、可以退役（只进不退；拿不准就先不调）",
                      {"type": "object",
                       "properties": {"msg_seq": {"type": "integer",
                                                  "description": "保留起点：这条之前的不再进上下文"}},
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
        "向记忆整理模块提案要记住的内容（事实、约定、结论）。异步生效：内容先入账，"
        "对话停顿时由记忆整理模块写入长期记忆。适合'用户让我记住某事'或你确认值得长存的事实。",
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
