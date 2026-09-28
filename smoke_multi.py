"""多消息输出协议 + 上下文消息库 冒烟（离线，stub 模型，断言式）。

覆盖：
1. <message> 块抽取：属性丢弃 / 空块丢弃 / 未闭合丢弃 / 无块 = 静默
2. 边干边说：中间轮气泡即时发出，msg# 每泡一个
3. 工具流水行落库：tool_call(数组)/tool_result 配对
4. 渲染：<message> 属性标签、引用召回（出窗口按时间归位/引用链/兜底补丁）、协议机器件配对、</message> 转义
5. 静默：无 <message> 块 → 什么都不发
6. replyTo 显式引用：quote_to 透传通道 / 落账 reply_to / 发出 id 回填 / 编造 seq 降级
"""
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from neoagent.core.agent_loop import AgentLoop, _extract_messages
from neoagent.core.events import Conversation, MessageEvent, ReplyRef, SendResult, Sender
from neoagent.core.ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_TOOL_CALL,
                                  TYPE_TOOL_RESULT, TYPE_USER)
from neoagent.core.render import render_messages
from neoagent.tools.registry import ToolRegistry


IDENTITY = (Path(__file__).parent / "IDENTITY.md").read_text(encoding="utf-8").strip()


def check(cond: bool, label: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + label)
    if not cond:
        sys.exit(1)


