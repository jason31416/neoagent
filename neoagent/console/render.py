"""控制台渲染：事件 → 一行文本（朴素风格）。

规则（docs/设计-控制台与配置.md 第三/四节）：
- 形如 `时间 频道 说话人: 正文`，单空格左对齐，不做补空格的列对齐；
- 不用装饰字符：无分割线、无图标、不用 · 分隔、不用 ›；箭头 → 可用；
- 颜色只承担语义（复用 log.py 那套）：频道=青、neoagent=绿、说话人=粗白、
  时间/工具行=灰、正文=白（不着色）、系统=青、异常=黄/红；
- 工具行：`时间 工具名 参数 → 结果摘要`，`+N` 绿 / `-N` 红（与日志同语义）；
- 同一渲染器出两版：转录存纯文本，TTY 客户端要色。
"""

from __future__ import annotations

import re
import time
from typing import Dict, List

from ..log import bold, bold_cyan, bold_green, cyan, gray, green, red

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts or time.time()))


def _paint_stat(digest: str) -> str:
    """结果摘要里的 +N/-N 带色（其余灰）——与日志的增减语义一致。"""
    out = []
    for tok in (digest or "").split(" "):
        if re.match(r"\+\d", tok):
            out.append(green(tok))
        elif re.match(r"-\d", tok):
            out.append(red(tok))
        else:
            out.append(gray(tok))
    return " ".join(out)


def _speaker(speaker: str) -> str:
    if speaker == "neoagent":
        return bold_green(speaker)
    if speaker == "系统":
        return cyan(speaker)
    return bold(speaker)


def render(ev: Dict, color: bool = True) -> str:
    """一行事件文本。color=False 时纯文本（落转录用）。"""
    from .. import log as _log
    prev = _log._COLOR_ON
    if not color:
        _log._COLOR_ON = False
    try:
        kind = ev.get("kind", "")
        ts = gray(_hhmm(float(ev.get("ts") or 0)))
        if kind == "tool":
            name = bold_cyan(ev.get("tool", ""))
            args = gray(ev.get("args", ""))
            digest = _paint_stat(strip_ansi(ev.get("digest", "")))
            mid = f"{name} {args}".strip()
            tail = f" → {digest}" if digest else ""
            return f"{ts} {mid}{tail}"
        if kind == "notice":
            return f"{ts} {gray(ev.get('text', ''))}"
        # msg（对话与指令回执）
        speaker = _speaker(ev.get("speaker", ""))
        body = ev.get("text", "")
        if ev.get("reply"):
            body = gray(f"回复 {ev['reply']}：") + body
        parts = [ts]
        if ev.get("channel"):
            parts.append(cyan(ev["channel"]))
        parts.append(f"{speaker}: {body}")
        return " ".join(parts)
    finally:
        _log._COLOR_ON = prev


def banner(home: str = "~/.neoagent") -> List[str]:
    """attach 首屏：ASCII logo（绿→青逐行渐变）+ 一行版本指引。"""
    art = [
        r"  _   _              _                    _",
        r" | \ | | ___  ___   / \   __ _  ___ _ __ | |_",
        r" |  \| |/ _ \/ _ \ / _ \ / _` |/ _ \ '_ \| __|",
        r" | |\  |  __/ (_) / ___ \ (_| |  __/ | | | |_",
        r" |_| \_|\___|\___/_/   \_\__, |\___|_| |_|\__|",
        r"                         |___/",
    ]
    from .. import log as _log
    out = []
    n = len(art)
    for i, line in enumerate(art):
        if _log._COLOR_ON:
            # 绿 (0,200,80) → 青 (0,190,210)，逐行插值
            t = i / max(1, n - 1)
            r_, g_, b_ = 0, int(200 - 10 * t), int(80 + 130 * t)
            out.append(f"\033[1;38;2;{r_};{g_};{b_}m{line}\033[0m")
        else:
            out.append(line)
    out.append(gray(f"neoagent v{_version()}  配置 {home}  输入 /help 看指令"))
    return out


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("neoagent")
    except Exception:
        return "?"
