"""上下文压缩：撞限 / 强制时的机械压缩（chatagent 与主循环共用一个核）。

一次压缩 = 一次总结调用 + 三条机械动作：
1. `<compact>` 备忘行落账（TYPE_INTERNAL，渲染成 system 消息，是压缩后的最新消息）；
2. 切点推进到"原样保留段"的组首（set_recorder_state 自带吸附协议组边界、
   单调只进不退——tool 链绝不从中间断开）；
3. 切点以下的图回收（压缩即销毁）。

没有模型裁量：切点什么时候动、动到哪都是机械决定。主循环只有两个压缩时刻——
撞到上下文限制（装配被迫砍行 / 超 90% 预算）、/compact 强制；chatagent 则在
压力超阈值后静默压。"先落记忆再退役"不在这层：R 的退役前扫描由调用方并发安排
（见 Recorder.run_retiring），两边各用各的切点口径。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .ledger import TYPE_INTERNAL, MessageLedger
from .render import LIVE_ROWS_LIMIT, rows_cost
from ..providers.vision import sniff_image_mime

logger = logging.getLogger("neoagent.compaction")

COMPACT_KEEP = 10          # 压缩后原样保留的最近消息条数
COMPACT_MAX_IMAGES = 6     # 总结调用最多带几张图
PRESSURE_PCT = 0.9         # 上下文占比超过它算"撞限"（chatagent 静默压缩的阈值）

# 主循环的总结口径（chatagent 的群聊口径在 chatagents.py 里，各用各的）
COMPACT_PROMPT = (
    """You are now acting as a compaction engine for neoagent, which is an AI assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context. This assistant completes real-world tasks with tools (searching, reading, extracting, computing, sending, writing)

Output the Markdown structure below. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section.

## Current Request and Intent
- [the current unfinished goal that the agent is working on; quote from user verbatim where the exact wording matters]

## User Preferences and Constraints
- [stated preferences, hard constraints, required formats, deadlines, tone, and things to avoid]

## Key Facts and Sources
- [established facts the outcome depends on, each tagged with its origin: user statement, tool result (name the tool or URL), or inference]

## Actions Taken and Outcomes
- [tool calls that produced a key result or changed something: what was done, where, and the outcome. Record every external side effect — messages sent, files written, records updated, purchases or bookings made — so it is never repeated]

## Data and Artifacts
- [values, IDs, references, extracted content, links, and produced outputs worth keeping]

## Errors and Recoveries
- [failed tool calls or wrong turns: what went wrong and what fixed or worked around it]

## Pending Tasks
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, open questions, assumptions still to verify, and the data needed to continue]

Rules:
- Write concise prose in the same language as the user's most recent messages.
- Preserve exact names, URLs, identifiers, numbers, dates, quotes, and command or parameter strings; never round, translate, or paraphrase them.
- Capture user instructions and corrections faithfully, especially the latest ones: the most recent instruction overrides all earlier ones.
- Keep verified facts separate from assumptions; label anything unverified as an assumption.
- Discard dead ends, transient tool noise, and details no longer relevant to the goal — keep only what a model needs to continue the work.
- Prefer durable state over narration: what is true now, not how the conversation unfolded.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compact> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""
)


@dataclass
class CompactResult:
    summary: str          # 接续备忘正文
    kept: int             # 原样保留的行数
    dropped: int          # 退役的行数
    cut_before: int
    cut_after: int
    freed: int            # 退役行的渲染成本（估算 token）
    blobs_gone: int       # 回收的图


def plain_row_view(r) -> str:
    """默认行视图：单行取纯文本（chatagent 的流水够干净，这样就行）。"""
    return r.text or ""


async def compact(ledger: MessageLedger, llm, session_key: str, *,
                  prompt: str, counter=None,
                  row_view: Callable = plain_row_view,
                  vision=None, keep: int = COMPACT_KEEP,
                  session: str = "") -> Optional[CompactResult]:
    """跑一次压缩；没什么可压（活行 ≤ keep）时返回 None，账本不动。

    总结调用失败会抛给调用方（/compact 要回报"压缩失败"，静默路径自己吞）。
    """
    _, cut = ledger.get_recorder_state(session_key)
    rows = ledger.since(session_key, max(cut - 1, 0), limit=LIVE_ROWS_LIMIT)
    if len(rows) <= keep:
        return None
    kept = rows[-keep:]
    dropped = rows[:-keep]

    summary = (await _summarize(llm, rows, row_view, prompt, vision,
                                session or session_key)).strip()
    if not summary:
        logger.warning("[compact] 总结为空，本次不动账本 %s", session_key)
        return None

    ts = time.time()
    body = summary.replace("</compact>", "</ compact>")   # 防结束符截断备忘
    stamp = time.strftime("%Y-%m-%d %H:%M")
    ledger.add_turn(
        session_key=session_key, type=TYPE_INTERNAL,
        text='<compact time="%s">%s</compact>' % (stamp, body),
        timestamp=ts)
    ledger.set_recorder_state(session_key, cut_seq=kept[0].seq_id)
    gone = ledger.reclaim_blobs(session_key)
    _, cut_after = ledger.get_recorder_state(session_key)
    result = CompactResult(
        summary=summary, kept=len(kept), dropped=len(dropped),
        cut_before=cut, cut_after=cut_after,
        freed=rows_cost(dropped, counter) if counter is not None else 0,
        blobs_gone=gone)
    logger.info("[compact] %s：留 %d 条 + 备忘，切点 %d→%d，回收图 %d",
                session_key, result.kept, result.cut_before, result.cut_after,
                result.blobs_gone)
    return result


async def _summarize(llm, rows: List, row_view: Callable, prompt: str,
                     vision, session: str) -> str:
    """打包行（文字 + 图）跑一次总结调用（思考关）。row_view：单行 → 文本。"""
    content: list = [{"type": "text",
                      "text": "\n".join(x for x in (row_view(r) for r in rows) if x)}]
    n = 0
    for r in rows:
        for a in r.attachments:
            if a.kind == "image" and a.data and n < COMPACT_MAX_IMAGES:
                mime = sniff_image_mime(a.data)
                if mime and vision is not None:
                    content.append(vision.content_part(a.data, mime))
                    n += 1
    msgs = [{"role": "system", "content": prompt},
            {"role": "user", "content": content}]
    result = await llm.chat(msgs, reasoning_effort="none", session=session)
    return result.content or ""
