"""modellog：每个会话保留**最新一次**模型请求的上下文（调试用）。

data/modellog/ 下一个会话一份 markdown，每次请求**覆盖**写：

    m_feishu_oc_xxx.md    M 最新一次请求的 messages
    r_feishu_oc_xxx.md    R 最新一次整理请求的 messages

- 只存 messages（请求的 tools/effort 等字段不存），markdown 排版、真换行，直接看；
- 覆盖不累积：请求本来就是增量来的，留最新一份就够 debug（"当时到底发了什么"
  以这一份为准；历史请求在账本里都能重建）；
- 请求**发出前**落盘：400 之类的翻车现场也留得下证据；
- 落盘失败绝不影响请求（内部全吞，只留一行 warning）。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DIR: Optional[Path] = None
_SLUG_RE = re.compile(r"[^\w.-]")


def setup(directory: Optional[Path]) -> None:
    """开启落盘（app 组装时调一次）。传 None / 空路径 = 关闭。"""
    global _DIR
    if not directory:
        _DIR = None
        return
    _DIR = Path(directory)
    _DIR.mkdir(parents=True, exist_ok=True)


def enabled() -> bool:
    return _DIR is not None


def dump(kind: str, session: str, payload: Dict[str, Any]) -> Optional[Path]:
    """把最新一次请求的 messages 覆盖落盘。kind = "m"（M）/ "r"（R）。

    返回落盘路径（未开启/失败为 None）。绝不抛异常。"""
    if _DIR is None:
        return None
    try:
        slug = _SLUG_RE.sub("_", session or "")[:60] or "session"
        path = _DIR / f"{kind}_{slug}.md"
        path.write_text(_render_md(kind, session, payload), encoding="utf-8")
        return path
    except Exception:
        logger.warning("modellog 落盘失败（不影响请求）", exc_info=True)
        return None


# ----------------------------------------------------------------------
# markdown 渲染：一份请求 = 一个标题 + 每条消息一节
# ----------------------------------------------------------------------
def _content_md(content: Any) -> str:
    """消息正文 → markdown 文本。图片 parts 只留占位（base64 别刷屏）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            out.append(str(p))
        elif p.get("type") == "text":
            out.append(str(p.get("text", "")))
        elif p.get("type") == "image_url":
            url = str(((p.get("image_url") or {}).get("url")) or "")
            out.append(f"（图片 {url[:48]}…）" if url else "（图片）")
        else:
            out.append(f"（{p.get('type', '?')}）")
    return "\n".join(x for x in out if x)


def _msg_md(m: Dict[str, Any]) -> str:
    role = str(m.get("role", "?"))
    head = f"## {role}"
    if m.get("tool_call_id"):
        head += f" · {m['tool_call_id']}"
    parts = [head, ""]

    reasoning = m.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        parts.append("> " + reasoning.strip().replace("\n", "\n> "))
        parts.append("")

    body = _content_md(m.get("content"))
    if body:
        parts.append(body)
        parts.append("")

    calls = m.get("tool_calls") or []
    if calls:
        parts.append("**tool_calls**")
        for c in calls:
            fn = (c.get("function") or {}) if isinstance(c, dict) else {}
            parts.append(f"- `{fn.get('name', '?')}({fn.get('arguments', '')})`")
        parts.append("")
    return "\n".join(parts)


def _render_md(kind: str, session: str, payload: Dict[str, Any]) -> str:
    bits = [f"# modellog · {kind} · {session}",
            f"round {payload.get('round', 0)}",
            time.strftime("%Y-%m-%d %H:%M:%S")]
    if payload.get("cycle"):
        bits.insert(2, f"周期 {payload['cycle']}")
    if payload.get("phase"):
        bits.insert(2, str(payload["phase"]))
    sections = [" · ".join(bits), ""]
    for m in payload.get("messages") or []:
        if isinstance(m, dict):
            sections.append(_msg_md(m))
    return "\n".join(sections).rstrip() + "\n"
