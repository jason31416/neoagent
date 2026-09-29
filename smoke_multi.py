"""多消息输出协议 + 上下文消息库 + token 预算窗口 冒烟（离线，stub 模型，断言式）。

覆盖：
1. <message> 块抽取：属性丢弃 / 空块丢弃 / 未闭合丢弃 / 无块 = 静默
2. 边干边说：中间轮气泡即时发出，msg# 每泡一个
3. 工具流水行落库：tool_call(数组)/tool_result 配对
4. 渲染：<message> 属性标签、协议机器件配对、</message> 转义
5. 引用召回：出窗口按时间归位（不计预算）/ 引用链 / 兜底补丁
6. 静默：无 <message> 块 → 什么都不发
7. replyTo 显式引用：quote_to 透传通道 / 落账 reply_to / 发出 id 回填 / 编造 seq 降级
8. token 预算：从最老端成组抛弃 / tool 组原子 / 最新块保底截断 / 增量渲染==全量渲染
"""
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from neoagent.core.agent_loop import AgentLoop, _extract_messages
from neoagent.core.events import (Attachment, Conversation, MessageEvent, ReplyRef,
                                  SendResult, Sender)
from neoagent.core.ledger import (MessageLedger, TYPE_ASSISTANT, TYPE_TOOL_CALL,
                                  TYPE_TOOL_RESULT, TYPE_USER)
from neoagent.core.render import TimelineCache, render_messages
from neoagent.providers.tokens import HeuristicTokenCounter, TokenAnchor, count_chat_payload
from neoagent.tools.registry import ToolRegistry

IDENTITY = (Path(__file__).parent / "IDENTITY.md").read_text(encoding="utf-8").strip()
COUNTER = HeuristicTokenCounter()


def check(cond: bool, label: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + label)
    if not cond:
        sys.exit(1)


