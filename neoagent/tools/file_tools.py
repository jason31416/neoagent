"""文件工具：read_file / write_file / patch_file（+ delete_file / move_file）。

R/M 共用**同一个实现**（以 M 的形态为基准），沙箱按 FileScope 绑定：
- M：roots = files.allowed_roots + 工作台、base = 工作台、可看图；
  记忆区**只读**——read_file 认 memory:// 路径口（如 memory://old/x.md），
  write/patch 碰记忆区（URI 或绝对路径）一律拒（"M 只读记忆"硬线的落点）；
- R：roots = base = memory_dir、不看图（记忆库是文本世界），
  另注册 delete_file/move_file（只给 R——M 的删改走 exec）。

返回统一走 XML 骨架（format.py）：属性放数据、正文放内容，失败 <error kind="…">。
行号列（`N|内容`）是只读标注，写回和 patch 的 old/new 按原文、不带行号。
路径越界防护：展开 ~ 并解析符号链接后再比较（防 ../ 和软链逃逸）。
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..log import green, red
from .format import error, file_attrs, numbered, root
from .registry import ToolOutput, ToolRegistry

DEFAULT_READ_LIMIT = 200  # 单次最多读多少行（IM 场景不需要长输出）
IMAGE_READ_MAX_BYTES = 20 * 1024 * 1024   # read_file 看图的大小上限

MEM_PREFIX = "memory://"   # 记忆区只读口（仅 read_file 认）


@dataclass
class FileScope:
    """一套文件工具的沙箱绑定。roots/base 是读写沙箱与相对路径基准；
    images=False 时 read_file 不看图（R：记忆库只收文本）；
    memory_root 非空时开 memory:// 只读口，且写路径不许碰它（M 的形态）。"""
    roots: List[str] = field(default_factory=list)
    base: Optional[Path] = None
    images: bool = True
    memory_root: Optional[Path] = None
    base_label: str = "工作目录"   # 描述里怎么称呼相对路径基准


def _resolve_in_roots(path_str: str, allowed_roots: List[str],
                      base: Optional[Path] = None) -> Path:
    """解析路径并校验在允许的根目录内。
    相对路径以 base 为基准；统一展开 ~ 并解析符号链接后再比较，
    防止 ../ 和软链越界。不在允许范围内直接抛 PermissionError（转成 <error> 回给模型）。"""
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    p = p.resolve()
    for root_dir in allowed_roots:
        r = Path(root_dir).expanduser().resolve()
        if p == r or r in p.parents:
            return p
    raise PermissionError(f"路径超出允许范围（allowed_roots={allowed_roots}）：{p}")


def _check_text_file(path: Path, display: str) -> Optional[str]:
    """文件形态检查：不存在/目录/非普通文件 → <error> XML；正常返回 None。"""
    if not path.exists():
        return error("not_found", f"文件不存在：{display}")
    if path.is_dir():
        return error("is_dir", f"这是目录：{display}（read_file/patch_file 只处理文件）")
    if not path.is_file():
        return error("bad_param", f"不是普通文件：{display}")
    return None


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


