"""服务入口（一体化 / serve / console 三形态）与控制台接线。

    uv run python -m neoagent.run              # 一体化：服务 + 控制台（已在跑则 attach）
    uv run python -m neoagent.run serve        # 无头服务（launchd/systemd 收日志）
    uv run python -m neoagent.run console      # 只开控制台连上服务（别名 tui）

配置在 ~/.neoagent/（config.yml + channels/*.yml），NEOAGENT_HOME/--home 可指走。
所有频道共用同一个 agent 核心、同一条连续记录（每条消息标 channel）。
控制台是 socket 客户端（前端无关内核的第一客户端）：日志落文件不刷控制台，
控制台只看对话（全频道）+ 工作行（工具调用）+ 指令回执 + 需要主人动作的通知。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path

from .app import build_agent
from .config import (ChannelConfig, Config, load_channels, load_config,
                     save_channel, save_config_value)
from .console.client import run_client
from .console.transcript import ConsoleTranscript
from .control import ClientConn, ControlServer, control_alive
from .core import commands
from .core.channel import ChannelAdapter
from .core.events import Conversation, MessageEvent, SendResult, Sender
from .core.proactive import ProactiveProbe, WakeScheduler, wake_line
from .core.settle import SettleGate
from .core.skills import list_names
from .log import bold_cyan, bold_green, gray, setup_logging
from .providers.jev import JevClient

logger = logging.getLogger("neoagent")


def _make_adapter(ch: ChannelConfig, cfg: Config) -> ChannelAdapter | None:
    """按频道 kind 构造适配器（新频道类型在这里登记）。"""
    if ch.kind == "weixin":
        from .channels.weixin.adapter import WeixinAdapter
        return WeixinAdapter(cfg.data_dir, max_bytes=cfg.media.max_bytes)
    if ch.kind == "feishu":
        from .channels.feishu.adapter import FeishuAdapter
        return FeishuAdapter(str(ch.params.get("app_id") or ""),
                             str(ch.params.get("app_secret") or ""),
                             data_dir=cfg.data_dir, max_bytes=cfg.media.max_bytes)
    if ch.kind == "qq":
        from .channels.qq.adapter import QQAdapter
        return QQAdapter(str(ch.params.get("ws_url") or ""),
                         str(ch.params.get("token") or ""),
                         data_dir=cfg.data_dir, max_bytes=cfg.media.max_bytes)
    logger.warning("未知的频道类型 %s（%s.yml），跳过", ch.kind, ch.name)
    return None


_KIND_WORD = {"image": "图片", "file": "文件", "audio": "语音", "video": "视频"}


def _inbound_ev(event: MessageEvent) -> dict:
    """入站消息 → 控制台事件（媒体出占位、回复挂前缀素材）。"""
    text = event.text or ""
    counts: dict = {}
    for att in event.attachments or []:
        counts[att.kind] = counts.get(att.kind, 0) + 1
    if counts:
        ph = "、".join(f"{_KIND_WORD.get(k, k)} {n}" for k, n in counts.items())
        text = f"{text}（{ph}）" if text else f"（{ph}）"
    ev = {"kind": "msg", "channel": event.channel,
          "speaker": "你" if event.channel == "cli" else (event.sender.name or event.sender.id),
          "text": text}
    if event.reply_to is not None and event.reply_to.sender_name:
        ev["reply"] = event.reply_to.sender_name
    return ev


async def _run(mode: str, verbose: bool) -> None:
    cfg = load_config()
    sock = cfg.data_dir / "control.sock"
    log_file = cfg.home / "logs" / "latest.log"

    # ---- 只开控制台：连上服务就干活
    if mode in ("console", "tui"):
        setup_logging(file_path=log_file, stderr=verbose, color_stream=sys.stdout)
        if not control_alive(sock):
            raise SystemExit(f"服务没在跑，请先 `neoagent` 或 `neoagent serve`（{sock} not found）")
        await run_client(sock, home=str(cfg.home))
        return

    # ---- 一体化：服务已在跑 → 降级 attach（永不双开）
    if mode != "serve" and control_alive(sock):
        print("服务已经在跑，帮你连上控制台")
        await run_client(sock, home=str(cfg.home))
        return

    headless = mode == "serve" or not sys.stdin.isatty()
    if not headless:
        print("Loading neoagent TUI...")
    else:
        print("Loading neoagent server...")
    setup_logging(file_path=log_file, stderr=(headless or verbose),
                  color_stream=sys.stdout)

    agent = build_agent(cfg)
    ConsoleTranscript.drop_stored(cfg.data_dir / "neoagent.db")   # 旧版落盘转录：一次性清掉
    transcript = ConsoleTranscript()
    stop_event = asyncio.Event()

    scheduler = getattr(agent, "recorder_scheduler", None)
    if scheduler is not None:
        asyncio.get_event_loop().create_task(scheduler.run_forever())

    by_name: dict[str, ChannelAdapter] = {}    # 在跑的频道
    conn_tasks: dict[str, asyncio.Task] = {}   # 连接在飞的（扫码等待也算）
    last_events: dict[str, MessageEvent] = {}

    # ---- 控制台转录：工具工作行（M 的工具才有；R 的整理细节进日志不进控制台）。
    # 行形：时间 工具名 参数  数据字段…——无箭头无结果摘要，字段是结构化数据
    # （exec: 0.4s exit 0；写类: +38 -2；失败: err denied），数字直接表述不带量词。
    def _args_line(args: dict) -> str:
        items = list((args or {}).items())
        if len(items) == 1:
            s = str(items[0][1])
        else:
            s = " ".join(f"{k}={v}" for k, v in items)
        return s if len(s) <= 60 else s[:57] + "…"

    def tool_observer(part: str, name: str, args: dict, output) -> None:
        if part != "messenger":
            return
        if name == "run_command":
            # agent 自调指令：指令行 + 完整工具返回（与用户敲指令的回执同形）
            server.broadcast({"kind": "tool",
                              "tool": str(args.get("command") or "").strip(),
                              "args": "", "fields": []})
            text = (getattr(output, "text", "") or "").strip()
            if text:
                server.broadcast({"kind": "msg", "channel": "", "speaker": "系统",
                                  "text": text})
            return
        server.broadcast({"kind": "tool", "tool": name, "args": _args_line(args),
                          "fields": [[k, v] for k, v in (getattr(output, "fields", None) or [])]})

    agent.registry.observer = tool_observer

    # ---- 多消息攒批（settle）：判定"后面还有没有下文"，窗口到点才开工
    jev = (JevClient(cfg.jev.api_key, base_url=cfg.jev.base_url, model=cfg.jev.model)
           if cfg.jev.api_key else None)
    gate = SettleGate(agent, cfg.settle, jev=jev)

    # ---- 主动性：闹钟调度（M 排的 wake 到点注入 <wake>，说不说 M 自己定）。
    # 出口就绪 = home 频道连上（不等所有平台）；没就绪先攒着，就绪后一条消息合并补漏。
    connected: set[str] = set()

    def outbound_ready() -> bool:
        home = agent.home_channel()
        return home == "cli" or home in connected

    def rebuild_event(target: str) -> MessageEvent | None:
        """主动出站兜底：进程里还没有对话事件（刚重启）→ 从账本最近入站气泡重建。
        chat_id 一路带过去：飞书 create 直发、微信凭它查 context_token 缓存。"""
        hit = agent.ledger.last_inbound(agent.timeline_bucket or "main", channel=target)
        if hit is None or not hit.chat_id:
            return None
        return MessageEvent(id=hit.message_id or "proactive", channel=target,
                            sender=Sender(id=hit.chat_id, name=hit.sender or ""),
                            conversation=Conversation(id=hit.chat_id, type="direct"),
                            text="", timestamp=time.time(),
                            metadata={"chat_id": hit.chat_id})

    def quote_ref(quote_to: str | None, target: str) -> str:
        """出站回复前缀素材：引用那条的正文摘要（拿不到就不挂）。"""
        if not quote_to:
            return ""
        try:
            hit = agent.ledger.find_bubble(agent.timeline_bucket or "main",
                                           quote_to, channel=target)
            if hit is not None and hit.content:
                clip = hit.content.replace("\n", " ")[:12]
                return clip + ("…" if len(hit.content) > 12 else "")
        except Exception:
            pass
        return ""

    def make_responder(default_target) -> object:
        """回复出口工厂：按气泡的 channel 路由（不写 = default_target 指的频道）。
        入站回复用「来路频道」，主动出站用 home——同一个发送链。"""

        async def responder(text: str, quote_to: str | None = None,
                            media: str | None = None, channel: str | None = None):
            target = channel or (default_target() if callable(default_target)
                                 else default_target)
            reply = quote_ref(quote_to, target)
            body = text or f"（{Path(media).name}）" if media else text
            if target == "cli":
                server.broadcast({"kind": "msg", "channel": "cli", "speaker": "neoagent",
                                  "text": body, "reply": reply})
                return SendResult(ok=True)
            ad = by_name.get(target)
            ev = last_events.get(target) or rebuild_event(target)
            if ad is None or ev is None:
                logger.warning("[%s] 频道还没有对话上下文，发不出去", target)
                server.broadcast({"kind": "notice",
                                  "text": f"{target} 还没有对话，发不出去"})
                return SendResult(ok=False, error=f"频道 {target} 还没有对话，发不出去")
            note = gray(f"（引用 {quote_to}）") if quote_to else ""
            if media:
                logger.info("[%s] %s %s%s", target, bold_green(">>"),
                            gray(f"（媒体 {media}）"), note)
                result = await ad.send_media(ev, media, quote_to=quote_to)
            else:
                logger.info("[%s] %s %s%s", target, bold_green(">>"), note, text)
                result = await ad.send_reply(ev, text, quote_to=quote_to)
            if result.ok:
                server.broadcast({"kind": "msg", "channel": target, "speaker": "neoagent",
                                  "text": body, "reply": reply})
            else:
                logger.error("[%s] 回复失败：%s", target, result.error)
                server.broadcast({"kind": "notice",
                                  "text": f"{target} 回复失败：{result.error}"})
                # 发不出去的事实进时间线：M 下次对话能看到、记得补一句（不重试轰炸）
                agent.inject_internal(agent.timeline_bucket or "main",
                                      f"（{target} 频道发送失败：{result.error}；"
                                      "刚才的话没送达，下次对话记得补）")
            return result

        return responder

    # 主动出站走同一个发送链（默认发 home）：重启后 st.responder 没绑过也发得出。
    # 哨兵（Jev 自主探测）：配了 jev 才有；没配主动性只剩闹钟。
    probe = (ProactiveProbe(agent, jev, cfg.proactive,
                            bucket=agent.timeline_bucket or "main")
             if jev is not None and cfg.proactive.enabled else None)
    wake_scheduler = WakeScheduler(agent.ledger, gate,
                                   bucket=agent.timeline_bucket or "main",
                                   outbound_ready=outbound_ready,
                                   responder=make_responder(agent.home_channel),
                                   probe=probe)
    wake_task = asyncio.get_event_loop().create_task(wake_scheduler.run_forever())

    # ---- 消息进来：转录 + 入核心；回复经 responder 按气泡 channel 路由
    # 身份层：owner 判定在代码层（sender.id 与频道配置的 owner_id 精确比对），
    # 判定结果灌进 event.is_owner（定稿写 owner="1"、指令闸用它）。
    def owner_id_of(channel: str) -> str:
        ch = next((c for c in cfg.channels if c.name == channel), None)
        return str(ch.params.get("owner_id") or "") if ch is not None else ""

    def claim_owner(channel: str, sender_id: str) -> None:
        """配对成功：该频道的 owner_id 槽位换成它（每频道一个槽位，只换本频道）。"""
        _reload_channels()
        ch = next((c for c in cfg.channels if c.name == channel), None)
        if ch is not None:
            ch.params["owner_id"] = sender_id
            save_channel(ch)
        agent.ledger.set_setting(f"pair_code:{channel}", "")

    def new_pair_code(channel: str) -> str:
        """/connect 发一次性配对码（kv 存 10 分钟；重连顶掉旧码）。"""
        code = uuid.uuid4().hex[:6].upper()
        agent.ledger.set_setting(f"pair_code:{channel}",
                                 f"{code}|{time.time() + 600}")
        return code

    def try_pair(event: MessageEvent) -> bool:
        """私聊里的配对码比对（非主人私聊的唯一豁免；管道层完成不入账）。"""
        raw = agent.ledger.get_setting(f"pair_code:{event.channel}")
        if not raw:
            return False
        code, _, exp = raw.partition("|")
        if (event.text or "").strip() != code.strip():
            return False
        if time.time() >= float(exp or 0):
            agent.ledger.set_setting(f"pair_code:{event.channel}", "")
            return False            # 过期码：当普通私聊忽略
        claim_owner(event.channel, event.sender.id)
        return True

    async def on_message(event: MessageEvent) -> None:
        logger.info("[%s] %s %s", event.channel, bold_cyan("<<"), (event.text or "")[:80])
        owner_id = owner_id_of(event.channel)
        event.is_owner = event.channel == "cli" or (
            bool(owner_id) and event.sender.id == owner_id)

        # 非主人私聊：配对码是唯一豁免；其余完全忽略（不入账不回应不提示）
        if event.conversation.type == "direct" and not event.is_owner:
            if try_pair(event):
                last_events[event.channel] = event
                responder = make_responder(lambda: event.channel)
                await responder("配对完成，你是主人")
                server.broadcast({"kind": "notice",
                                  "text": f"{event.channel} 配对成功："
                                          f"{event.sender.id} 成为主人"})
                logger.info("[%s] 配对成功，主人 = %s", event.channel, event.sender.id)
            else:
                logger.info("[%s] 非主人私聊忽略 %s", event.channel, event.sender.id)
            return

        last_events[event.channel] = event
        server.broadcast(_inbound_ev(event))

        # 群聊：全量落群记录 + 活跃群的 chatagent（主 agent 侧只读查询，不进 main 桶）
        if event.conversation.type == "group":
            await agent.chatagents.on_group_message(event)
            return

        adapter = by_name.get(event.channel)
        responder = make_responder(lambda: event.channel)

        await gate.push(event,
                        quote_fetcher=(adapter.fetch_message_text if adapter else None),
                        responder=responder)

    # ---- 频道管理：/connect //disconnect 热插拔（写回 enabled = 自启开关）
    # 附件按需下载：活跃群的 chatagent / 主人私聊才拉字节；其余零 API
    def download_gate(event: MessageEvent) -> bool:
        if event.conversation.type == "group":
            return agent.chatagents.is_active(event.conversation.id)
        return event.is_owner

    async def group_sender(chat_id: str, text: str,
                           quote_to: str | None = None, media: str | None = None):
        """chatagent 的发群出口：按群找频道适配器原路发回。"""
        title = agent.ledger.get_setting(f"group_title:{chat_id}")
        for name, ad in list(by_name.items()):
            ev = last_events.get(name)
            # 找该群最近一次入站消息的频道做回复载体；没有就造一个最小载体
            if ev is None or ev.conversation.id != chat_id:
                ev = MessageEvent(
                    id="chatagent-send", channel=name,
                    sender=Sender(id="chatagent", name="chatagent"),
                    conversation=Conversation(id=chat_id, type="group",
                                              title=title),
                    text="", metadata={"chat_id": chat_id})
            if media:
                return await ad.send_media(ev, media, quote_to=quote_to)
            return await ad.send_reply(ev, text, quote_to=quote_to)
        return SendResult(ok=False, error="没有已连接的频道")

    agent.chatagents.set_sender(group_sender)

    # ---- 群记录保留期清扫（每日一次，超期行删掉；文字之外本就没存）
    async def group_log_gc() -> None:
        days = max(1, int(cfg.groups.log_retention_days))
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                n = agent.ledger.gc_group_log(time.time() - days * 86400)
                if n:
                    logger.info("[groups] 群记录清扫 %d 条（保留 %d 天）", n, days)
            except Exception:
                logger.exception("[groups] 群记录清扫出错")

    asyncio.get_event_loop().create_task(group_log_gc())

    async def start_channel(ch: ChannelConfig) -> None:
        adapter = _make_adapter(ch, cfg)
        if adapter is None:
            server.broadcast({"kind": "notice",
                              "text": f"{ch.name} 不认识的频道类型 {ch.kind}"})
            return
        adapter.name = ch.name            # 频道名 = channels/<name>.yml 的文件名
        adapter.set_message_handler(on_message)
        adapter.download_gate = download_gate   # 附件按需下载（休眠群/非主人零 API）
        if hasattr(adapter, "fetch_message_text"):
            agent.chatagents.set_quote_fetcher(adapter.fetch_message_text)
        if hasattr(adapter, "notice"):    # 扫码等提示 → 控制台通知流
            adapter.notice = (lambda s, _n=ch.name: server.broadcast(
                {"kind": "notice", "text": f"{_n} {s}"}))
        by_name[ch.name] = adapter
        server.broadcast({"kind": "notice", "text": f"{ch.name} 连接中…"})
        try:
            await adapter.connect()
            connected.add(ch.name)          # 主动出站的就绪判据（home 连上即可发）
            server.broadcast({"kind": "notice", "text": f"{ch.name} 已连接"})
        except Exception as exc:
            logger.exception("[%s] 连接失败", ch.name)
            server.broadcast({"kind": "notice", "text": f"{ch.name} 连接失败：{exc}"})
            by_name.pop(ch.name, None)
            connected.discard(ch.name)

    def _connecting(name: str) -> bool:
        t = conn_tasks.get(name)
        return t is not None and not t.done()

    def _reload_channels() -> None:
        """频道配置以文件为准：手改 yml 后 /connect //disconnect //status 立刻认新值
        （"编辑 yml 再 /connect" 的指引要真的生效）。"""
        cfg.channels = load_channels(cfg.home)
        agent.known_channels = tuple(cfg.channel_names())

    def op_connect(name: str) -> str:
        _reload_channels()
        ch = next((c for c in cfg.channels if c.name == name), None)
        if ch is None:
            if name not in ("weixin", "feishu", "qq"):
                return f"未知频道类型：{name}（可用：weixin、feishu、qq）"
            ch = ChannelConfig(name=name, kind=name, enabled=True, params={"owner": ""})
            save_channel(ch)              # /connect 顺手生成配置
            cfg.channels.append(ch)
        if name in by_name or _connecting(name):
            # 已在跑：/connect 的另一半用途是换主人——照发新码（旧码自动作废）
            code = new_pair_code(name)
            return (f"{name} 已经在跑了\n"
                    f"配对码 {code}：在 {name} **私聊**发给我（10 分钟内有效）——"
                    f"发码的人成为主人（顶掉原主人）")
        if ch.kind == "feishu" and not (ch.params.get("app_id") and ch.params.get("app_secret")):
            return f"飞书要先填凭据：编辑 {ch.path} 的 app_id/app_secret，再来 /connect {name}"
        if ch.kind == "qq" and not ch.params.get("ws_url"):
            return (f"QQ 要先填 ws_url（napcat 的 OneBot 11 WebSocket 地址，"
                    f"如 ws://1.2.3.4:32159，token 填 napcat 那边配的）："
                    f"编辑 {ch.path}，再来 /connect {name}")
        ch.enabled = True
        save_channel(ch)                  # 写入自启
        conn_tasks[name] = asyncio.get_event_loop().create_task(start_channel(ch))
        code = new_pair_code(name)        # 一次性配对码：谁先私聊发来谁是主人
        return (f"{name} 连接中（要扫码的话二维码马上来）\n"
                f"配对码 {code}：连上后在 {name} **私聊**发给我（10 分钟内有效）——"
                f"发码的人成为主人（每频道一个槽位，/connect 重来可换人）")

    def op_disconnect(name: str) -> str:
        _reload_channels()
        ch = next((c for c in cfg.channels if c.name == name), None)
        ad = by_name.pop(name, None)
        connected.discard(name)
        task = conn_tasks.pop(name, None)
        if ad is None and (task is None or task.done()):
            return f"{name} 没在跑"
        if task is not None:
            task.cancel()
        if ch is not None:
            ch.enabled = False
            save_channel(ch)              # 取消自启；登录态保留，再 /connect 免扫码

        async def _down() -> None:
            if ad is not None:
                try:
                    await asyncio.wait_for(ad.disconnect(), timeout=5)
                except Exception:
                    logger.exception("[%s] 断开出错", name)
            server.broadcast({"kind": "notice", "text": f"{name} 已断开（登录态保留）"})

        asyncio.get_event_loop().create_task(_down())
        return f"{name} 停用中（登录态保留，再 /connect 不用扫码）"

    def op_channels() -> list:
        _reload_channels()
        out = []
        for ch in cfg.channels:
            if ch.name in by_name:
                state = "已连接"
            elif _connecting(ch.name):
                state = "连接中"
            elif ch.enabled:
                state = "已启用（还没连上）"
            else:
                state = "已停用"
            out.append(f"{ch.name}（{ch.kind}）{state}")
        return out

    # ---- 控制台输入：/ 指令（回执两形态）或裸输入（channel=cli 聊天）
    def console_ctx(conn: ClientConn) -> commands.CommandContext:
        def tail_logs(n: int):
            try:
                lines = (cfg.home / "logs" / "latest.log").read_text(
                    encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
            return lines[-n:]

        async def notify_cli(text: str) -> None:
            server.broadcast({"kind": "msg", "channel": "cli", "speaker": "neoagent",
                              "text": text})

        return commands.CommandContext(
            surface="console", channel="cli",
            home=agent.ledger.get_setting("home"),
            known_channels=cfg.channel_names(),
            set_home=lambda c: agent.ledger.set_setting("home", c),
            thinking=agent.effort_provider(),
            set_thinking=lambda v: save_config_value("model.thinking", v, home=cfg.home),
            status_extra=op_channels,
            connect_channel=op_connect,
            disconnect_channel=op_disconnect,
            channels_info=op_channels,
            wakes_info=lambda: [wake_line(w) for w in agent.ledger.list_wakes(
                agent.timeline_bucket or "main")],
            skills_info=lambda: list_names(agent.skills_dir),
            compact=(lambda: agent.recorder.compact(agent.timeline_bucket or "main"))
            if getattr(agent, "recorder", None) else None,
            notify=notify_cli,
            tail_logs=tail_logs,
            reply_rows=lambda rows: server.reply_rows(conn, list(rows)),
            get_quiet=lambda: conn.quiet,
            set_quiet=lambda on: setattr(conn, "quiet", on),
            request_stop=stop_event.set)

    async def on_input(conn: ClientConn, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd = commands.lookup(text)
            receipt = commands.execute(text, console_ctx(conn))
            if not receipt:
                return
            if cmd is not None and cmd.view:
                server.reply_text(conn, receipt)       # 视图指令：只给发起者，不落转录
            else:
                server.broadcast({"kind": "msg", "channel": "cli", "speaker": "系统",
                                  "text": receipt})
            return
        # 裸输入 = 以主人身份跟 neoagent 说话（入账 channel=cli；转录经 on_message）
        event = MessageEvent(id=uuid.uuid4().hex[:12], channel="cli",
                             sender=Sender(id="cli-owner", name="owner"),
                             conversation=Conversation(id="cli", type="direct"),
                             text=text, timestamp=time.time())
        await on_message(event)

    server = ControlServer(sock, transcript, on_input=on_input)
    await server.start()

    # run_command 工具也能碰频道管理（agent 几乎全放开；审计工作行可见）
    agent.channel_ops = {"connect": op_connect, "disconnect": op_disconnect,
                         "info": op_channels}

    if cfg.fresh_install or not cfg.llm.api_key:
        server.broadcast({"kind": "notice",
                          "text": f"neoagent 首启：配置在 {cfg.home / 'config.yml'}"})
        server.broadcast({"kind": "notice",
                          "text": "三步：1) model.api_key 填进 config.yml  2) /connect weixin 扫码  3) 回来说话"})

    # ---- 控制台：一体化且有终端才内嵌（attach 形态自己起客户端）。
    # 先起控制台再连频道：频道连得慢（扫码/建连）不该挡提示符，
    # 连接进度以通知流的形式出在提示符上方。
    async def embedded_console() -> None:
        reason = await run_client(sock, home=str(cfg.home), detachable=False)
        if reason == "stop":
            stop_event.set()

    console_task = None
    if not headless:
        console_task = asyncio.get_event_loop().create_task(embedded_console())

    enabled = [ch for ch in cfg.channels
               if ch.enabled and ch.name not in by_name and not _connecting(ch.name)]
    if enabled:
        logger.info("neoagent 已启动，启用频道：%s", ", ".join(ch.name for ch in enabled))
        for ch in enabled:
            conn_tasks[ch.name] = asyncio.get_event_loop().create_task(start_channel(ch))
    else:
        server.broadcast({"kind": "notice",
                          "text": "没有启用任何频道：/connect weixin 之类接一个就能聊"})

    try:
        await stop_event.wait()
    finally:
        wake_task.cancel()
        if console_task is not None and not console_task.done():
            console_task.cancel()
        for t in list(conn_tasks.values()):
            t.cancel()
        await server.stop()
        # 收尾限时：哪个频道断得慢都别拖住退出
        try:
            await asyncio.wait_for(
                asyncio.gather(*(a.disconnect() for a in list(by_name.values())),
                               return_exceptions=True),
                timeout=5)
        except Exception:
            logger.exception("断开频道超时/出错，直接退出")

def _version() -> str:
    try:
        from importlib.metadata import version
        return version("neoagent")
    except Exception:
        return "?"

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="neoagent",
        description="neoagent：你的个人助手服务")
    parser.add_argument("mode", nargs="?", default="",
                        choices=["", "serve", "console", "tui"],
                        help="serve=无头服务 / console(tui)=只开控制台 / 不写=一体化")
    parser.add_argument("--home", default=None,
                        help="配置家目录（默认 ~/.neoagent，或环境变量 NEOAGENT_HOME）")
    parser.add_argument("--verbose", action="store_true",
                        help="日志同时打进终端（开发用；默认日志只落文件）")
    args = parser.parse_args()
    if args.home:
        import os
        os.environ["NEOAGENT_HOME"] = str(Path(args.home).expanduser())
    try:
        asyncio.run(_run(args.mode, args.verbose))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
