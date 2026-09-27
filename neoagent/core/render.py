"""M 的上下文渲染：消息库（事实层）→ OpenAI messages（渲染层）。

产品文档 M 的上下文四段式：
  【它是谁】（system）+【neoagent 的记忆（R 维护）】
  +（【E 的输出/cron/jev 的提醒/其他内部消息】+【最近 M/用户的对话】按时间整合）

本期变体：R/E/cron/jev 均未实现——记忆段是记忆文件全文（M 自己编辑），
内部消息段是 role="internal" 的注入点。每条消息带时间戳。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from .events import Attachment, ReplyRef

# 单条引用内联文本的上限（防超长引用刷屏）
_QUOTE_TEXT_MAX = 200


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def render_quote(ref: Optional[ReplyRef], visible_roles: Dict[int, str]) -> str:
    """引用行渲染（两档，渲染那一刻现算，指针永不悬空）：
    - 被引用消息在窗口内 → 指针（用户消息带 #N 编号；M 的回复用时间锚）
    - 不在窗口 → 内联文本（模型看不到才补内容）
    """
    if ref is None:
        return ""

    # 锚点：所有时间线消息都有编号，#N 对任何目标都成立
    ts = _fmt_ts(ref.timestamp) if ref.timestamp else ""
    if ref.seq_id:
        anchor = f"#{ref.seq_id}"
        meta = "，".join(x for x in (anchor, ts, ref.sender_name) if x)
    else:
        # 从未入库（bot 加入前的消息等）：锚点能给多少给多少
        meta = "，".join(x for x in (ts, ref.sender_name, "较早的消息") if x)

    if ref.seq_id and ref.seq_id in visible_roles:
        return f"（引用 {meta}）"
    text = (ref.text or "").strip()
    if text:
        if len(text) > _QUOTE_TEXT_MAX:
            text = text[:_QUOTE_TEXT_MAX] + "…"
        return f"（引用 {meta}：{text}）"
    return f"（引用 {meta}，内容已不可见）"


def render_attachments(atts: List[Attachment]) -> str:
    """附件清单文本（正文后的括号行）。

    - 图片本体由 content parts 承载，这里只列名字；
    - 语音已转写成正文（普通文本消息），**不列附件行**（音频只是转写原料）；
    - 引用占位的附件（local_path 空）也列出元数据。
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
        if a.kind == "image" and not a.provider_ref:
            desc += "（图内容不可用）"
        lines.append(f"（附件 {i}：{desc}）")
    return ("\n" + "\n".join(lines)) if lines else ""


def render_messages(ledger, session_key: str, *, identity: str, memory_text: str,
                    window: int, vision=None) -> List[Dict]:
    """把消息库渲染成 OpenAI messages。每次 LLM 调用现渲染，不落状态。

    identity    【它是谁】——system 主体
    memory_text 【记忆】——记忆文件全文（R 维护，本期 M 自己编辑）
    vision      VisionInput 实例（None = 不带图）
    """
    # ── 段 1+2：system = 它是谁 + 记忆 ──
    system = identity
    if memory_text.strip():
        system += f"\n\n【neoagent 的记忆】\n{memory_text.strip()}"

    msgs: List[Dict] = [{"role": "system", "content": system}]

    entries = ledger.visible(session_key, window)
    visible_roles = {e.seq_id: e.role for e in entries}

    for e in entries:
        ts = _fmt_ts(e.timestamp)

        # ── 段 3：内部消息（E 的输出 / cron / jev）按时间混入时间线 ──
        if e.role == "internal":
            msgs.append({
                "role": "system",
                "content": f"[msg#{e.seq_id} | {ts} | 内部消息] {e.text}",
            })
            continue

        # ── 段 4：最近 M/用户的对话（每条带时间戳 + #N 编号，
        #    所有消息都编号——引用可能指向 M 自己的回复）──
        quote = render_quote(e.reply_to, visible_roles)
        body = f"{e.text}{render_attachments(e.attachments)}"
        head = f"[msg#{e.seq_id} | {ts} | {e.sender_name}]"
        text = f"{head} {quote}{body}" if quote else f"{head} {body}"

        # 图片以 content parts 附上（历史里的也带——DeepSeek file_id 很便宜）
        images = [a for a in e.attachments
                  if a.kind == "image" and a.provider_ref is not None and vision is not None]
        content: object = text
        if images:
            content = [{"type": "text", "text": text}] + [
                vision.content_part(a.provider_ref) for a in images
            ]

        if e.role == "assistant" and e.tool_trace:
            # 工具轨迹展开成协议机器件：assistant(tool_calls) → tool 结果 → 最终回复
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{e.seq_id}_{i}",
                        "type": "function",
                        "function": {"name": t.name, "arguments": t.arguments},
                    }
                    for i, t in enumerate(e.tool_trace)
                ],
            })
            for i, t in enumerate(e.tool_trace):
                msgs.append({
                    "role": "tool",
                    "tool_call_id": f"call_{e.seq_id}_{i}",
                    "content": t.result,
                })
            msgs.append({"role": "assistant", "content": content})
        else:
            role = "assistant" if e.role == "assistant" else "user"
            msgs.append({"role": role, "content": content})

    return msgs
