"""文件工具：read_file / write_file / patch_file。

返回统一走 XML 骨架（format.py）：属性放数据、正文放内容，失败 <error kind="…">。
行号列（`N|内容`）是只读标注，写回和 patch 的 old/new 按原文、不带行号。
路径访问受 files.allowed_roots 约束（防越界）。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..log import green, red
from .format import error, file_attrs, numbered, root
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
    防止 ../ 和软链越界。不在允许范围内直接抛 PermissionError（转成 <error> 回给模型）。"""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (base or _workspace or Path.cwd()) / p
    p = p.resolve()
    for root_dir in allowed_roots:
        r = Path(root_dir).expanduser().resolve()
        if p == r or r in p.parents:
            return p
    raise PermissionError(f"路径超出允许范围（allowed_roots={allowed_roots}）：{p}")


def _display(args: Dict[str, Any], path: Path) -> str:
    """回显给模型的路径：优先它自己传的那个。"""
    return str(args.get("path", "")) or str(path)


def _check_text_file(path: Path, display: str) -> Optional[str]:
    """文件形态检查：不存在/目录/非普通文件 → <error> XML；正常返回 None。"""
    if not path.exists():
        return error("not_found", f"文件不存在：{display}")
    if path.is_dir():
        return error("is_dir", f"这是目录：{display}（read_file/patch_file 只处理文件）")
    if not path.is_file():
        return error("bad_param", f"不是普通文件：{display}")
    return None


# ----------------------------------------------------------------------
# read_file
# ----------------------------------------------------------------------
async def read_file(args: Dict[str, Any]):
    """文本带行号分页（行号是标注列）；图片直接给图像内容。"""
    try:
        path = _resolve_in_roots(str(args.get("path", "")), _allowed_roots())
    except PermissionError as e:
        return error("out_of_scope", str(e))
    display = _display(args, path)
    bad = _check_text_file(path, display)
    if bad:
        return bad

    # 图片 → 工具结果信封（图像内容随工具结果进上下文，主模型亲眼看）。
    # svg 例外：矢量图本质是文本，读源码更有用（也塞不进视觉接口）
    mime = mimetypes.guess_type(str(path))[0] or ""
    if mime.startswith("image/") and mime != "image/svg+xml":
        size = path.stat().st_size
        if size > IMAGE_READ_MAX_BYTES:
            return error("too_large", f"图片太大（{size // 1024}KB，"
                                      f"上限 {IMAGE_READ_MAX_BYTES // 1024}KB）")
        return ToolOutput(text=root("read_file", {"path": display, "kind": "image"},
                                    f"（图片 {path.name} 的图像内容如下）"),
                          images=[str(path)])

    # 二进制（非图片）明确拒绝，不吐乱码误导模型
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError as e:
        return error("bad_param", f"读取失败：{e}")
    if b"\x00" in head:
        return error("binary", f"二进制文件读不出文本内容：{path.name}（{mime or '未知类型'}）")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return error("bad_param", f"读取失败：{e}")

    offset = int(args.get("offset", 1))          # 1-based
    limit = min(int(args.get("limit", DEFAULT_READ_LIMIT)), 500)
    lines = text.splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    if chunk:
        body = numbered(chunk, offset)
        shown: Optional[str] = f"{offset}-{offset + len(chunk) - 1}"
    elif not lines:
        body, shown = "（空文件）", None
    else:
        body, shown = f"（offset={offset} 超出范围，共 {len(lines)} 行）", None
    attrs = file_attrs(path, display, total_lines=len(lines))
    attrs["shown_lines"] = shown
    return ToolOutput(text=root("read_file", attrs, body), stat=f"读 {len(chunk)} 行")


# ----------------------------------------------------------------------
# write_file
# ----------------------------------------------------------------------
async def write_file(args: Dict[str, Any]):
    """写文本文件：mode 必填——overwrite 覆盖全文 / new 只许新建。自动建父目录。"""
    try:
        path = _resolve_in_roots(str(args.get("path", "")), _allowed_roots())
    except PermissionError as e:
        return error("out_of_scope", str(e))
    display = _display(args, path)
    mode = args.get("mode")
    if mode not in ("overwrite", "new"):
        return error("bad_param", 'mode 必填：overwrite（覆盖全文）或 new（只许新建）')
    content = args.get("content")
    if content is None:
        return error("bad_param", "缺少 content 参数")
    if path.is_dir():
        return error("is_dir", f"这是目录：{display}")

    old_lines = 0
    if path.is_file():
        if mode == "new":
            return error("exists", f'文件已存在：{display}（要覆盖用 mode="overwrite"）')
        try:
            old_lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            old_lines = 0

    text = str(content)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        return error("bad_param", f"写入失败：{e}")
    new_lines = len(text.splitlines()) or 1
    attrs = file_attrs(path, display)
    attrs.update({"mode": mode, "new_lines": new_lines})
    if mode == "overwrite":
        attrs["old_lines"] = old_lines
    return ToolOutput(text=root("write_file", attrs, "已写入"),
                      stat=f"{green('+' + str(new_lines))} 行")