class StubLLM:
    """按脚本回放模型返回（SimpleNamespace: content / tool_calls / reasoning）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []          # 每次调用的 messages（渲染检查用）
        self.efforts = []       # 每次调用的 reasoning_effort

    async def chat(self, messages, tools=None, reasoning_effort=None):
        self.seen.append(messages)
        self.efforts.append(reasoning_effort)
        r = self.script.pop(0)
        if not hasattr(r, "reasoning"):
            r.reasoning = None
        return r


def tc(id, name, arguments):
    return SimpleNamespace(id=id, name=name, arguments=arguments)


def mk_event(text: str, conv: str = "smoke") -> MessageEvent:
    return MessageEvent(
        id=uuid.uuid4().hex[:12], channel="cli",
        sender=Sender(id="u", name="jason31416"),
        conversation=Conversation(id=conv, type="direct"), text=text)


class Collector:
    """stub responder：记录文本/媒体发送，返回带 message_id 的 SendResult。"""

    def __init__(self, ok: bool = True):
        self.texts = []
        self.quotes = []
        self.media = []      # (path, quote_to)
        self.ok = ok

    async def __call__(self, text: str, quote_to: str | None = None, media: str | None = None):
        if media:
            self.media.append((media, quote_to))
        else:
            self.texts.append(text)
            self.quotes.append(quote_to)
        return SendResult(ok=self.ok, error=None if self.ok else "通道不支持",
                          message_id=f"m{len(self.texts) + len(self.media)}")


def render(ledger, sk, budget=250_000, cache=None, overhead=0):
    return render_messages(ledger, sk, identity=IDENTITY, memory_text="",
                           budget=budget, counter=COUNTER, cache=cache, overhead=overhead)


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="neoagent-smoke-")
    ledger = MessageLedger(Path(tmp) / "t.db")
    registry = ToolRegistry()
    mem = Path(tmp) / "MEMORY.md"
    sent = Collector()

    # ── 1. 块抽取纯函数 ──
    print("=== 1. 消息/媒体块抽取 ===")
    got = _extract_messages('<message>甲</message>草稿'
                            '<img src="a.png"/>'
                            '<message sender="x" id="msg#9" replyTo="msg#3">乙</message>'
                            '<file src="b.pdf" name="报告"/>')
    check([(o.kind, o.text or o.path, o.quote_seq, o.name) for o in got]
          == [("text", "甲", None, ""), ("image", "a.png", None, ""),
              ("text", "乙", 3, ""), ("file", "b.pdf", None, "报告")],
          f"属性丢弃、replyTo/src/name 提取、顺序保留（实际 {got}）")
    check(_extract_messages('<message></message>\n<message>  </message>') == [], "空块丢弃")
    check(_extract_messages('<message>没闭合') == [], "未闭合标签按草稿丢")
    check(_extract_messages('纯草稿，没有标签') == [], "无块 = 静默")
    check(_extract_messages('<img alt="没src"/>') == [], "没 src 的媒体标签按草稿丢")
    inner = _extract_messages('<message>看这个 <img src="in.png"/></message>')
    check(len(inner) == 1 and inner[0].kind == "text" and "<img" in inner[0].text,
          "message 正文里的 <img> 不另拆消息")

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

    # 工具流水在请求里恰好一份（回归锚：曾有"落库渲染 + turn_tool_msgs 暂存"双份 bug）
    req2 = llm.seen[-1]
    tc_msgs = [m for m in req2 if m.get("tool_calls")]
    tool_ids = [m.get("tool_call_id") for m in req2 if m["role"] == "tool"]
    check(len(tc_msgs) == 1 and tool_ids == [calls[0].call_id, calls[1].call_id],
          f"每轮工具流水恰好一份、id 与落库持久 id 一致（实际 tool_calls 消息 "
          f"{len(tc_msgs)} 条，tool_ids={tool_ids}）")

    # ── 4. 渲染 ──
    print("=== 3. 渲染 ===")
    flat = "\n".join(str(m) for m in render(ledger, "cli:smoke"))
    check('sender="jason31416"' in flat and 'id="msg#1"' in flat, "用户行元数据进标签属性")
    check('<message sender="neoagent"' in flat, "assistant 行同语法")
    check('id="msg#99"' not in flat and "time=\"2020-01-01\"" not in flat,
          "模型自编属性不出现在任何地方")
    msgs = render(ledger, "cli:smoke")
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    ai_tools = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    check(len(tool_msgs) == 2 and len(ai_tools) == 1
          and [c["id"] for c in ai_tools[0]["tool_calls"]] ==
          [m["tool_call_id"] for m in tool_msgs], "协议机器件展开且配对")

    # ── 5. 引用召回：出预算窗口的被引用消息 → 按时间顺序召回（不计预算）──
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
        ledger.add(session_key=sk, type=TYPE_USER, sender_name="jason31416",
                   text=f"填充 {i}" + "x" * 400)
    ledger.add(session_key=sk, type=TYPE_ASSISTANT, sender_name="neoagent", text="回很早那句",
               reply_to=ReplyRef(message_id="p1", sender_name="jason31416",
                                timestamp=0, text="很早的原话", attachments=[],
                                resolved_from="db", seq_id=s1))
    msgs = render(ledger, sk, budget=600)     # 小预算：s0/s1 被裁出窗口
    flat = "\n".join(str(m) for m in msgs)
    i_fill, i_q = flat.find("填充 4"), flat.find("回很早那句")
    seg = flat[flat.rfind("<message", 0, i_q):i_q]
    check(f'<recall id="msg#{s1}"' in seg and f'<recall id="msg#{s0}"' in seg,
          "窗外引用内联 <recall>（全文；引用链内嵌展平）")
    check(flat.find("最早的话") > i_fill and flat.find("很早的原话") > i_fill,
          "不回插时间线（回补内联进引用方消息，保前缀缓存）")
    check(f'replyTo="msg#{s1}"' in flat and "已不可见" not in flat,
          "回补后 replyTo 是纯指针、无降级补丁")
    # 库里没有的消息：退化为正文补丁
    ledger.add(session_key=sk, type=TYPE_ASSISTANT, sender_name="neoagent", text="回一条没库的",
               reply_to=ReplyRef(message_id="p9", sender_name="x",
                                timestamp=0, text="很早的原话2", attachments=[],
                                resolved_from="db", seq_id=999))
    msgs = render(ledger, sk, budget=600)
    flat = "\n".join(str(m) for m in msgs)
    check('replyTo="msg#999"' in flat and "（#999 已不可见" in flat,
          "库里没有 → 指针 + 正文补丁兜底")

    # </message> 转义
    ledger.add(session_key="cli:smoke", type=TYPE_USER, sender_name="jason31416",
               text="我偏要打</message>")
    flat = "\n".join(str(m) for m in render(ledger, "cli:smoke"))
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

    # ── 8. token 预算：抛弃 / 原子 / 截断 / 对拍 ──
    print("=== 7. token 计数与裁剪 ===")
    check(COUNTER.count("你好") == 2 and COUNTER.count("abcd") == 1
          and COUNTER.count("a") == 1, "启发式计数（CJK 1/字，ASCII 4 字/词）")

    sk3 = "cli:trim"
    for i in range(30):
        ledger.add(session_key=sk3, type=TYPE_USER, sender_name="jason31416",
                   text=f"重{i} " + "x" * 400)
    flat = "\n".join(str(m) for m in render(ledger, sk3, budget=1_000))
    check("重29 " in flat and "重0 " not in flat and "重20 " not in flat,
          "从最老端抛弃：最新的在、最老的不在")
    n_msgs = flat.count("<message ")
    check(n_msgs <= 10, f"裁剪到预算内（保留 {n_msgs} 条）")

    print("=== 8. tool 组原子性 ===")
    sk4 = "cli:toolgroup"
    ledger.add(session_key=sk4, type=TYPE_TOOL_CALL, sender_name="neoagent",
               calls=[{"call_id": "c1", "name": "exec", "arguments": "{}"},
                      {"call_id": "c2", "name": "exec", "arguments": "{}"}])
    ledger.add(session_key=sk4, type=TYPE_TOOL_RESULT, sender_name="neoagent",
               call_id="c1", name="exec", text="r1 " + "x" * 400)
    ledger.add(session_key=sk4, type=TYPE_USER, sender_name="jason31416", text="插话")
    ledger.add(session_key=sk4, type=TYPE_TOOL_RESULT, sender_name="neoagent",
               call_id="c2", name="exec", text="r2 " + "x" * 400)
    for i in range(10):
        ledger.add(session_key=sk4, type=TYPE_USER, sender_name="jason31416",
                   text=f"新{i} " + "x" * 400)

    def tool_state(msgs):
        has_call = any(m.get("tool_calls") for m in msgs)
        n_tool = sum(1 for m in msgs if m.get("role") == "tool")
        return has_call, n_tool

    has_call, n_tool = tool_state(render(ledger, sk4, budget=1_200))
    check(not has_call and n_tool == 0, "预算不够 → 旧 tool 组整组被抛弃")
    has_call, n_tool = tool_state(render(ledger, sk4, budget=2_000))
    check(has_call and n_tool == 2, "预算够 → tool_call/results 整组保留（含被插话隔开的）")

    print("=== 9. 最新块保底截断 ===")
    sk5 = "cli:overflow"
    ledger.add(session_key=sk5, type=TYPE_USER, sender_name="jason31416",
               text="超长 " + "x" * 5000)
    flat = "\n".join(str(m) for m in render(ledger, sk5, budget=300))
    check("超长截断" in flat and "超长 " in flat, "单块超预算：最新块保底并截断（保头尾）")

    print("=== 10. 增量渲染 == 全量渲染 ===")
    sk6 = "cli:inc"
    cache = TimelineCache()
    ledger.add(session_key=sk6, type=TYPE_USER, sender_name="jason31416",
               text="第一段 " + "x" * 300)
    render(ledger, sk6, cache=cache)
    ledger.add(session_key=sk6, type=TYPE_USER, sender_name="jason31416",
               text="第二段 " + "x" * 300)
    ledger.add(session_key=sk6, type=TYPE_TOOL_CALL, sender_name="neoagent",
               calls=[{"call_id": "ic1", "name": "exec", "arguments": "{}"}])
    ledger.add(session_key=sk6, type=TYPE_TOOL_RESULT, sender_name="neoagent",
               call_id="ic1", name="exec", text="ir " + "x" * 200)
    inc = render(ledger, sk6, cache=cache)
    full = render(ledger, sk6)
    check(inc == full, "增量缓存渲染与全量渲染结果一致")
    check(cache.last_cost > 0, f"本地 token 估算有值（{cache.last_cost}）")

    print("=== 11. 媒体消息（<img>/<file> 独立消息）===")
    img_path = Path(tmp) / "pic.png"
    img_path.write_bytes(b"\x89PNG fake")
    msent = Collector()
    ev = mk_event("发图", conv="media")
    llm4 = StubLLM([SimpleNamespace(
        content=f'<message>图来了</message><img src="{img_path}" replyTo="msg#1"/>'
                '<img src="/etc/hosts"/>',
        tool_calls=None)])
    agent4 = AgentLoop(llm=llm4, registry=registry, ledger=ledger, memory_file=mem,
                       media_roots=[tmp])
    await agent4.submit(ev, responder=msent)
    await agent4.wait_idle("cli:media")
    check(msent.media == [(str(img_path.resolve()), ev.id)],
          f"图片走 send_media 且带 quote_to（实际 {msent.media}）")
    check(any("发不出去" in t for t in msent.texts), "越界路径降级为文字说明")
    m_rows = ledger.visible("cli:media", 20)
    att_rows = [e for e in m_rows if e.type == TYPE_ASSISTANT and e.attachments]
    check(len(att_rows) == 1 and att_rows[0].attachments[0].kind == "image"
          and att_rows[0].text == "", "媒体消息独立成行（不是附件，自己一个 msg#）")
    flat = "\n".join(str(m) for m in render(ledger, "cli:media"))
    check('kind="image"' in flat, "媒体行渲染带 kind/file 属性")

    # 通道不支持 → 发送失败降级文字
    fsent = Collector(ok=False)
    llm5 = StubLLM([SimpleNamespace(content=f'<img src="{img_path}"/>', tool_calls=None)])
    agent5 = AgentLoop(llm=llm5, registry=registry, ledger=ledger, memory_file=mem,
                       media_roots=[tmp])
    await agent5.submit(mk_event("再发", conv="media2"), responder=fsent)
    await agent5.wait_idle("cli:media2")
    check(any("发不出去：通道不支持" in t for t in fsent.texts), "发送失败降级为文字说明")

    print("=== 11b. 微信媒体出向（item 构造 + 加密）===")
    from neoagent.channels.weixin import ilink
    key = b"0123456789abcdef"
    ct = ilink._aes128_ecb_encrypt(b"hello wechat media", key)
    check(len(ct) % 16 == 0 and ilink._aes128_ecb_decrypt(ct, key) == b"hello wechat media",
          "AES-128-ECB 加解密对称（PKCS7）")
    item = ilink.media_item(ilink.ITEM_IMAGE, encrypt_query_param="ep", aes_key_for_api="ak",
                            ciphertext_size=32, plaintext_size=20, filename="a.png", rawfilemd5="m")
    check(item["type"] == ilink.ITEM_IMAGE
          and item["image_item"]["media"]["encrypt_query_param"] == "ep"
          and item["image_item"]["mid_size"] == 32, "image_item 构造与收包同构")
    item = ilink.media_item(ilink.ITEM_FILE, encrypt_query_param="ep", aes_key_for_api="ak",
                            ciphertext_size=32, plaintext_size=20, filename="r.pdf", rawfilemd5="m")
    check(item["file_item"]["file_name"] == "r.pdf" and item["file_item"]["len"] == "20",
          "file_item 构造")

    print("=== 12. 真值锚定与载荷计价 ===")
    anchor = TokenAnchor()
    check(anchor.predict(1_000) == 1_000, "无锚 = 纯估算")
    anchor.update(real_total=2_000, est_total=1_000)
    check(anchor.predict(1_500) == 2_500, "预估 = 上次真实 + 增量估算（锚定公式）")
    check(anchor.offset == 1_000, "残差吸收恒定开销（工具 schema/框架）")
    anchor.update(real_total=2_100, est_total=2_000)
    check(anchor.predict(2_000) == 2_100, "重新锚定后贴回真实值")
    tools_est = count_chat_payload(COUNTER, tools=[{"type": "function", "function": {
        "name": "exec", "description": "跑命令", "parameters": {"type": "object"}}}])
    turn_est = count_chat_payload(COUNTER, [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "exec", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}])
    check(tools_est > 0 and turn_est > 24, f"载荷计价：schema={tools_est}，机器件={turn_est}")
    # overhead 挤占时间线预算：同样预算下保留的行变少
    flat_free = "\n".join(str(m) for m in render(ledger, sk3, budget=1_000))
    flat_oh = "\n".join(str(m) for m in render(ledger, sk3, budget=1_000, overhead=500))
    check(flat_oh.count("<message ") < flat_free.count("<message "),
          "overhead（schema/机器件/锚定残差）挤占时间线预算")

    print("=== 13. 工作台（workspace）===")
    from neoagent.tools.file_tools import _resolve_in_roots, set_workspace
    ws = Path(tmp) / "ws"
    ws.mkdir()
    (ws / "rel.png").write_bytes(b"\x89PNG fake")
    set_workspace(ws)
    check(_resolve_in_roots("rel.png", [tmp]) == (ws / "rel.png").resolve(),
          "read/write 相对路径基准 = 工作台")
    set_workspace(None)
    wsent = Collector()
    llm6 = StubLLM([SimpleNamespace(content='<img src="rel.png"/>', tool_calls=None)])
    agent6 = AgentLoop(llm=llm6, registry=registry, ledger=ledger, memory_file=mem,
                       media_roots=[tmp], workspace=ws)
    await agent6.submit(mk_event("相对路径", conv="ws"), responder=wsent)
    await agent6.wait_idle("cli:ws")
    check(wsent.media == [(str((ws / "rel.png").resolve()), None)],
          f"媒体相对路径以工作台为基准（实际 {wsent.media}）")
    sys_msg = str(llm6.seen[0][0])          # stub 模型真实收到的 system 消息
    check("工作目录" in sys_msg and str(ws) in sys_msg, "system 附【工作目录】")

    print("=== 14. reasoning_effort 与思考草稿 ===")
    think_llm = StubLLM([
        SimpleNamespace(content='<message>想好了</message>草稿不要我',
                        tool_calls=[tc("tt1", "noop", "{}")], reasoning="我先想想"),
        SimpleNamespace(content='<message>做完了</message>',
                        tool_calls=None, reasoning="收尾思考"),
    ])
    agent7 = AgentLoop(llm=think_llm, registry=registry, ledger=ledger, memory_file=mem,
                       effort_provider=lambda: "low")
    tsent = Collector()
    await agent7.submit(mk_event("想个办法", conv="think"), responder=tsent)
    await agent7.wait_idle("cli:think")
    check(think_llm.efforts == ["low", "low"],
          f"每轮按 effort_provider 取档位（实际 {think_llm.efforts}）")
    t_rows = ledger.visible("cli:think", 20)
    check(t_rows[1].reasoning == "我先想想" and t_rows[4].reasoning == "收尾思考",
          "思考草稿挂轮首行（meta.reasoning）")
    flat = "\n".join(str(m) for m in think_llm.seen[-1])
    check("reasoning_content" in flat and "我先想想" in flat, "渲染回传 reasoning_content")
    msgs = render_messages(ledger, "cli:think", identity=IDENTITY, memory_text="",
                           budget=250_000, counter=COUNTER, include_reasoning=False)
    check("reasoning_content" not in str(msgs), "include_reasoning=False 逃生门生效")
    check("想好了" in flat and all("草稿不要我" not in (r.text or "") for r in t_rows),
          "标签外草稿不进账（当轮协议原始输出除外）")

    print("=== 15. effort 热调整（config 改了即生效）===")
    import time as _time
    from neoagent.config import ReasoningEffortSource
    cfg_path = Path(tmp) / "effort.toml"
    cfg_path.write_text('[llm]\nreasoning_effort = "none"\n', encoding="utf-8")
    src = ReasoningEffortSource(cfg_path, initial="medium")
    check(src.get() == "none", "改配置即生效（none）")
    check(callable(src) and src() == "none", "effort_provider 契约：实例可直接调用")
    _time.sleep(0.02)
    cfg_path.write_text('[llm]\nreasoning_effort = "high"\n', encoding="utf-8")
    check(src.get() == "high", "再改再生效（high）")

    print("=== 16. 图片落库与协议角色 ===")
    from neoagent.providers.vision import OpenAIImageVision
    ov = OpenAIImageVision()
    png = b"\x89PNG\r\n\x1a\n" + b"fake bytes for blob"
    sk7 = "cli:imgrole"
    ledger.add(session_key=sk7, type=TYPE_USER, sender_name="jason31416", text="用户发的图",
               attachments=[Attachment(kind="image", filename="u.png", mime="image/png",
                                       size=len(png), local_path="/tmp/不存在的路径/u.png",
                                       data=png)])
    img_seq = ledger.add(session_key=sk7, type=TYPE_ASSISTANT, sender_name="neoagent", text="",
                         attachments=[Attachment(kind="image", filename="m.png", mime="image/png",
                                                 size=10, local_path="/tmp/m.png")])
    ledger.attach_reasoning(sk7, img_seq, "发图前的思考")
    ledger.add(session_key=sk7, type=TYPE_TOOL_CALL, sender_name="neoagent",
               calls=[{"call_id": "ig1", "name": "read_file", "arguments": "{}"}])
    ledger.add(session_key=sk7, type=TYPE_TOOL_RESULT, sender_name="neoagent", call_id="ig1",
               name="read_file", text="（图片 r.png 的图像内容如下）",
               attachments=[Attachment(kind="image", filename="r.png", mime="image/png",
                                       size=len(png), local_path="/tmp/r.png", data=png)])
    msgs = render_messages(ledger, sk7, identity=IDENTITY, memory_text="",
                           budget=250_000, counter=COUNTER, vision=ov)
    parts_msgs = [m for m in msgs if isinstance(m.get("content"), list)]
    user_imgs = [m for m in parts_msgs if m.get("role") == "user"]
    tool_imgs = [m for m in parts_msgs if m.get("role") == "tool"]
    check(len(user_imgs) == 1 and len(tool_imgs) == 1,
          f"user 行带 parts、tool 信封带图（实际 user={len(user_imgs)} tool={len(tool_imgs)}）")
    a_texts = [m for m in msgs if m.get("role") == "assistant"]
    check(all(not isinstance(m.get("content"), list) for m in a_texts),
          "assistant 消息永不带图片 parts（协议限制，实测 400）")
    check(any("reasoning_content" in m for m in a_texts)
          and not any("reasoning_content" in m for m in parts_msgs),
          "思考草稿只挂 assistant 消息")
    row = ledger.visible("cli:imgrole", 50)[0]
    check(row.attachments[0].data == png,
          "字节从 blobs 还原（local_path 是不存在的路径——不依赖本地文件）")
    n_blobs = ledger._conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    check(n_blobs == 1, f"同图只存一份 blob（内容寻址去重，实际 {n_blobs}）")

    print("=== 17. read_file 读图（工具结果信封）===")
    from neoagent.tools.file_tools import read_file, set_allowed_roots
    from neoagent.tools.registry import ToolOutput
    rf_dir = Path(tmp) / "rf"
    rf_dir.mkdir()
    img = rf_dir / "shot.png"
    img.write_bytes(png)
    (rf_dir / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (rf_dir / "t.txt").write_text("hello 文本", encoding="utf-8")
    set_allowed_roots([tmp])
    out = await read_file({"path": str(img)})
    check(isinstance(out, ToolOutput) and out.images == [str(img.resolve())]
          and "图像内容如下" in out.text, "read_file 图片 → ToolOutput（图像随工具结果进上下文）")
    out2 = await read_file({"path": str(rf_dir / "blob.bin")})
    check(isinstance(out2, str) and "二进制文件读不出文本" in out2, "非图二进制明确拒绝，不吐乱码")
    out3 = await read_file({"path": str(rf_dir / "t.txt")})
    check(isinstance(out3, str) and "hello 文本" in out3, "文本文件照常读")
    (rf_dir / "x.svg").write_text("<svg><!-- 矢量图是文本 --></svg>", encoding="utf-8")
    out4 = await read_file({"path": str(rf_dir / "x.svg")})
    check(isinstance(out4, str) and "<svg>" in out4,
          "svg 按文本读源码（不塞视觉接口）")

    # 全链路：stub 模型两轮都调"看图"工具 → 信封进请求、同轮去重
    reg2 = ToolRegistry()

    async def _look(args):
        return ToolOutput(text="（图片 shot.png 的图像内容如下）", images=[str(img)])

    reg2.register("look", "看图", {"type": "object", "properties": {}}, _look)
    llm8 = StubLLM([
        SimpleNamespace(content="草稿", tool_calls=[tc("q1", "look", "{}")]),
        SimpleNamespace(content="草稿", tool_calls=[tc("q2", "look", "{}")]),
        SimpleNamespace(content='<message>看完了</message>', tool_calls=None),
    ])
    agent8 = AgentLoop(llm=llm8, registry=reg2, ledger=ledger, memory_file=mem, vision=ov)
    await agent8.submit(mk_event("看看这张图", conv="look"), responder=Collector())
    await agent8.wait_idle("cli:look")
    req = str(llm8.seen[-1])
    check("image_url" in req and "data:image/png;base64," in req,
          "工具结果信封进请求（tool 消息带 image_url data URL）")
    look_rows = [e for e in ledger.visible("cli:look", 30) if e.type == TYPE_TOOL_RESULT]
    check(len(look_rows) == 2 and look_rows[0].attachments and not look_rows[1].attachments
          and "不重复" in look_rows[1].text,
          "同轮同图去重（第二条只留说明不重复嵌）")
    set_allowed_roots([])

    print("=== 18. 图片格式保险丝（坏图绝不进上下文）===")
    from neoagent.providers.vision import normalize_image, sniff_image_mime
    ok_norm = normalize_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/*")
    check(ok_norm is not None and ok_norm[1] == "image/png",
          "魔数嗅探为准（mime 占位符/后缀不可信——飞书 .img 实锤）")
    ok_bad = normalize_image(b"NOTANIMAGE\x00\x01\x02")
    check(ok_bad is None or sniff_image_mime(ok_bad[0]) != "",
          f"异类格式：转 png 或拒绝（实际 {'转换' if ok_bad else '拒绝'}）")
    ledger.add(session_key="cli:poison", type=TYPE_USER, sender_name="x", text="坏图",
               attachments=[Attachment(kind="image", filename="bad.img", mime="image/*",
                                       size=9, local_path="", data=b"NOTANIMAGE\x00")])
    msgs = render_messages(ledger, "cli:poison", identity=IDENTITY, memory_text="",
                           budget=250_000, counter=COUNTER, vision=ov)
    check(not any(isinstance(m.get("content"), list) for m in msgs),
          "历史残留坏图自动降级（渲染保险丝，救活中毒历史）")

    print("=== 19. feishu post 富文本解析 ===")
    from neoagent.channels.feishu.adapter import _parse_post as _pp
    res = []
    lines = _pp({"title": "标题", "content": [
        [{"tag": "text", "text": "你好"}, {"tag": "at", "user_name": "阿水"}],
        [{"tag": "a", "text": "链接", "href": "https://x.y"}],
        [{"tag": "img", "image_key": "k1"}],
    ]}, res)
    check(lines[0] == "标题" and "你好@阿水" in lines[1], f"post 段落/标题/at 解析：{lines}")
    check("https://x.y" in lines[2], f"post 链接解析：{lines}")
    check(res and res[0][1] == "k1" and res[0][2] == "image", f"post 内嵌图当图片资源：{res}")
    check(_pp({"zh_cn": {"title": "", "content": [[{"tag": "text", "text": "段"}]]}}, [])[0] == "段",
          "post 语言包裹形态兼容")

    # ---- 20. R 记忆工具组（沙箱 / 切点单调） ----
    print("=== 20. R（Recorder）记忆工具组 ===")
    from neoagent.tools.memory_tools import (build_memory_registry,
                                             register_record_memory, record_memory)
    from neoagent.core.recorder import Recorder, RecorderScheduler
    memdir = Path(tmp) / "memory"
    (memdir / "old").mkdir(parents=True, exist_ok=True)
    (memdir / "memory.md").write_text("# 记忆\n- wifi 是 123", encoding="utf-8")
    (memdir / "old/旧.md").write_text("归档内容", encoding="utf-8")
    regm = build_memory_registry(ledger, "cli:r", memdir)
    check("wifi 是 123" in await regm.call("read_file", {"path": "memory.md"}),
          "read_file 读记忆（相对路径以记忆库为基准）")
    check("已更新" in await regm.call("patch_file",
          {"path": "memory.md", "old": "123", "new": "456"})
          and "456" in (memdir / "memory.md").read_text(), "patch_file 精确替换")
    check("允许范围" in await regm.call("move_file", {"src": "memory.md", "dst": "/etc/x"}),
          "move 越界拒绝（src/dst 都查）")
    check("已移动" in await regm.call("move_file", {"src": "old/旧.md", "dst": "old/新.md"})
          and (memdir / "old/新.md").exists(), "move 归档移动")
    check("已删除" in await regm.call("delete_file", {"path": "old/新.md"})
          and not (memdir / "old/新.md").exists(), "delete 真删")
    check("已推进" in await regm.call("set_cut_point", {"msg_seq": 10})
          and "未变" in await regm.call("set_cut_point", {"msg_seq": 5}),
          "切点单调只进不退（工具层反馈 + 账本层强制）")
    check(ledger.get_recorder_state("cli:r")[1] == 10, "切点落库")

    # ---- 21. Recorder 上下文构造与整理跑 ----
    print("=== 21. Recorder 上下文与整理跑 ===")
    sk8 = "cli:rflow"
    ledger.add(session_key=sk8, type=TYPE_USER, sender_name="jason31416",
               text="记住 wifi 是 123")
    ledger.add(session_key=sk8, type=TYPE_TOOL_CALL, sender_name="neoagent",
               calls=[{"call_id": "rc1", "name": "record_memory",
                       "arguments": '{"content": "wifi=123"}'},
                      {"call_id": "rc2", "name": "exec", "arguments": '{"command":"ls"}'}])
    ledger.add(session_key=sk8, type=TYPE_TOOL_RESULT, sender_name="neoagent",
               call_id="rc2", name="exec", text="big output")
    r_stub = StubLLM([SimpleNamespace(content="done", tool_calls=[], usage=None)])
    rec = Recorder(r_stub, ledger, memdir)
    ctx = rec._context(sk8, "fast", rec._window(sk8, "fast"))
    check('<file src="memory.md">' in ctx and "旧.md" not in ctx,
          "记忆文件全量喂入、old/ 不喂（索引在 memory.md 里可见）")
    check('<record seq="msg#2"' in ctx and "wifi=123" in ctx,
          "record_memory 委托内联 <record>（落在调用位置）")
    check("big output" not in ctx and "exec" not in ctx.replace("已略", ""),
          "工具调用与结果剥除（只留委托）")
    check("# 切点" in ctx and "未处理委托 1 条" in ctx, "# 注释头带元数据")
    r_stub2 = StubLLM([
        SimpleNamespace(content="", tool_calls=[SimpleNamespace(
            id="w1", name="patch_file",
            arguments='{"path":"memory.md","old":"456","new":"789"}')], usage=None),
        SimpleNamespace(content="", tool_calls=[SimpleNamespace(
            id="c1", name="set_cut_point", arguments='{"msg_seq":2}')], usage=None),
        SimpleNamespace(content="整理完毕", tool_calls=[], usage=None),
    ])
    rec2 = Recorder(r_stub2, ledger, memdir)
    report = await rec2.run(sk8, "fast")
    check("789" in (memdir / "memory.md").read_text(), "整理跑：工具循环改记忆")
    check(ledger.get_recorder_state(sk8) == (3, 2), "整理跑：r_seen_seq 推进、切点生效")
    check(report == "整理完毕", "工作简报返回（只进日志）")

    # ---- 22. record_memory 回执 + 紧急压缩钩子 ----
    print("=== 22. record_memory 与紧急压缩 ===")
    check("已记下" in await record_memory({"content": "x"}), "record_memory 固定回执")
    reg_m = ToolRegistry()
    register_record_memory(reg_m)
    check("record_memory" in [s["function"]["name"] for s in reg_m.schemas()],
          "record_memory 进 M 的工具面")

    class StubR:
        """假 R：整理时推进/不推进切点两种模式。"""
        def __init__(self, advance): self.runs = []; self.advance = advance
        async def run(self, session_key, cycle="fast"):
            self.runs.append((session_key, cycle))
            if self.advance:
                _, cut = ledger.get_recorder_state(session_key)
                ledger.set_recorder_state(session_key, cut_seq=cut + 5)
            return "ok"

    async def _compress_case(advance: bool) -> tuple:
        sk9 = f"cli:comp{int(advance)}"
        for i in range(8):
            ledger.add(session_key=sk9, type=TYPE_USER, sender_name="jason31416",
                       text=f"压{i} " + "x" * 400)
        stub_r = StubR(advance)
        llm9 = StubLLM([SimpleNamespace(
            content='<message sender="neoagent" id="msg#9">压缩后继续</message>',
            tool_calls=[], reasoning=None)])
        sent9 = Collector()
        agent9 = AgentLoop(llm=llm9, registry=registry, ledger=ledger,
                           memory_file=mem, token_budget=800)
        agent9.recorder = stub_r
        await agent9.submit(mk_event("压缩测试", conv=sk9.replace("cli:", "")), responder=sent9)
        await agent9.wait_idle(sk9)
        return sent9.texts, stub_r.runs

    texts_adv, runs_adv = await _compress_case(True)
    check(texts_adv[:2] == ["压缩上下文中", "压缩完成"]
          and "压缩后继续" in texts_adv, f"紧急压缩：播报 + turn 续跑（实际 {texts_adv}）")
    check(len(runs_adv) == 1, "R 同步跑一次（切点推进 → 重渲染继续）")
    texts_fix, runs_fix = await _compress_case(False)
    check(texts_fix[:2] == ["压缩上下文中", "压缩完成"]
          and len(runs_fix) == 1, "R 未推进切点：不死循环，走渲染端兜底")

    ledger.close()
    print("=== 全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
