"""入库定稿（frames）：模型/用户消息的文本在**入库前**定稿成上下文终态。

定稿做三件事（都是一次性固化，之后永不改写——这也是前缀缓存一致性的来源）：

1. **属性净化**：标签属性分两类——模型可用（replyTo/src/name）与系统专属
   （id/time/sender…）。模型自己写的 id/time 一律剥掉不算数，防伪造；
2. **气泡编号**：每遇到一个真气泡（<message>/<img>/<file>）分配一个 msg#N
   写进标签（编号由调用方预分配，连续、持久化）；
3. **recall 快照**：replyTo 指向的气泡**不管在不在上下文**，快照一律固化在
   标签前面（独立 <recall> 标签，不占正文）——被引消息哪怕以后被裁掉，
   引用的字节也不会变。

标签外的文字（模型的碎碎念）原样留存、原样回放；坏标签（没 src 的媒体、
空 <message>）也按碎碎念对待——不编号、不发送、不改样。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Tuple

# 与生成侧同一个语法：<message …>正文</message> 或自闭合媒体 <img|file …/>
_ITEM_RE = re.compile(
    r"<message([^>]*)>(.*?)</message>|<(img|file)\b([^>]*?)/?>", re.S)
_REPLYTO_RE = re.compile(r'replyTo\s*=\s*"msg#(\d+)"')
_SRC_RE = re.compile(r'src\s*=\s*"([^"]*)"')
_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"')


@dataclass
class Bubble:
    """定稿过程顺手抽出的气泡（投递层用）。"""
    bubble_id: int                       # msg#N
    kind: str                            # "text" | "image" | "file"
    content: str = ""                    # 气泡纯文本（发送正文 / recall 快照）
    src: str = ""                        # 媒体路径
    name: str = ""                       # 媒体显示名
    reply_to: Optional[int] = None       # 引用的气泡编号（平台挂真引用用）


def esc_body(text: str) -> str:
    """转义正文里的标签边界字面量，防止把定稿结构打乱。"""
    return (text or "").replace("</message>", "＜/message＞") \
                       .replace("</recall>", "＜/recall＞")


def esc_attr(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def attachment_notes(atts) -> str:
    """附件清单文本（正文后的括号行，入库时固化）。

    图片本体由 content parts 承载，这里只列名字；语音已转写成正文，不列。
    """
    lines = []
    i = 0
    for a in atts:
        if a.kind == "audio":
            continue                      # 语音=转写后的普通文本，不带附件行
        i += 1
        desc = f"{a.kind}，{a.filename}"
        if a.size:
            desc += f"，{a.size // 1024}KB"
        if a.local_path:
            desc += f"，路径 {a.local_path}"
        if a.kind == "image" and not a.data:
            desc += "（图内容未附）"
        lines.append(f"（附件 {i}：{desc}）")
    return ("\n" + "\n".join(lines)) if lines else ""


def recall_tag(bubble_id: Optional[int], sender: str, ts: float, text: str) -> str:
    """引用快照：<recall> 独立标签，贴在被引消息自己标签的前面。"""
    id_attr = f' id="msg#{bubble_id}"' if bubble_id else ""
    return (f'<recall{id_attr} sender="{esc_attr(sender)}"'
            f' time="{fmt_ts(ts)}">{esc_body((text or "").strip())}</recall>')


def finalize_user(text: str, *, sender: str, bubble_id: int, ts: float,
                  recall: str = "") -> str:
    """用户消息定稿：<message sender id time>正文</message>（recall 快照贴前面）。"""
    body = esc_body(text or "")
    return (f'{recall}<message sender="{esc_attr(sender)}" id="msg#{bubble_id}"'
            f' time="{fmt_ts(ts)}">{body}</message>')


def finalize_assistant(content: Optional[str], *, ts: float,
                       alloc: Callable[[], int],
                       lookup: Callable[[int], Optional[object]],
                       ) -> Tuple[str, List[Bubble]]:
    """模型输出定稿 → (定稿文本, 气泡列表)。

    alloc：拿一个新气泡编号；lookup：按气泡编号查历史气泡（验真 replyTo +
    取 recall 快照），查不到的 replyTo 直接剥掉（模型伪造的不算数）。
    """
    src_text = content or ""
    out: List[str] = []
    bubbles: List[Bubble] = []
    pos = 0
    ts_str = fmt_ts(ts)

    def _recall_for(target: Optional[int]) -> str:
        if target is None:
            return ""
        hit = lookup(target)
        return recall_tag(target, hit.sender, hit.timestamp, hit.content) if hit else ""

    for m in _ITEM_RE.finditer(src_text):
        stray = src_text[pos:m.start()]
        if stray:
            out.append(esc_body(stray))          # 碎碎念原样留存
        pos = m.end()

        if m.group(1) is not None:               # ---- <message> ----
            body = m.group(2)
            raw_attrs = m.group(1) or ""
            if not body.strip():
                out.append(m.group(0))            # 空块按碎碎念原样留着，不编号不发送
                continue
            q = _REPLYTO_RE.search(raw_attrs)
            target = int(q.group(1)) if q else None
            if target is not None and lookup(target) is None:
                target = None                     # 伪造/失效引用剥掉
            bid = alloc()
            reply_attr = f' replyTo="msg#{target}"' if target is not None else ""
            out.append(_recall_for(target))
            out.append(f'<message id="msg#{bid}" time="{ts_str}"{reply_attr}>'
                       f'{body}</message>')
            bubbles.append(Bubble(bid, "text", content=body.strip(), reply_to=target))
        else:                                     # ---- <img>/<file> ----
            raw_attrs = m.group(4) or ""
            s = _SRC_RE.search(raw_attrs)
            if not s or not s.group(1).strip():
                out.append(m.group(0))            # 没 src 的坏标签按碎碎念原样留着
                continue
            n = _NAME_RE.search(raw_attrs)
            q = _REPLYTO_RE.search(raw_attrs)
            target = int(q.group(1)) if q else None
            if target is not None and lookup(target) is None:
                target = None
            bid = alloc()
            tag = m.group(3)                      # 标签名原样：img 就是 img，别改样
            kind = "image" if tag == "img" else "file"
            src = s.group(1).strip()
            name = n.group(1).strip() if n else ""
            reply_attr = f' replyTo="msg#{target}"' if target is not None else ""
            name_attr = f' name="{esc_attr(name)}"' if name else ""
            out.append(_recall_for(target))
            out.append(f'<{tag} src="{esc_attr(src)}"{name_attr}{reply_attr}'
                       f' id="msg#{bid}" time="{ts_str}"/>')
            bubbles.append(Bubble(bid, kind, content=name or src, src=src,
                                  name=name, reply_to=target))

    if pos < len(src_text):
        out.append(esc_body(src_text[pos:]))     # 结尾碎碎念
    return "".join(out), bubbles
