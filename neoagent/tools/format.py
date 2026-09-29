"""工具返回的统一 XML 骨架（M/R 共用）。

原则：**属性放数据、正文放内容**。标签只用来分隔真实的数据块（文件文本、匹配块、
stdout/stderr），工具自己的话压成属性或正文一句话——不发明 <hint>/<note> 这类
纯装饰节点。所有失败统一 `<error kind="…">一句话 + 数据块（如有）</error>`。

kind 名单（固定，不随意添）：
    not_found   文件不存在        no_match    patch 找不到 old
    not_unique  patch 匹配多处    exists      new 模式下文件已存在
    is_dir      传的是目录        bad_param   参数不对
    out_of_scope 路径出界         binary      二进制读不了
    too_large   超限              denied      命令被策略拒绝
    timeout     命令超时          exec_failed 命令启动失败

行号列（`N|内容`）是只读标注：写回和 patch 的 old/new 一律按原文、不带行号。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _attrs_str(attrs: Dict[str, Any]) -> str:
    parts = []
    for k, v in attrs.items():
        if v is None:
            continue
        parts.append(f' {k}="{str(v).replace(chr(34), chr(39))}"')
    return "".join(parts)


def root(name: str, attrs: Dict[str, Any], body: str = "") -> str:
    """根标签：属性放数据，正文放内容（没有内容就自闭合）。"""
    a = _attrs_str(attrs)
    return f"<{name}{a}>\n{body}\n</{name}>" if body else f"<{name}{a}/>"


def error(kind: str, message: str, body: str = "") -> str:
    """统一错误形状：kind + 一句话（建议揉在话里）+ 数据块（如有）。"""
    return (f'<error kind="{kind}">{message}\n{body}\n</error>'
            if body else f'<error kind="{kind}">{message}</error>')


def numbered(lines: List[str], start: int) -> str:
    """行号标注列：`N|内容`。只出现在读取/匹配/报错的返回里。"""
    return "\n".join(f"{start + i}|{line}" for i, line in enumerate(lines))


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def mtime_str(path: Path) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def file_attrs(path: Path, display: str, total_lines: Optional[int] = None) -> Dict[str, Any]:
    """文件概况属性：路径、行数、大小、修改时间。"""
    attrs: Dict[str, Any] = {"path": display, "size": human_size(path.stat().st_size),
                             "modified": mtime_str(path)}
    if total_lines is not None:
        attrs["total_lines"] = total_lines
    return attrs