class FileKit:
    """一套文件工具实现；沙箱/看图/记忆口按 scope 绑定。R/M 注册同一份。"""

    def __init__(self, scope: FileScope) -> None:
        self.scope = scope

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def _path(self, args: Dict[str, Any], key: str = "path") -> Tuple[Path, str]:
        """(绝对路径, 回显路径)。回显优先用模型自己传的字面量；
        memory:// 记忆口以记忆库为根解析（read 用；写侧见 _deny_memory）。"""
        raw = str(args.get(key, ""))
        if raw.startswith(MEM_PREFIX):
            if self.scope.memory_root is None:
                raise PermissionError(f"这个工具面不支持 {MEM_PREFIX} 路径：{raw}")
            return (_resolve_in_roots(raw[len(MEM_PREFIX):],
                                      [str(self.scope.memory_root)],
                                      base=self.scope.memory_root), raw)
        return _resolve_in_roots(raw, self.scope.roots, base=self.scope.base), raw

    def _deny_memory(self, path: Path, display: str) -> Optional[str]:
        """记忆区写拒（"M 只读记忆"硬线）：URI 进来的和绝对路径进来的都拦。"""
        mr = self.scope.memory_root
        if mr is None:
            return None
        mr = mr.resolve()
        if path == mr or mr in path.parents:
            return error("denied",
                         f"记忆只读：{display}（记忆由 Recorder 维护，"
                         "要记的内容用 record_memory 提案）")
        return None

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------
    async def read_file(self, args: Dict[str, Any]):
        """文本带行号分页（行号是标注列）；图片直接给图像内容（R 不看图）。"""
        try:
            path, display = self._path(args)
        except PermissionError as e:
            return error("out_of_scope", str(e))
        bad = _check_text_file(path, display)
        if bad:
            return bad

        # 图片 → 工具结果信封（图像内容随工具结果进上下文，主模型亲眼看）。
        # svg 例外：矢量图本质是文本，读源码更有用（也塞不进视觉接口）
        mime = mimetypes.guess_type(str(path))[0] or ""
        if mime.startswith("image/") and mime != "image/svg+xml":
            if not self.scope.images:
                return error("binary", f"这里只读文本，看不了图片：{display}")
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
            return error("binary", f"二进制文件读不出文本内容：{path.name}"
                                   f"（{mime or '未知类型'}）")

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

    # ------------------------------------------------------------------
    # write_file
    # ------------------------------------------------------------------
    async def write_file(self, args: Dict[str, Any]):
        """写文本文件：mode 必填——overwrite 覆盖全文 / new 只许新建。自动建父目录。"""
        try:
            path, display = self._path(args)
        except PermissionError as e:
            return error("out_of_scope", str(e))
        denied = self._deny_memory(path, display)
        if denied:
            return denied
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

    # ------------------------------------------------------------------
    # patch_file
    # ------------------------------------------------------------------
    async def patch_file(self, args: Dict[str, Any]):
        """精确替换文件里的一段文本（old 必须唯一匹配、按原文不带行号）；改一处的首选。"""
        try:
            path, display = self._path(args)
        except PermissionError as e:
            return error("out_of_scope", str(e))
        denied = self._deny_memory(path, display)
        if denied:
            return denied
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

    # ------------------------------------------------------------------
    # delete_file / move_file（只给 R 注册：M 的删改走 exec）
    # ------------------------------------------------------------------
    async def delete_file(self, args: Dict[str, Any]) -> str:
        try:
            path, display = self._path(args)
        except PermissionError as e:
            return error("out_of_scope", str(e))
        denied = self._deny_memory(path, display)
        if denied:
            return denied
        if path.is_dir():
            return error("bad_param", "delete 只删文件；目录请用 move 归档")
        if not path.is_file():
            return error("not_found", f"文件不存在：{display}")
        path.unlink()
        return root("delete_file", {"path": display}, "已删除")

    async def move_file(self, args: Dict[str, Any]) -> str:
        try:
            src, src_display = self._path(args, key="src")
            dst, dst_display = self._path(args, key="dst")
        except PermissionError as e:
            return error("out_of_scope", str(e))
        for p, d in ((src, src_display), (dst, dst_display)):
            denied = self._deny_memory(p, d)
            if denied:
                return denied
        if not src.is_file():
            return error("not_found", f"源文件不存在：{src_display}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return root("move_file", {"src": src_display, "dst": dst_display}, "已移动")

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, registry: ToolRegistry, *, delete_move: bool = False) -> None:
        """把文件工具注册进 registry（同一实现，沙箱按 scope）。
        delete_move：delete_file/move_file 只给 R 注册（M 的删改走 exec）。"""
        s = self.scope
        img_clause = ("图片（png/jpg 等）直接给图像内容" if s.images
                      else "只读文本（图片读不了）")
        mem_read = (f" 记忆文件走 {MEM_PREFIX} 相对路径（如 {MEM_PREFIX}old/x.md，只读）。"
                    if s.memory_root else "")
        mem_write = " 记忆区只读，写不进去。" if s.memory_root else ""
        path_desc = f"文件路径（相对路径以{s.base_label}为基准，或绝对路径）"

        registry.register(
            "read_file",
            f"读取文件：文本带行号分页输出（'行号|内容'，行号是标注，写回/patch 不带行号），"
            f"大文件用 offset/limit 分页；{img_clause}。{mem_read}",
            {"type": "object",
             "properties": {"path": {"type": "string", "description": path_desc},
                            "offset": {"type": "integer",
                                       "description": "起始行号（1 起，默认 1）",
                                       "default": 1, "minimum": 1},
                            "limit": {"type": "integer",
                                      "description": "最多读多少行（默认 200，上限 500）",
                                      "default": DEFAULT_READ_LIMIT, "maximum": 500}},
             "required": ["path"]}, self.read_file)
        registry.register(
            "write_file",
            f"写入文本文件（按 mode 整体处理）。自动创建父目录。只改局部内容用 patch_file。"
            f"{mem_write}",
            {"type": "object",
             "properties": {"path": {"type": "string", "description": path_desc},
                            "content": {"type": "string", "description": "完整文件内容"},
                            "mode": {"type": "string", "enum": ["overwrite", "new"],
                                     "description": "overwrite=覆盖全文（文件不存在则新建）；"
                                                    "new=只许新建，已存在会报错"}},
             "required": ["path", "content", "mode"]}, self.write_file)
        registry.register(
            "patch_file",
            f"精确替换文件中的一段文本：old 按原文匹配（不带行号）且必须只出现一次，"
            f"整体换成 new。改一处内容的首选。{mem_write}",
            {"type": "object",
             "properties": {"path": {"type": "string", "description": path_desc},
                            "old": {"type": "string", "description": "要替换的原文（必须唯一匹配）"},
                            "new": {"type": "string", "description": "替换成的新文本"}},
             "required": ["path", "old", "new"]}, self.patch_file)
        if delete_move:
            registry.register(
                "delete_file", "删除文件（真删；长时间用不到的一般用 move 移进 old/）",
                {"type": "object",
                 "properties": {"path": {"type": "string", "description": path_desc}},
                 "required": ["path"]}, self.delete_file)
            registry.register(
                "move_file", "移动/归档文件（如移进 old/）；src/dst 都在允许范围内",
                {"type": "object",
                 "properties": {"src": {"type": "string", "description": path_desc},
                                "dst": {"type": "string", "description": path_desc}},
                 "required": ["src", "dst"]}, self.move_file)


def register_file_tools(registry: ToolRegistry, scope: FileScope, *,
                        delete_move: bool = False) -> None:
    """把文件工具注册进 registry（同一实现，沙箱按 scope 绑定）。"""
    FileKit(scope).register(registry, delete_move=delete_move)
