"""M 的上下文渲染：消息库（事实层）→ OpenAI messages（渲染层）。

产品文档 M 的上下文四段式：
  【它是谁】（system）+【neoagent 的记忆（R 维护）】
  +【时间线】（内部消息 / 对话 / 工具流水按时间整合）

消息协议（渲染格式，与模型输出格式同构）：
  <message sender="jason31416" id="msg#12" time="2026-09-28 15:18" replyTo="msg#10">正文</message>
- 元数据全在标签属性里——模型模仿格式也抄不脏正文（输出解析时属性一律丢弃）；
- 引用 = replyTo 属性指针（指针永不悬空：目标不在上下文就召回，见下）；
- tool_call / tool_result 行渲染成协议机器件（assistant(tool_calls) / role:"tool"）。

token 预算与抛弃（替代旧的"最新 N 行"窗口）：
- 时间线按**块**计价：tool_call 行 + 其全部 tool_result 行 = 一个原子块（协议配对
  不可拆），对话行各自成块；从最老端成组抛弃直到装进预算（预算 90% 实装，留估算余量）；
- **引用召回的块不计预算**——被保留行引用的旧消息无条件回补（上限 32 行），
  指针完整性优先于预算；
- **增量缓存**（TimelineCache）：账本只追加、行写入后不变，渲染过的块不重复
  渲染/计价——大块（超长工具结果等）只在入库那一轮算一次，之后每轮 O(新增行)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .events import Attachment, ReplyRef
from .ledger import (TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_CALL, TYPE_TOOL_RESULT,
                     TYPE_USER)

# 单条引用内联摘要的上限（防超长引用刷屏）
_QUOTE_TEXT_MAX = 200
# 预算安全系数：估算器宁高勿低，但实装再留 10% 余量防误差
_BUDGET_SAFETY = 0.9
# 冷启动/增量翻批取行的批大小
_SCAN_BATCH = 500
# 引用召回上限（防恶意引用链撑爆上下文）
_RECALL_CAP = 32

_TYPE_ROLE = {TYPE_USER: "user", TYPE_ASSISTANT: "assistant", TYPE_INTERNAL: "system"}


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _esc_attr(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _esc_body(text: str) -> str:
    """转义正文里的 </message> 字面量，防止把标签结构打乱。"""
    return (text or "").replace("</message>", "＜/message＞")


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
            return attr, f"（#{ref.seq_id} 已不可见：“{_esc_body(text)}”）"
        return attr, f"（#{ref.seq_id} 已不可见，内容拿不到）"

    # 从未入库（bot 加入前的消息等）：给不了指针，能补多少补多少
    ts = _fmt_ts(ref.timestamp) if ref.timestamp else ""
    meta = "，".join(x for x in (ts, ref.sender_name, "较早的消息") if x)
    text = (ref.text or "").strip()
    if text:
        if len(text) > _QUOTE_TEXT_MAX:
            text = text[:_QUOTE_TEXT_MAX] + "…"
        return "", f"（引用了{meta}：“{_esc_body(text)}”）"
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
    return f"<message {' '.join(attrs)}>{body}</message>"


def _truncate_text(text: str, max_tokens: int, counter) -> str:
    """超预算正文截断：保头 60% + 保尾 40%（按字符近似 token，宁紧勿松）。"""
    if counter.count(text) <= max_tokens:
        return text
    budget_chars = max(max_tokens, 32)      # 启发式下 1 字符 ≤ 1 token，取整防死循环
    if len(text) <= budget_chars:
        return text
    head = int(budget_chars * 0.6)
    tail = budget_chars - head
    return text[:head] + "\n…（超长截断）…\n" + (text[-tail:] if tail else "")


# ----------------------------------------------------------------------
# 块：裁剪的最小单位
# ----------------------------------------------------------------------
class _Row:
    """一行（对话/内部）的缓存渲染件。body 在建块时渲染一次，引用属性组装时现拼。
    代价按**完整标签文本**计（含 sender/id/time 属性的框架开销）。
    reasoning 是轮首行的思考草稿——回传进协议消息（实测不计价），不给用户看。"""

    __slots__ = ("seq_id", "role", "sender", "ts", "reply_to", "body", "att", "parts",
                 "reasoning", "cost")

    def __init__(self, entry, counter, vision) -> None:
        self.seq_id = entry.seq_id
        self.role = _TYPE_ROLE.get(entry.type, "user")
        self.sender = "internal" if entry.type == TYPE_INTERNAL else entry.sender_name
        self.ts = _fmt_ts(entry.timestamp)
        self.reply_to = entry.reply_to
        self.reasoning = getattr(entry, "reasoning", "") or ""
        self.body = _esc_body(
            f"{entry.text}{render_attachments(entry.attachments)}".strip())
        self.att = entry.attachments[0] if entry.attachments else None
        images = [a for a in entry.attachments
                  if a.kind == "image" and a.provider_ref is not None and vision is not None]
        self.parts = [vision.content_part(a.provider_ref) for a in images] or None
        self.cost = self.measure(counter)

    def measure(self, counter) -> int:
        """当前形态的代价（body 截断后重算用）。reasoning 不计价（实测服务端不计）。"""
        tag = _message_tag(sender=self.sender, seq_id=self.seq_id, ts=self.ts,
                           body=self.body, att=self.att)
        cost = counter.count(tag)
        if self.parts:
            cost += counter.count_image() * len(self.parts)
        return cost

    def compose(self, visible_seqs: set, include_reasoning: bool = True) -> dict:
        reply_attr, patch = _quote_parts(self.reply_to, visible_seqs)
        body = f"{patch}{self.body}" if patch else self.body
        text = _message_tag(sender=self.sender, seq_id=self.seq_id, ts=self.ts,
                            body=body, reply_attr=reply_attr, att=self.att)
        if self.parts:
            msg = {"role": self.role,
                   "content": [{"type": "text", "text": text}] + self.parts}
        else:
            msg = {"role": self.role, "content": text}
        if self.reasoning and include_reasoning:
            msg["reasoning_content"] = self.reasoning
        return msg


class Block:
    """裁剪原子：chat = 一行对话；tool = 一个 tool_call 行 + 其全部 tool_result 行。"""

    __slots__ = ("kind", "first_seq", "rows", "refs", "cost", "tool_msgs", "call_index",
                 "reasoning")

    def __init__(self, kind: str, rows: List[_Row], cost: int,
                 tool_msgs: Optional[List[dict]] = None, reasoning: str = "") -> None:
        self.kind = kind
        self.rows = rows
        self.first_seq = rows[0].seq_id if rows else 0
        self.refs = [r.reply_to for r in rows if r.reply_to]
        self.cost = cost
        self.tool_msgs = tool_msgs or []
        self.call_index: Dict[str, Block] = {}   # 仅 tool 块有意义（迟到结果修复用）
        self.reasoning = reasoning               # 轮首行的思考草稿（tool 块挂 assistant 首条）

    def compose(self, visible_seqs: set, include_reasoning: bool = True) -> List[dict]:
        if self.kind == "tool":
            msgs = [dict(m) for m in self.tool_msgs]
            if self.reasoning and include_reasoning and msgs:
                msgs[0]["reasoning_content"] = self.reasoning
            return msgs
        return [r.compose(visible_seqs, include_reasoning) for r in self.rows]


class TimelineCache:
    """每会话的增量渲染缓存（内存态；重启从库重建，无需对账）。"""

    def __init__(self) -> None:
        self.built_until_seq = 0                 # 已建块的游标
        self.blocks: List[Block] = []            # 基线块（旧→新，可被裁剪）
        self.recalled: Dict[int, Block] = {}     # 召回块缓存（seq → 块，不计预算）
        self.call_blocks: Dict[str, Block] = {}  # call_id → tool 块（迟到结果修复）
        self.last_cost = 0                       # 最近一次渲染的时间线估算 token


# ----------------------------------------------------------------------
# 建块 / 裁剪
# ----------------------------------------------------------------------
def _build_blocks(rows: list, counter, vision) -> List[Block]:
    """行 → 块。tool_call 行 + 其全部 tool_result（按 call_id 配对，可被插话隔开）
    = 一个原子块；对话行各自成块。孤儿 tool_result（call 行不在本批，冷扫描边界）
    直接丢弃——它们必然在抛弃端。"""
    call_rows: Dict[str, int] = {}       # call_id → 所属 tool_call 行 seq
    results: Dict[str, list] = {}        # call_id → [entry]（旧→新）
    for e in rows:
        if e.type == TYPE_TOOL_CALL:
            for c in e.calls:
                call_rows[c.call_id] = e.seq_id
        elif e.type == TYPE_TOOL_RESULT:
            results.setdefault(e.call_id, []).append(e)

    blocks: List[Block] = []
    emitted_call_rows = set()
    for e in rows:
        if e.type == TYPE_TOOL_RESULT:
            continue                     # 结果行只跟随自己的 tool_call 块走
        if e.type == TYPE_TOOL_CALL:
            if e.seq_id in emitted_call_rows:
                continue
            emitted_call_rows.add(e.seq_id)
            msgs = [{
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": c.call_id, "type": "function",
                     "function": {"name": c.name, "arguments": c.arguments}}
                    for c in e.calls
                ],
            }]
            # 机器件计价：assistant(tool_calls) + 每条 tool 结果，含 JSON 框架
            cost = 12 + sum(24 + counter.count(c.name) + counter.count(c.arguments)
                            for c in e.calls)
            for c in e.calls:
                for r in results.get(c.call_id, ()):
                    msgs.append({"role": "tool", "tool_call_id": r.call_id, "content": r.text})
                    cost += 12 + counter.count(r.text)
            blk = Block("tool", [], cost, tool_msgs=msgs, reasoning=e.reasoning or "")
            blk.first_seq = e.seq_id
            blk.call_index = {c.call_id: blk for c in e.calls}
            blocks.append(blk)
        else:
            row = _Row(e, counter, vision)
            blocks.append(Block("chat", [row], row.cost))
    return blocks


def _trim_blocks(blocks: List[Block], usable: int) -> Tuple[List[Block], int]:
    """从最老端成组抛弃：保留最新的连续一段，代价和 ≤ usable。
    最新块必保留（哪怕单块超预算——正文截断另行处理）。"""
    kept: List[Block] = []
    cost = 0
    for b in reversed(blocks):
        if kept and cost + b.cost > usable:
            break
        kept.append(b)
        cost += b.cost
    kept.reverse()
    return kept, cost


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def render_messages(ledger, session_key: str, *, identity: str, memory_text: str,
                    budget: int, counter, vision=None,
                    cache: Optional[TimelineCache] = None,
                    overhead: int = 0, include_reasoning: bool = True) -> List[dict]:
    """把消息库渲染成 OpenAI messages。每次 LLM 调用现渲染，不落状态。

    budget    时间线 token 预算（system 不占；召回不计）
    counter   TokenCounter 实现（providers/tokens.py）
    cache     TimelineCache（不传 = 全量渲染，测试对拍用）
    overhead  时间线之外但要计入预算的开销估算（工具 schema / turn 内工具机器件 /
              真值锚定残差）——这些是会增长的大头，得挤占时间线预算
    include_reasoning  协议消息是否带 reasoning_content（思考草稿回传；某供应商的
              非思考请求若拒收就关掉，实测 DeepSeek 无所谓）
    """
    cache = cache if cache is not None else TimelineCache()

    # ── 段 1+2：system = 它是谁 + 记忆 ──
    system = identity
    if memory_text.strip():
        system += f"\n\n【neoagent 的记忆】\n{memory_text.strip()}"
    usable = max(int(budget * _BUDGET_SAFETY) - max(overhead, 0), 1)

    # ── 1) 新行进缓存：冷启动翻批扫到够预算；增量只取游标后 ──
    rows = _fetch_rows(ledger, session_key, cache, usable, counter)
    _absorb(cache, rows, counter, vision)

    # ── 2) 裁剪（从最老端成组抛弃；最新块保底）──
    kept, kept_cost = _trim_blocks(cache.blocks, usable)
    if kept_cost > usable and len(kept) == 1 and kept[0].kind == "chat":
        row = kept[0].rows[0]
        row.body = _truncate_text(row.body, usable, counter)
        kept[0].cost = kept_cost = row.measure(counter)
    cache.blocks = kept                        # 顺手淘汰掉出窗口的块（回收内存）

    # ── 3) 引用召回：被保留行引用的旧消息无条件回补（不计预算）──
    recalled = _recall_missing(ledger, session_key, cache, kept, counter, vision)

    # ── 4) 组装（按 seq 时间序）──
    visible = sorted(kept + recalled, key=lambda b: b.first_seq)
    visible_seqs = {r.seq_id for b in visible for r in b.rows}
    msgs: List[dict] = [{"role": "system", "content": system}]
    for b in visible:
        msgs.extend(b.compose(visible_seqs, include_reasoning))
    cache.last_cost = counter.count(system) + sum(b.cost for b in visible)
    return msgs


def _fetch_rows(ledger, session_key: str, cache: TimelineCache,
                usable: int, counter) -> list:
    """取需要建块的新行（旧→新）。"""
    if cache.built_until_seq:
        rows: list = []
        cur = cache.built_until_seq
        while True:
            batch = ledger.since(session_key, cur, limit=_SCAN_BATCH)
            if not batch:
                return rows
            rows += batch
            cur = batch[-1].seq_id
            if len(batch) < _SCAN_BATCH:
                return rows

    # 冷启动：从最新往老翻批，粗估够预算就停（多留 10% 防粗估偏小）
    rows = []
    before: Optional[int] = None
    rough = 0
    target = int(usable * 1.1)
    while True:
        batch = ledger.tail(session_key, before, limit=_SCAN_BATCH)
        if not batch:
            break
        rows = list(reversed(batch)) + rows     # batch 新→旧，拼成旧→新
        rough += sum(counter.count(e.text or "") + 32 for e in batch)   # 粗估（含标签框架）
        before = batch[-1].seq_id
        if rough >= target or len(batch) < _SCAN_BATCH:
            break
    return rows


def _absorb(cache: TimelineCache, rows: list, counter, vision) -> None:
    """新行建块入缓存；顺带修复"迟到的 tool_result"（挂回已建 tool 块）。"""
    if not rows:
        return
    late: Dict[str, list] = {}
    rest: list = []
    for e in rows:
        if e.type == TYPE_TOOL_RESULT and e.call_id in cache.call_blocks:
            late.setdefault(e.call_id, []).append(e)
        else:
            rest.append(e)
    for call_id, lst in late.items():
        blk = cache.call_blocks[call_id]
        for r in lst:
            blk.tool_msgs.append({"role": "tool", "tool_call_id": r.call_id, "content": r.text})
            blk.cost += 12 + counter.count(r.text)

    for blk in _build_blocks(rest, counter, vision):
        cache.blocks.append(blk)
        cache.call_blocks.update(blk.call_index)
    cache.built_until_seq = rows[-1].seq_id


def _recall_missing(ledger, session_key: str, cache: TimelineCache,
                    kept: List[Block], counter, vision) -> List[Block]:
    """召回被引用但不在上下文里的行（含引用链，去重防环，上限 _RECALL_CAP）。"""
    have = {r.seq_id for b in kept for r in b.rows}
    pending = {ref.seq_id for b in kept for ref in b.refs
               if ref.seq_id and ref.seq_id not in have}
    fetched: Dict[int, object] = {}
    while pending and len(fetched) < _RECALL_CAP:
        batch = sorted(pending)[:_RECALL_CAP - len(fetched)]
        pending -= set(batch)
        for r in ledger.by_seqs(session_key, batch):
            if r.seq_id in have or r.seq_id in fetched:
                continue
            fetched[r.seq_id] = r
            if r.reply_to and r.reply_to.seq_id \
                    and r.reply_to.seq_id not in have and r.reply_to.seq_id not in fetched:
                pending.add(r.reply_to.seq_id)

    out: List[Block] = []
    new_cache: Dict[int, Block] = {}
    for seq in sorted(fetched):
        blk = cache.recalled.get(seq)
        if blk is None:
            built = _build_blocks([fetched[seq]], counter, vision)
            blk = built[0] if built else None
        if blk is not None:
            new_cache[seq] = blk
            out.append(blk)
    cache.recalled = new_cache                 # 顺手淘汰不再被引用的召回块
    return out
