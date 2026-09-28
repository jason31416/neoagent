"""M 的上下文渲染：消息库（事实层）→ OpenAI messages（渲染层）。

产品文档 M 的上下文四段式：
  【它是谁】（system）+【neoagent 的记忆（R 维护）】
  +【时间线】（内部消息 / 对话 / 工具流水按时间整合）

消息协议（渲染格式，与模型输出格式同构）：
  <message sender="jason31416" id="msg#12" time="2026-09-28 15:18" replyTo="msg#10">正文</message>
- 元数据全在标签属性里——模型模仿格式也抄不脏正文（输出解析时属性一律丢弃）；
- 引用 = replyTo 属性指针（目标在窗口内纯指针；出窗口才补内容摘要）；
- tool_call / tool_result 行渲染成协议机器件（assistant(tool_calls) / role:"tool"）。

本期变体：R/E/cron/jev 均未实现——记忆段是记忆文件全文（M 自己编辑），
内部消息是 type="internal" 的注入行。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from .events import Attachment, ReplyRef

# 单条引用内联摘要的上限（防超长引用刷屏）
_QUOTE_TEXT_MAX = 200

_TYPE_ROLE = {"user": "user", "assistant": "assistant", "internal": "system"}


# 引用召回上限（防恶意引用链把上下文撑爆）
_RECALL_CAP = 32


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _esc_attr(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _esc_body(text: str) -> str:
    """转义正文里的 </message> 字面量，防止把标签结构打乱。"""
    return (text or "").replace("</message>", "＜/message＞")


def _recall_quotes(ledger, session_key: str, entries: list) -> list:
    """把被引用但不在窗口里的消息**按时间顺序召回进上下文**。

    - 只召回库里有的行（按 seq）；召回的消息若又引用了更早的消息，递归带上
      （去重防环），指针链不悬空；
    - 上限 _RECALL_CAP 条，超限的退化为正文补丁（见 _quote_parts）。
    """
    have = {e.seq_id for e in entries}
    pending = {e.reply_to.seq_id for e in entries
               if e.reply_to and e.reply_to.seq_id and e.reply_to.seq_id not in have}
    recalled = []
    while pending and len(recalled) < _RECALL_CAP:
        batch = sorted(pending)[:_RECALL_CAP - len(recalled)]
        pending -= set(batch)
        for r in ledger.by_seqs(session_key, batch):
            if r.seq_id in have:
                continue
            have.add(r.seq_id)
            recalled.append(r)
            if r.reply_to and r.reply_to.seq_id and r.reply_to.seq_id not in have:
                pending.add(r.reply_to.seq_id)
    return recalled


def _quote_parts(ref: Optional[ReplyRef], visible_seqs: set) -> Tuple[str, str]:
    """引用渲染：返回 (replyTo 属性值, 正文补丁)。

    - 被引用消息在上下文里（含召回）→ 纯指针 replyTo="msg#N"，模型自己往上翻；
    - 库里没有 / 召回超限 → 退化为正文补丁（罕见路径）。
    """
    if ref is None:
        return "", ""

    if ref.seq_id:
        attr = f"msg#{ref.seq_id}"
        if ref.seq_id in visible_seqs:
            return attr, ""
        text = (ref.text or "").strip()
        if text:
            if len(text) > _QUOTE_TEXT_MAX:
                text = text[:_QUOTE_TEXT_MAX] + "…"
            return attr, f"（#{ref.seq_id} 已不可见：“{text}”）"
        return attr, f"（#{ref.seq_id} 已不可见，内容拿不到）"

    # 从未入库（bot 加入前的消息等）：给不了指针，能补多少补多少
    ts = _fmt_ts(ref.timestamp) if ref.timestamp else ""
    meta = "，".join(x for x in (ts, ref.sender_name, "较早的消息") if x)
    text = (ref.text or "").strip()
    if text:
        if len(text) > _QUOTE_TEXT_MAX:
            text = text[:_QUOTE_TEXT_MAX] + "…"
        return "", f"（引用了{meta}：“{text}”）"
    return "", f"（引用了{meta}，内容已不可见）"


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


def _message_tag(*, sender: str, seq_id: int, ts: str, body: str,
                 reply_attr: str = "", att: Optional[Attachment] = None) -> str:
    """一条 user/assistant/internal 行 → <message …>正文</message>。"""
    attrs = [f'sender="{_esc_attr(sender)}"', f'id="msg#{seq_id}"', f'time="{ts}"']
    if reply_attr:
        attrs.append(f'replyTo="{_esc_attr(reply_attr)}"')
    if att is not None:
        attrs.append(f'kind="{_esc_attr(att.kind)}"')
        if att.filename:
            attrs.append(f'file="{_esc_attr(att.filename)}"')
    return f"<message {' '.join(attrs)}>{_esc_body(body)}</message>"


def render_messages(ledger, session_key: str, *, identity: str, memory_text: str,
                    window: int, vision=None) -> List[dict]:
    """把消息库渲染成 OpenAI messages。每次 LLM 调用现渲染，不落状态。

    identity    【它是谁】——system 主体
    memory_text 【记忆】——记忆文件全文（R 维护，本期 M 自己编辑）
    vision      VisionInput 实例（None = 不带图）
    """
    # ── 段 1+2：system = 它是谁 + 记忆 ──
    system = identity
    if memory_text.strip():
        system += f"\n\n【neoagent 的记忆】\n{memory_text.strip()}"

    msgs: List[dict] = [{"role": "system", "content": system}]

    entries = ledger.visible(session_key, window)
    # 被引用但滚出窗口的消息：按 seq 召回、按时间归位（seq 排序 = 时间顺序）
    entries = sorted(entries + _recall_quotes(ledger, session_key, entries),
                     key=lambda e: e.seq_id)
    visible_seqs = {e.seq_id for e in entries}

    for e in entries:
        # ── 工具流水：协议机器件（call_id 用库存的，配对关系原样还原）──
        if e.type == "tool_call":
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": c.call_id, "type": "function",
                     "function": {"name": c.name, "arguments": c.arguments}}
                    for c in e.calls
                ],
            })
            continue
        if e.type == "tool_result":
            msgs.append({"role": "tool", "tool_call_id": e.call_id, "content": e.text})
            continue

        # ── 段 3/4：内部消息与对话，统一 <message> 语法 ──
        ts = _fmt_ts(e.timestamp)
        reply_attr, quote_patch = _quote_parts(e.reply_to, visible_seqs)
        body = f"{quote_patch}{e.text}{render_attachments(e.attachments)}".strip()
        att = e.attachments[0] if e.attachments else None
        sender = "internal" if e.type == "internal" else e.sender_name
        text = _message_tag(sender=sender, seq_id=e.seq_id, ts=ts, body=body,
                            reply_attr=reply_attr, att=att)

        # 图片以 content parts 附上（历史里的也带——DeepSeek file_id 很便宜）
        images = [a for a in e.attachments
                  if a.kind == "image" and a.provider_ref is not None and vision is not None]
        content: object = text
        if images:
            content = [{"type": "text", "text": text}] + [
                vision.content_part(a.provider_ref) for a in images
            ]

        msgs.append({"role": _TYPE_ROLE.get(e.type, "user"), "content": content})

    return msgs