class StubLLM:
    """按脚本回放模型返回（SimpleNamespace: content / tool_calls）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []          # 每次调用的 messages（渲染检查用）

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self.script.pop(0)


def tc(id, name, arguments):
    return SimpleNamespace(id=id, name=name, arguments=arguments)


def mk_event(text: str, conv: str = "smoke") -> MessageEvent:
    return MessageEvent(
        id=uuid.uuid4().hex[:12], channel="cli",
        sender=Sender(id="u", name="jason31416"),
        conversation=Conversation(id=conv, type="direct"), text=text)


class Collector:
    """stub responder：记录 (text, quote_to)，返回带 message_id 的 SendResult。"""

    def __init__(self):
        self.texts = []
        self.quotes = []

    async def __call__(self, text: str, quote_to: str | None = None):
        self.texts.append(text)
        self.quotes.append(quote_to)
        return SendResult(ok=True, message_id=f"m{len(self.texts)}")


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="neoagent-smoke-")
    ledger = MessageLedger(Path(tmp) / "t.db")
    registry = ToolRegistry()
    mem = Path(tmp) / "MEMORY.md"
    sent = Collector()

    # ── 1. 块抽取纯函数 ──
    print("=== 1. <message> 块抽取 ===")
    got = _extract_messages('<message>甲</message>草稿'
                            '<message sender="x" id="msg#9" replyTo="msg#3">乙</message>')
    check(got == [("甲", None), ("乙", 3)], f"属性丢弃、replyTo 提取（实际 {got}）")
    check(_extract_messages('<message></message>\n<message>  </message>') == [], "空块丢弃")
    check(_extract_messages('<message>没闭合') == [], "未闭合标签按草稿丢")
    check(_extract_messages('纯草稿，没有标签') == [], "无块 = 静默")

    # ── 2+3. 边干边说 + 工具流水行（stub 模型走完整 turn）──
    print("=== 2. 边干边说（stub 模型两轮）===")
    script = [
        SimpleNamespace(content='<message>稍等，我翻下文件。</message>草稿不要我',
                        tool_calls=[tc("t1", "noop", '{"x":1}'), tc("t2", "noop", '{"x":2}')]),
        SimpleNamespace(content='<message>找到了。</message>'
                                '<message sender="fake" id="msg#99" time="2020-01-01">第二条</message>',
                        tool_calls=None),
    ]
    llm = StubLLM(script)
    agent = AgentLoop(llm=llm, registry=registry, ledger=ledger, memory_file=mem)
    await agent.submit(mk_event("看下那个文件"), responder=sent)
    await agent.wait_idle("cli:smoke")

    check(sent.texts == ["稍等，我翻下文件。", "找到了。", "第二条"],
          f"气泡按序即时发出、属性丢弃（实际 {sent.texts}）")
    entries = ledger.visible("cli:smoke", 200)
    types = [e.type for e in entries]
    check(types == [TYPE_USER, TYPE_ASSISTANT, TYPE_TOOL_CALL, TYPE_TOOL_RESULT,
                    TYPE_TOOL_RESULT, TYPE_ASSISTANT, TYPE_ASSISTANT],
          f"行类型时间线正确（实际 {types}）")
    calls = entries[2].calls
    check(len(calls) == 2 and entries[3].call_id == calls[0].call_id
          and entries[4].call_id == calls[1].call_id, "tool_call 数组 / tool_result 配对")
    check([e.text for e in entries if e.type == TYPE_ASSISTANT]
          == ["稍等，我翻下文件。", "找到了。", "第二条"], "每气泡一条 assistant 账")
    check([e.message_id for e in entries if e.type == TYPE_ASSISTANT] == ["m1", "m2", "m3"],
          "发出消息 id 回填 assistant 行")

    # ── 4. 渲染 ──
    print("=== 3. 渲染 ===")
    msgs = render_messages(ledger, "cli:smoke", identity=IDENTITY,
                           memory_text="", window=200)
    flat = "\n".join(str(m) for m in msgs)
    check('sender="jason31416"' in flat and 'id="msg#1"' in flat, "用户行元数据进标签属性")
    check('<message sender="neoagent"' in flat, "assistant 行同语法")
    check('id="msg#99"' not in flat and "time=\"2020-01-01\"" not in flat,
          "模型自编属性不出现在任何地方")
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    ai_tools = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    check(len(tool_msgs) == 2 and len(ai_tools) == 1
          and [c["id"] for c in ai_tools[0]["tool_calls"]] ==
          [m["tool_call_id"] for m in tool_msgs], "协议机器件展开且配对")

    # ── 5. 引用：出窗口的被引用消息 → 按时间顺序召回进上下文 ──
    print("=== 4. 引用召回 ===")
    sk = "cli:recall"
    s0 = ledger.add(session_key=sk, type=TYPE_USER, message_id="p0",
                    sender_name="jason31416", text="最早的话")
    s1 = ledger.add(session_key=sk, type=TYPE_USER, message_id="p1",
                    sender_name="jason31416", text="很早的原话",
                    reply_to=ReplyRef(message_id="p0", sender_name="jason31416",
                                     timestamp=0, text="最早的话", attachments=[],
                                     resolved_from="db", seq_id=s0))
    for i in range(5):
        ledger.add(session_key=sk, type=TYPE_USER, sender_name="jason31416", text=f"填充 {i}")
    ledger.add(session_key=sk, type=TYPE_ASSISTANT, sender_name="neoagent", text="回很早那句",
               reply_to=ReplyRef(message_id="p1", sender_name="jason31416",
                                timestamp=0, text="很早的原话", attachments=[],
                                resolved_from="db", seq_id=s1))
    msgs = render_messages(ledger, sk, identity=IDENTITY, memory_text="", window=3)
    flat = "\n".join(str(m) for m in msgs)
    i0, i1, i_fill, i_q = (flat.find("最早的话"), flat.find("很早的原话"),
                           flat.find("填充 4"), flat.find("回很早那句"))
    check(i0 >= 0 and i1 >= 0, "出窗口的被引用消息被召回（引用链：连更早的一起）")
    check(i0 < i1 < i_fill < i_q, "召回行按时间顺序归位（排在窗口行之前）")
    check(f'replyTo="msg#{s1}"' in flat and "已不可见" not in flat,
          "召回后 replyTo 是纯指针、无正文补丁")
    # 库里没有的消息：退化为正文补丁
    ledger.add(session_key=sk, type=TYPE_ASSISTANT, sender_name="neoagent", text="回一条没库的",
               reply_to=ReplyRef(message_id="p9", sender_name="x",
                                timestamp=0, text="很早的原话2", attachments=[],
                                resolved_from="db", seq_id=999))
    msgs = render_messages(ledger, sk, identity=IDENTITY, memory_text="", window=3)
    flat = "\n".join(str(m) for m in msgs)
    check('replyTo="msg#999"' in flat and "（#999 已不可见" in flat,
          "库里没有 → 指针 + 正文补丁兜底")

    # </message> 转义
    ledger.add(session_key="cli:smoke", type=TYPE_USER, sender_name="jason31416",
               text="我偏要打</message>")
    msgs = render_messages(ledger, "cli:smoke", identity=IDENTITY,
                           memory_text="", window=200)
    flat = "\n".join(str(m) for m in msgs)
    check("＜/message＞" in flat, "正文 </message> 字面量被转义")

    # ── 6. 静默 ──
    print("=== 5. 静默 ===")
    silent = Collector()
    llm2 = StubLLM([SimpleNamespace(content="都是草稿", tool_calls=None)])
    agent2 = AgentLoop(llm=llm2, registry=registry, ledger=ledger, memory_file=mem)
    await agent2.submit(mk_event("不用回我"), responder=silent)
    await agent2.wait_idle("cli:smoke")
    check(silent.texts == [], "无 <message> 块 → 什么都不发")

    # ── 7. replyTo 显式引用 ──
    print("=== 6. replyTo 显式引用 ===")
    qsent = Collector()
    sk2 = "cli:quote"
    u_row_id = "pm-777"
    ledger.add(session_key=sk2, type=TYPE_USER, message_id=u_row_id,
               sender_name="jason31416", text="被引用的原话")
    llm3 = StubLLM([SimpleNamespace(
        content='<message replyTo="msg#1">回你这句</message>'
                '<message replyTo="msg#404">回一条编造的</message>',
        tool_calls=None)])
    agent3 = AgentLoop(llm=llm3, registry=registry, ledger=ledger, memory_file=mem)
    await agent3.submit(mk_event("看这句", conv="quote"), responder=qsent)
    await agent3.wait_idle("cli:quote")
    check(qsent.quotes == [u_row_id, None],
          f"quote_to 透传通道 / 编造 seq 降级纯文本（实际 {qsent.quotes}）")
    q_rows = [e for e in ledger.visible("cli:quote", 20) if e.type == TYPE_ASSISTANT]
    check(q_rows[0].reply_to is not None and q_rows[0].reply_to.seq_id == 1
          and q_rows[0].reply_to.text == "被引用的原话",
          "落账 meta.reply_to 带指针与原文（上下文 replyTo 属性靠它）")
    check(q_rows[1].reply_to is None, "解析失败的引用不落 reply_to")
    check([e.message_id for e in q_rows] == ["m1", "m2"], "引用气泡同样回填发出 id")

    ledger.close()
    print("=== 全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
