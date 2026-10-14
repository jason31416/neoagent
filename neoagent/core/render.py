"""M 的上下文装配：主表（定稿）→ OpenAI messages。

定稿（frames.py）已在入库时完成，这里**几乎不渲染**，只做三件事：

1. system 现拼：它是谁 + R 维护的记忆段（记忆每轮在变，必须现拼）；
2. 预算裁剪：从最老端**成组**（协议组）抛弃直到装进预算——tool 链绝不从中间断；
3. 行原样进 messages：文本零加工；图片 parts 机械拼装（blob 字节 → data URL）。

协议完整性兜底：万一某 tool 结果缺失（当时异常中断），补一条
<error kind="exec_failed"> 占位，保证 assistant(tool_calls) 后必有结果。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import List, Optional, Tuple

from .ledger import TYPE_ASSISTANT, TYPE_INTERNAL, TYPE_TOOL_RESULT, TYPE_USER
from ..providers.vision import sniff_image_mime

# 预算安全系数：估算器宁高勿低，实装再留 10% 余量防误差
_BUDGET_SAFETY = 0.9
# 翻批取行的批大小
_SCAN_BATCH = 500
# DeepSeek 思考模式强制回传 reasoning_content（缺草稿垫 " "，缺字段 400）
_REASONING_PAD = " "
# 结果缺失占位（罕见：入库中途崩过）
_MISSING_RESULT = '<error kind="exec_failed">工具结果缺失（当时异常中断）</error>'

# 图片滞回窗口：这次请求里的图超过 trigger 张 → 一轮替换只保留最新 keep 张，
# 老图降级"（图略）"。降级水位记在会话上（只进不退）——两次替换之间渲染纯追加，
# 缓存前缀不破；每次替换付一次前缀重填，频率 ≈ 每 (trigger-keep) 张超额图一次。
IMAGES_LIVE_TRIGGER = 20
IMAGES_LIVE_KEEP = 10
_IMG_CUT: dict = {}          # session_key → 图片降级水位 seq（内存态；重启重算一次）


def _image_window(session_key: str, rows: List, trigger: int, keep: int) -> int:
    """算本次渲染的图片水位：seq 低于水位的行不带图 parts（原图仍在账本）。

    rows 旧→新；水位只进不退。活图 ≤ trigger 不动；超了就砍到最新 keep 张
    （水位停在"第 keep 张所在的行"，行级整组降级不劈消息）。"""
    cut = _IMG_CUT.get(session_key, 0)
    live = sum(1 for r in rows if r.seq_id >= cut
               for a in r.attachments if a.kind == "image" and a.data)
    if live <= trigger:
        return cut
    acc = 0
    for r in reversed(rows):
        if r.seq_id < cut:
            break
        acc += sum(1 for a in r.attachments if a.kind == "image" and a.data)
        if acc >= keep:
            cut = r.seq_id
            break
    _IMG_CUT[session_key] = cut
    return cut


def render_messages(ledger, session_key: str, *, identity: str, memory_text: str,
                    budget: int, counter, vision=None,
                    overhead: int = 0, include_reasoning: bool = True,
                    cut_seq: int = 0,
                    images_live: Tuple[int, int] = (IMAGES_LIVE_TRIGGER,
                                                    IMAGES_LIVE_KEEP)
                    ) -> Tuple[List[dict], int, bool]:
    """装配一次请求的 messages。

    返回 (messages, 时间线估算 token, 是否被迫裁剪)。
    cut_seq   上下文切点（压缩时机械推进）：低于它的行不装配（记录不动）。
    include_reasoning  协议消息是否带 reasoning_content；碰到见这个字段就拒收的
              严格供应商（Mistral/Groq 等）关掉。
    """
    system = identity
    if memory_text.strip():
        system += (f"\n\n【neoagent 的记忆】（记忆与最近对话冲突时，以对话为准）"
                   f"\n（指针/归档里的子文件按需用 read_file 读，路径写 memory://文件名，"
                   f"如 memory://项目A-接口细节.md）"
                   f"\n{memory_text.strip()}")
    usable = max(int(budget * _BUDGET_SAFETY) - max(overhead, 0), 1)

    groups, trimmed = _select_groups(ledger, session_key, usable, counter, cut_seq)
    if len(groups) == 1 and sum(_row_cost(r, counter) for r in groups[0]) > usable:
        # 最新组保底：单组超预算就截最大那行的正文（保头 60% + 尾 40%）
        victim = max(groups[0], key=lambda r: counter.count(r.text or ""))
        victim.text = _truncate_text(victim.text, usable, counter)
    img_cut = _image_window(session_key, [r for g in groups for r in g],
                            *images_live)      # 图片滞回窗口（老图降级占位）
    msgs: List[dict] = [{"role": "system", "content": system}]
    cost = counter.count(system)
    for g in groups:
        for r in _fill_missing(g):
            msg, c = _assemble(r, counter, vision, include_reasoning, img_cut)
            msgs.append(msg)
            cost += c
    return msgs, cost, trimmed


# ----------------------------------------------------------------------
# 选择：尾部翻批 → 组原子裁剪
# ----------------------------------------------------------------------
def _row_cost(r, counter) -> int:
    c = 12 + counter.count(r.text or "")
    for call in r.calls:
        c += 24 + counter.count(call.name) + counter.count(call.arguments)
    if r.type == TYPE_ASSISTANT:            # 思考草稿要回传，成本得算上
        c += counter.count(r.reasoning or "")
    c += sum(counter.count_image(a.size or len(a.data)) for a in r.attachments
             if a.kind == "image" and a.data)   # 图按字节估（贴近真实体积）
    return c


def _select_groups(ledger, session_key: str, usable: int, counter,
                   cut_seq: int) -> Tuple[List[list], bool]:
    """取行（尾部翻批够预算）→ 按协议组成组 → 从最老端成组裁剪。"""
    rows: List = []
    before = None
    rough = 0
    target = int(usable * 1.1)
    while True:
        batch = ledger.tail(session_key, before, limit=_SCAN_BATCH)
        below = [r for r in batch if r.seq_id < cut_seq]
        batch = [r for r in batch if r.seq_id >= cut_seq]
        if batch:
            rows = batch + rows            # batch 新→旧，拼成旧→新
            rough += sum(counter.count(r.text or "") + 32 for r in batch)
        if below or not batch or rough >= target or len(batch) < _SCAN_BATCH:
            break                          # 摸到切点/扫完/粗估够了
        before = batch[-1].seq_id
    return _trim_groups(rows, usable, counter)


def _trim_groups(rows: List, usable: int, counter) -> Tuple[List[list], bool]:
    """按协议组成组（组序 = 组首 seq；组内 seq 旧→新），从最老端成组裁剪。"""
    by_group: dict = {}
    for r in rows:
        by_group.setdefault(r.group_id, []).append(r)
    groups = [sorted(by_group[g], key=lambda r: r.seq_id) for g in sorted(by_group)]
    costs = [sum(_row_cost(r, counter) for r in g) for g in groups]

    keep_from = 0
    total = sum(costs)
    while keep_from < len(groups) - 1 and total > usable:
        total -= costs[keep_from]
        keep_from += 1
    trimmed = keep_from > 0

    kept = groups[keep_from:]
    # 尾部翻批可能只抓到某组的后半截：组首（assistant 行）缺席的孤儿 tool 行丢弃
    while kept and kept[0] and kept[0][0].type == TYPE_TOOL_RESULT:
        kept = kept[1:]
        trimmed = True
    return kept, trimmed


def _fill_missing(group: List) -> List:
    """组内 tool 结果缺失（异常中断遗留）→ 补占位，协议形状不留洞。"""
    have = {r.call_id for r in group if r.type == TYPE_TOOL_RESULT}
    out: List = []
    for r in group:
        out.append(r)
        if r.type == TYPE_ASSISTANT:
            for call in r.calls:
                if call.call_id not in have:
                    out.append(SimpleNamespace(
                        type=TYPE_TOOL_RESULT, call_id=call.call_id,
                        tool_name=call.name, text=_MISSING_RESULT,
                        reasoning="", calls=[], attachments=[],
                        group_id=r.group_id, seq_id=r.seq_id, timestamp=r.timestamp))
    return out


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
# 上下文用量：统计（R 整理读）+ 行成本（压缩报告算释放量）
# ----------------------------------------------------------------------
LIVE_ROWS_LIMIT = 1_000_000   # 活行扫描不设行数帽（整理/压缩的窗口自管封顶）

@dataclass
class ContextStats:
    """一次渲染的上下文用量（agent_loop 落，Recorder 整理时读）。"""
    tokens: int = 0        # 估算总量（时间线 + 工具 schema 等开销）
    predicted: int = 0     # 锚定预测的真实总量
    budget: int = 0        # 预算（上限）
    ts: float = field(default_factory=time.time)


def rows_cost(rows, counter) -> int:
    """一批行的渲染成本合计（与预算口径一致，含工具结果/思考草稿）。"""
    return sum(_row_cost(r, counter) for r in rows)


# ----------------------------------------------------------------------
# 组装：行 → 协议消息（文本零加工）
# ----------------------------------------------------------------------
def _parts(r, vision, img_cut: int = 0) -> Optional[list]:
    """图片附件 → content parts（blob 字节现编码；格式不支持的保险丝拦下）。
    img_cut：图片滞回水位——seq 低于它的行不带图（占位见 _assemble）。"""
    if vision is None:
        return None
    parts = []
    for a in r.attachments:
        if a.kind == "image" and a.data and r.seq_id >= img_cut:
            mime = sniff_image_mime(a.data)      # 坏图绝不进上下文（会 400）
            if mime:
                parts.append(vision.content_part(a.data, mime))
    return parts or None


def _assemble(r, counter, vision, include_reasoning: bool,
              img_cut: int = 0) -> Tuple[dict, int]:
    parts = _parts(r, vision, img_cut)
    cost = _row_cost(r, counter)
    hidden = bool(img_cut) and r.seq_id < img_cut and any(
        a.kind == "image" and a.data for a in r.attachments)
    text = r.text + ("\n（图略）" if hidden else "")

    if r.type == TYPE_TOOL_RESULT:
        content: object = ([{"type": "text", "text": text}] + parts) if parts else text
        return {"role": "tool", "tool_call_id": r.call_id, "content": content}, cost

    if r.type == TYPE_ASSISTANT:
        msg: dict = {
            "role": "assistant",
            "content": r.text if r.text else (None if r.calls else ""),
        }
        if r.calls:
            msg["tool_calls"] = [
                {"id": c.call_id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in r.calls
            ]
        if include_reasoning:
            msg["reasoning_content"] = r.reasoning or _REASONING_PAD
        return msg, cost

    # user / internal / wake：internal 是流程注记（role=system）；
    # wake 是自我唤醒（role=user，正文是 <wake> 块）——都在消息流里
    role = "system" if r.type == TYPE_INTERNAL else "user"
    if parts:
        return {"role": role,
                "content": [{"type": "text", "text": text}] + parts}, cost
    return {"role": role, "content": text}, cost
