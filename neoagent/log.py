"""日志格式：终端彩色、信息密度优先。

- 时间只留 HH:MM（发消息的场景不需要日期）；
- 不打 logger 类路径（模块名噪音），级别用颜色区分；
- 配色原则：色相少、语义固定、同色系明度分层、着色面积克制
  （一色刷屏=单调、规则乱匹配=花，都踩过）：
  · 青 = 谁在动：[…] 组件标签 → 暗青；`<<` 入站箭头、工具名 → 亮青粗；
  · 绿 = 产出：`>>` 出站箭头 → 亮绿粗；统计 `+N 行` → 绿；
  · 黄/红 = 异常：WARNING 黄粗、ERROR 红粗；统计 `-N 行` → 红；
  · 灰 = 细节：时间、工具参数、引用/媒体注释、DEBUG 级别名；
  · 白 = 内容：消息正文、"调用工具/结果"连接词、INFO 级别名（粗）——永不着色。
  统计的 +/- 由工具构造 stat 的地方显式包色（工具自己知道哪段是增删）。
- 着色分两层：formatter 只管**通用 pattern**（级别名、[…] 标签），具体某条
  日志里的重点（箭头、工具名、参数）由**打日志的调用点**用下面的
  gray()/bold_cyan()/green() 等辅助函数显式包着写——不做全文正则匹配
  （误伤不可控，踩过）。
- 辅助函数在非 TTY / NO_COLOR 下原样返回纯文本，管道输出不会夹带 ANSI。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

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
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    """剥掉 ANSI 色码（落文件/转录用纯文本）。"""
    return _ANSI_RE.sub("", str(s))


def _paint(msg: str) -> str:
    """通用 pattern 着色：[…] 组件标签 → 暗青，别的一律不匹配。

    消息内其余颜色由**打日志的调用点**用下面的辅助函数显式包起来；
    调用点着色的内容里不要再写 `[...]` 标签（会被这条通用规则再包一层 ANSI）。
    """
    return _TAG_RE.sub(lambda m: f"\033[36m[{m.group(1)}]{_RESET}", msg)


# ---- 调用点着色辅助（非 TTY / NO_COLOR 下原样返回纯文本，管道输出不夹带 ANSI） ----

_COLOR_ON = False


def _w(code: str, s) -> str:
    return f"\033[{code}m{s}{_RESET}" if _COLOR_ON else str(s)


def gray(s) -> str:        return _w("90", s)      # noqa: E704
def cyan(s) -> str:        return _w("36", s)      # noqa: E704  暗青（标签层）
def green(s) -> str:       return _w("32", s)      # noqa: E704
def red(s) -> str:         return _w("31", s)      # noqa: E704
def yellow(s) -> str:      return _w("33", s)      # noqa: E704
def magenta(s) -> str:     return _w("35", s)      # noqa: E704
def dim(s) -> str:         return _w("2", s)       # noqa: E704
def bold(s) -> str:        return _w("1", s)       # noqa: E704
def bold_cyan(s) -> str:   return _w("1;36", s)    # noqa: E704  亮青（动作锚点）
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


class PlainFormatter(logging.Formatter):
    """文件版：带日期、剥 ANSI（调用点包的颜色不进文件）。"""

    def format(self, record) -> str:
        msg = strip_ansi(record.getMessage())
        out = f"{self.formatTime(record)} {record.levelname:<7} {msg}"
        if record.exc_info:
            out += "\n" + strip_ansi(self.formatException(record.exc_info))
        return out


def _use_color(stream) -> bool:
    try:
        tty = stream.isatty()
    except Exception:
        tty = False
    return tty and os.environ.get("NO_COLOR") is None \
        and os.environ.get("TERM", "") != "dumb"


def setup_logging(level: int = logging.INFO, file_path=None,
                  stderr: bool = True, color_stream=None) -> None:
    """全局日志配置（run/cli 共用）。

    - stderr=False：日志不进终端（控制台形态——日志是日志、控制台是控制台）；
    - file_path：日志双写文件（剥 ANSI、带日期）；
    - color_stream：着色判断看哪个流（控制台形态看 stdout）。
    管道/非 TTY 自动退化为纯文本。
    """
    global _COLOR_ON
    color_stream = color_stream or sys.stderr
    _COLOR_ON = _use_color(color_stream)
    root = logging.getLogger()
    root.handlers[:] = []
    if stderr:
        handler = logging.StreamHandler(sys.stderr)
        if _COLOR_ON:
            handler.setFormatter(ColorFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M"))
        root.addHandler(handler)
    if file_path is not None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.StreamHandler(open(path, "a", encoding="utf-8"))
        fh.setFormatter(PlainFormatter())
        root.addHandler(fh)
    root.setLevel(level)
    # httpx 每个请求打一行 "HTTP Request: …"，纯噪音——压到 WARNING 以下不露面。
    # ⚠️ venv 里装的 httpx 分叉包 logger 叫 "httpx2"，只压 "httpx" 没用（踩过）。
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
