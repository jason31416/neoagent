"""日志格式：终端彩色、信息密度优先。

- 时间只留 HH:MM（发消息的场景不需要日期）；
- 不打 logger 类路径（模块名噪音），级别用颜色区分；
- 配色克制：颜色只标「状态 / 方向」，其余中性，不搞满屏彩字：
  · 时间、[…] 组件标签、工具参数与结果摘要、引用注释 → 灰；
  · 级别名：INFO 粗白、DEBUG 灰、WARNING 黄、ERROR 红；
  · `<<` 入站 → 亮青、`>>` 出站 → 亮绿（方向锚点）；工具名 → 粗白。
- 着色分两层：formatter 只管**通用 pattern**（级别名、[…] 标签），具体某条
  日志里的重点（箭头、工具名、参数）由**打日志的调用点**用下面的
  gray()/bold()/bold_cyan() 等辅助函数显式包着写——不做全文正则匹配
  （误伤不可控，踩过）。
- 辅助函数在非 TTY / NO_COLOR 下原样返回纯文本，管道输出不会夹带 ANSI。
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
    "DEBUG": "\033[90m",          # gray
    "INFO": "",                   # 粗白（head 统一加粗）
    "WARNING": "\033[33m",        # yellow
    "ERROR": "\033[1;31m",        # bold red
    "CRITICAL": "\033[1;35m",     # bold magenta
}

_TAG_RE = re.compile(r"\[([A-Za-z_][\w.]*)\]")     # [weixin] [M] [turn] …（唯一的通用 pattern）


def _paint(msg: str) -> str:
    """通用 pattern 着色：[…] 组件标签 → 灰，别的一律不匹配。

    消息内其余颜色由**打日志的调用点**用下面的辅助函数显式包起来；
    调用点着色的内容里不要再写 `[...]` 标签（会被这条通用规则再包一层 ANSI）。
    """
    return _TAG_RE.sub(lambda m: f"\033[90m[{m.group(1)}]{_RESET}", msg)


# ---- 调用点着色辅助（非 TTY / NO_COLOR 下原样返回纯文本，管道输出不夹带 ANSI） ----

_COLOR_ON = False


def _w(code: str, s) -> str:
    return f"\033[{code}m{s}{_RESET}" if _COLOR_ON else str(s)


def gray(s) -> str:        return _w("90", s)      # noqa: E704
def cyan(s) -> str:        return _w("36", s)      # noqa: E704
def green(s) -> str:       return _w("32", s)      # noqa: E704
def red(s) -> str:         return _w("31", s)      # noqa: E704
def yellow(s) -> str:      return _w("33", s)      # noqa: E704
def magenta(s) -> str:     return _w("35", s)      # noqa: E704
def dim(s) -> str:         return _w("2", s)       # noqa: E704
def bold(s) -> str:        return _w("1", s)       # noqa: E704
def bold_cyan(s) -> str:   return _w("1;36", s)    # noqa: E704
def bold_green(s) -> str:  return _w("1;32", s)    # noqa: E704


class ColorFormatter(logging.Formatter):
    """HH:MM + 彩色级别 + 消息着色。"""

    def formatTime(self, record, datefmt=None) -> str:
        return time.strftime("%H:%M", time.localtime(record.created))

    def format(self, record) -> str:
        level = record.levelname
        color = _LEVEL_COLOR.get(level, "")
        head = f"\033[90m{self.formatTime(record)}{_RESET} {color}{_BOLD}{level:<7}{_RESET}"
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
    global _COLOR_ON
    handler = logging.StreamHandler(sys.stderr)
    _COLOR_ON = _use_color(sys.stderr)
    if _COLOR_ON:
        handler.setFormatter(ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx 每个请求打一行 "HTTP Request: …"，纯噪音——压到 WARNING 以下不露面。
    # ⚠️ venv 里装的 httpx 分叉包 logger 叫 "httpx2"，只压 "httpx" 没用（踩过）。
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
