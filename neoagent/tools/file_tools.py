"""文件读写工具：read_file / write_file。

description 的措辞参考了 hermes（告诉模型"用这个代替 cat"这类习惯），
让模型少走弯路。路径访问受 files.allowed_roots 约束（防越界）。
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import ToolOutput, ToolRegistry

DEFAULT_READ_LIMIT = 200  # 单次最多读多少行（IM 场景不需要长输出）
IMAGE_READ_MAX_BYTES = 20 * 1024 * 1024   # read_file 看图的大小上限

# 工作台：相对路径的解析基准（agent 的工作目录，不是项目根）
_workspace: Optional[Path] = None


def set_workspace(path) -> None:
    """设置工作台目录（相对路径以它为基准；绝对路径不受影响）。"""
    global _workspace
    _workspace = Path(path) if path else None


def _resolve_in_roots(path_str: str, allowed_roots: List[str], base: Optional[Path] = None) -> Path:
    """解析路径并校验在允许的根目录内。
    相对路径以工作台（base/_workspace）为基准；统一展开 ~ 并解析符号链接后再比较，
    防止 ../ 和软链越界。不在允许范围内直接抛 PermissionError（转成字符串回给模型）。"""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (base or _workspace or Path.cwd()) / p
    p = p.resolve()
    for root in allowed_roots:
        r = Path(root).expanduser().resolve()
        if p == r or r in p.parents:
            return p
    raise PermissionError(f"路径超出允许范围（allowed_roots={allowed_roots}）：{p}")


async def read_file(args: Dict[str, Any]):
    """read_file 工具实现：文本带行号分页；图片走"工具结果信封"直接看图。"""
    path = _resolve_in_roots(str(args.get("path", "")), _allowed_roots())
    offset = int(args.get("offset", 1))          # 1-based
    limit = min(int(args.get("limit", DEFAULT_READ_LIMIT)), 500)

    if not path.exists():
        return f"文件不存在：{path}"
    if not path.is_file():
        return f"不是普通文件：{path}"

    # 图片 → 工具结果信封（图像内容随工具结果进上下文，主模型亲眼看）。
    # svg 例外：矢量图本质是文本，读源码更有用（也塞不进视觉接口）
    mime = mimetypes.guess_type(str(path))[0] or ""
    if mime.startswith("image/") and mime != "image/svg+xml":
        size = path.stat().st_size
        if size > IMAGE_READ_MAX_BYTES:
            return f"图片太大（{size // 1024}KB，上限 {IMAGE_READ_MAX_BYTES // 1024}KB），看不了"
        return ToolOutput(text=f"（图片 {path.name} 的图像内容如下）", images=[str(path)])

    # 二进制（非图片）明确拒绝，不吐乱码误导模型
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError as e:
        return f"读取失败：{e}"
    if b"\x00" in head:
        return (f"二进制文件读不出文本内容：{path.name}（{mime or '未知类型'}）。"
                "要看图片请确认它是 png/jpg 等图片格式；其他二进制内容无法查看。")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"读取失败：{e}"

    lines = text.splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    numbered = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
    note = ""
    if offset - 1 + limit < len(lines):
        note = f"\n…（共 {len(lines)} 行，继续读用 offset={offset + limit}）"
    return (numbered or "（空文件）") + note


async def write_file(args: Dict[str, Any]) -> str:
    """write_file 工具实现：整文件覆盖写，自动建父目录。"""
    path = _resolve_in_roots(str(args.get("path", "")), _allowed_roots())
    content = args.get("content")
    if content is None:
        return "缺少 content 参数"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")
    return f"已写入 {path}（{len(str(content))} 字符）"


def _allowed_roots() -> List[str]:
    # 延迟拿配置，避免 tools 层和 config 循环依赖（registry 注册处会注入）
    return _ALLOWED_ROOTS


_ALLOWED_ROOTS: List[str] = []


def set_allowed_roots(roots: List[str]) -> None:
    """由启动入口注入 files.allowed_roots 配置。"""
    global _ALLOWED_ROOTS
    _ALLOWED_ROOTS = roots


READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": (
        "读取文件：文本文件带行号分页输出（'行号|内容'，大文件用 offset/limit 分页）；"
        "图片文件（png/jpg 等）会直接把图像内容给你看。用这个代替 cat/head/tail。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
            "offset": {"type": "integer", "description": "起始行号（1 起，默认 1）", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "最多读多少行（默认 200，上限 500）", "default": DEFAULT_READ_LIMIT, "maximum": 500},
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": (
        "写入文本文件（整体覆盖）。用这个代替 echo/heredoc。自动创建父目录。"
        "注意：会覆盖已有文件的全部内容；改局部内容时先 read_file 读出来、改完再整写。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    },
}


def register_file_tools(registry: ToolRegistry, allowed_roots: List[str]) -> None:
    """把文件工具注册进 registry。"""
    set_allowed_roots(allowed_roots)
    registry.register(READ_FILE_SCHEMA["name"], READ_FILE_SCHEMA["description"],
                      READ_FILE_SCHEMA["parameters"], read_file)
    registry.register(WRITE_FILE_SCHEMA["name"], WRITE_FILE_SCHEMA["description"],
                      WRITE_FILE_SCHEMA["parameters"], write_file)