# ----------------------------------------------------------------------
# patch_file（M 用；R 的记忆 patch 也走 apply_patch 这套）
# ----------------------------------------------------------------------
def _match_block(text: str, pos: int, length: int, display: str) -> str:
    """一处匹配的完整块：起止行号 + 整块原文带行号（old 多行时块就是多行）。"""
    lines = text.splitlines()
    start = text.count("\n", 0, pos) + 1
    end = start + text[pos:pos + length].count("\n")
    return root("match", {"file": display, "lines": f"{start}-{end}"},
                numbered(lines[start - 1:end], start))


def apply_patch(text: str, old: str, new: str, display: str) -> Tuple[str, str, Dict[str, Any]]:
    """按原文精确替换一处（old 必须唯一）。返回 (新全文, 错误XML, 变更信息)。
    错误非空时新全文为空串；变更信息含 lines/added/removed。"""
    old_s, new_s = str(old), str(new)
    if not old_s:
        return "", error("bad_param", "old 不能为空"), {}
    positions = []
    i = text.find(old_s)
    while i != -1:
        positions.append(i)
        i = text.find(old_s, i + max(len(old_s), 1))
    if not positions:
        return "", error("no_match", "old 在文件里没有匹配（按原文匹配、不带行号；"
                                     "先 read_file 核对原文）"), {}
    if len(positions) > 1:
        blocks = "\n".join(_match_block(text, p, len(old_s), display) for p in positions)
        return "", error("not_unique",
                         f"old 匹配到 {len(positions)} 处，把 old 加上前后文让它唯一"
                         "（匹配原文、不带行号）", blocks), {}
    pos = positions[0]
    start = text.count("\n", 0, pos) + 1
    removed = old_s.count("\n") + 1
    added = new_s.count("\n") + 1
    info = {"lines": f"{start}-{start + removed - 1}", "added": added, "removed": removed}
    return text[:pos] + new_s + text[pos + len(old_s):], "", info


async def patch_file(args: Dict[str, Any]):
    """精确替换文件里的一段文本（old 必须唯一匹配、按原文不带行号）；改一处的首选。"""
    try:
        path = _resolve_in_roots(str(args.get("path", "")), _allowed_roots())
    except PermissionError as e:
        return error("out_of_scope", str(e))
    display = _display(args, path)
    bad = _check_text_file(path, display)
    if bad:
        return bad
    old, new = args.get("old"), args.get("new")
    if old is None or new is None:
        return error("bad_param", "缺少 old / new 参数")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return error("bad_param", f"读取失败：{e}")
    new_text, err, info = apply_patch(text, old, new, display)
    if err:
        return err
    path.write_text(new_text, encoding="utf-8")
    return ToolOutput(text=root("patch_file", dict({"path": display}, **info), "已修改"),
                      stat=f"{green('+' + str(info['added']))} "
                           f"{red('-' + str(info['removed']))} 行")


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
        "读取文件：文本带行号分页输出（'行号|内容'，行号是标注，写回/patch 不带行号），"
        "大文件用 offset/limit 分页；图片（png/jpg 等）直接给图像内容。"
        "用这个代替 cat/head/tail。"
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
        "写入文本文件（按 mode 整体处理）。自动创建父目录。用这个代替 echo/heredoc。"
        "只改局部内容用 patch_file。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
            "content": {"type": "string", "description": "完整文件内容"},
            "mode": {"type": "string", "enum": ["overwrite", "new"],
                     "description": "overwrite=覆盖全文（文件不存在则新建）；new=只许新建，已存在会报错"},
        },
        "required": ["path", "content", "mode"],
    },
}

PATCH_FILE_SCHEMA = {
    "name": "patch_file",
    "description": (
        "精确替换文件里的一段文本：old 按原文匹配（不带行号）且必须只出现一次，"
        "整体换成 new。改一处内容的首选。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
            "old": {"type": "string", "description": "要替换的原文（必须唯一匹配）"},
            "new": {"type": "string", "description": "替换成的新文本"},
        },
        "required": ["path", "old", "new"],
    },
}


def register_file_tools(registry: ToolRegistry, allowed_roots: List[str]) -> None:
    """把文件工具注册进 registry。"""
    set_allowed_roots(allowed_roots)
    registry.register(READ_FILE_SCHEMA["name"], READ_FILE_SCHEMA["description"],
                      READ_FILE_SCHEMA["parameters"], read_file)
    registry.register(WRITE_FILE_SCHEMA["name"], WRITE_FILE_SCHEMA["description"],
                      WRITE_FILE_SCHEMA["parameters"], write_file)
    registry.register(PATCH_FILE_SCHEMA["name"], PATCH_FILE_SCHEMA["description"],
                      PATCH_FILE_SCHEMA["parameters"], patch_file)
