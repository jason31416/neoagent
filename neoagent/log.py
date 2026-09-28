"""日志格式：终端彩色、信息密度优先。

- 时间只留 HH:MM（发消息的场景不需要日期）；
- 不打 logger 类路径（模块名噪音），级别用颜色区分；
- 消息内部也着色：通道标签、箭头、引用、工具名、HTTP 噪音、括号注释各有颜色，
  不搞满屏白字。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_LEVEL_COLOR = {
    "DEBUG": "\033[36m",          # cyan
    "INFO": "\033[32m",           # green
    "WARNING": "\033[33m",        # yellow
    "ERROR": "\033[1;31m",        # bold red
    "CRITICAL": "\033[1;35m",     # bold magenta
}

_TAG_RE = re.compile(r"\[([A-Za-z_][\w.]*)\]")     # [weixin] [llm] [turn] …
_QUOTE_RE = re.compile(r"（引用 [^）]*）")
_PAREN_RE = re.compile(r"（[^）]*）")               # 其余括号注释 → 暗
_TOOL_RE = re.compile(r"((?:调用工具|工具) )([A-Za-z_][\w.]*)")


def _paint(msg: str) -> str:
    """消息内重点着色（管道/非 TTY 时调用方应使用纯文本 formatter）。"""
    # 引用先暂存（避免被括号规则二次着色把 ANSI 套乱），最后还原
    quotes: list = []

    def _stash(m: re.Match) -> str:
        quotes.append(f"\033[35m{m.group(0)}{_RESET}")
        return f"\x00{len(quotes) - 1}\x00"

    msg = _QUOTE_RE.sub(_stash, msg)
    msg = _TOOL_RE.sub(lambda m: f"{m.group(1)}{_BOLD}\033[36m{m.group(2)}{_RESET}", msg)  # 工具名 → 亮青
    msg = _TAG_RE.sub(lambda m: f"\033[36m[{m.group(1)}]{_RESET}", msg)          # 通道/组件标签 → 青
    msg = msg.replace(">>>", f"{_BOLD}\033[32m>>>{_RESET}")                       # 箭头 → 亮绿
    msg = msg.replace("HTTP Request:", f"{_DIM}HTTP Request:{_RESET}")           # httpx 噪音 → 暗
    msg = _PAREN_RE.sub(lambda m: f"{_DIM}{m.group(0)}{_RESET}", msg)            # 括号注释 → 暗
    for i, q in enumerate(quotes):
        msg = msg.replace(f"\x00{i}\x00", q)
    return msg


class ColorFormatter(logging.Formatter):
    """HH:MM + 彩色级别 + 消息着色。"""

    def formatTime(self, record, datefmt=None) -> str:
        return time.strftime("%H:%M", time.localtime(record.created))

    def format(self, record) -> str:
        level = record.levelname
        color = _LEVEL_COLOR.get(level, "")
        head = f"{_DIM}{self.formatTime(record)}{_RESET} {color}{_BOLD}{level:<7}{_RESET}"
        msg = _paint(record.getMessage())
        out = f"{head} {msg}"
        if record.exc_info:
            exc = self.formatException(record.exc_info)
            out += "\n" + "\n".join(
                f"{_DIM}\033[31m{line}{_RESET}" for line in exc.splitlines())
        return out


def _use_color(stream) -> bool:
    try:
        tty = stream.isatty()
    except Exception:
        tty = False
    return tty and os.environ.get("NO_COLOR") is None \
        and os.environ.get("TERM", "") != "dumb"


def setup_logging(level: int = logging.INFO) -> None:
    """全局日志配置（run/cli 共用）。管道/非 TTY 自动退化为纯文本。"""
    handler = logging.StreamHandler(sys.stderr)
    if _use_color(sys.stderr):
        handler.setFormatter(ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
