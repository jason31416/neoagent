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
from .log import bold_cyan, bold_green, gray, setup_logging

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
            raise SystemExit(f"服务没在跑：先 `neoagent` 或 `neoagent serve`（{sock} 不通）")
        await run_client(sock, home=str(cfg.home))
        return

    # ---- 一体化：服务已在跑 → 降级 attach（永不双开）
    if mode != "serve" and control_alive(sock):
        print("服务已经在跑，帮你连上控制台。")
        await run_client(sock, home=str(cfg.home))
        return

    headless = mode == "serve" or not sys.stdin.isatty()
    setup_logging(file_path=log_file, stderr=(headless or verbose),
                  color_stream=sys.stdout)

    agent = build_agent(cfg)
    transcript = ConsoleTranscript(cfg.data_dir / "neoagent.db")
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

    # ---- 消息进来：转录 + 入核心；回复经 responder 按气泡 channel 路由
    async def on_message(event: MessageEvent) -> None:
        logger.info("[%s] %s %s", event.channel, bold_cyan("<<"), (event.text or "")[:80])
        last_events[event.channel] = event
        server.broadcast(_inbound_ev(event))
        adapter = by_name.get(event.channel)

        def quote_ref(quote_to: str | None, target: str) -> str:
            """出站回复前缀素材：引用那条的正文摘要（拿不到就不挂）。"""
            if not quote_to:
                return ""
            try:
                hit = agent.ledger.find_bubble(agent.bucket_of(event), quote_to, channel=target)
                if hit is not None and hit.content:
                    clip = hit.content.replace("\n", " ")[:12]
                    return clip + ("…" if len(hit.content) > 12 else "")
            except Exception:
                pass
            return ""

        async def responder(text: str, quote_to: str | None = None,
                            media: str | None = None, channel: str | None = None,
                            _event=event):
            """回复出口：按气泡的 channel 路由（不写 = 来的频道）。"""
            target = channel or _event.channel
            reply = quote_ref(quote_to, target)
            body = text or f"（{Path(media).name}）" if media else text
            if target == "cli":
                server.broadcast({"kind": "msg", "channel": "cli", "speaker": "neoagent",
                                  "text": body, "reply": reply})
                return SendResult(ok=True)
            ad = by_name.get(target)
            ev = last_events.get(target)
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
            return result

        await agent.submit(event,
                           quote_fetcher=(adapter.fetch_message_text if adapter else None),
                           responder=responder)

    # ---- 频道管理：/connect //disconnect 热插拔（写回 enabled = 自启开关）
    async def start_channel(ch: ChannelConfig) -> None:
        adapter = _make_adapter(ch, cfg)
        if adapter is None:
            server.broadcast({"kind": "notice",
                              "text": f"{ch.name} 不认识的频道类型 {ch.kind}"})
            return
        adapter.name = ch.name            # 频道名 = channels/<name>.yml 的文件名
        adapter.set_message_handler(on_message)
        if hasattr(adapter, "notice"):    # 扫码等提示 → 控制台通知流
            adapter.notice = (lambda s, _n=ch.name: server.broadcast(
                {"kind": "notice", "text": f"{_n} {s}"}))
        by_name[ch.name] = adapter
        try:
            await adapter.connect()
            server.broadcast({"kind": "notice", "text": f"{ch.name} 已连接"})
        except Exception as exc:
            logger.exception("[%s] 连接失败", ch.name)
            server.broadcast({"kind": "notice", "text": f"{ch.name} 连接失败：{exc}"})
            by_name.pop(ch.name, None)

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
            if name not in ("weixin", "feishu"):
                return f"未知频道类型：{name}（可用：weixin、feishu）"
            ch = ChannelConfig(name=name, kind=name, enabled=True, params={"owner": ""})
            save_channel(ch)              # /connect 顺手生成配置
            cfg.channels.append(ch)
        if name in by_name or _connecting(name):
            return f"{name} 已经在跑了"
        if ch.kind == "feishu" and not (ch.params.get("app_id") and ch.params.get("app_secret")):
            return f"飞书要先填凭据：编辑 {ch.path} 的 app_id/app_secret，再来 /connect {name}"
        ch.enabled = True
        save_channel(ch)                  # 写入自启
        conn_tasks[name] = asyncio.get_event_loop().create_task(start_channel(ch))
        return f"{name} 连接中（要扫码的话二维码马上来）"

    def op_disconnect(name: str) -> str:
        _reload_channels()
        ch = next((c for c in cfg.channels if c.name == name), None)
        ad = by_name.pop(name, None)
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
            tail_logs=tail_logs,
            tail_history=transcript.recent,
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
                             sender=Sender(id="cli-owner", name="主人"),
                             conversation=Conversation(id="cli", type="direct"),
                             text=text, timestamp=time.time())
        await on_message(event)

    server = ControlServer(sock, transcript, on_input=on_input)
    await server.start()

    # run_command 工具也能碰频道管理（agent 几乎全放开；审计工作行可见）
    agent.channel_ops = {"connect": op_connect, "disconnect": op_disconnect,
                         "info": op_channels}

    if cfg.fresh_install or not cfg.llm.api_key:
        print(f"neoagent 首启：配置在 {cfg.home / 'config.yml'}")
        print("三步：1) model.api_key 填进 config.yml  2) /connect weixin 扫码  3) 回来说话")

    for ch in cfg.channels:
        if ch.enabled and ch.name not in by_name and not _connecting(ch.name):
            conn_tasks[ch.name] = asyncio.get_event_loop().create_task(start_channel(ch))
    if conn_tasks:
        await asyncio.gather(*conn_tasks.values())
        logger.info("neoagent 已启动，频道：%s", ", ".join(by_name) or "（无）")
    else:
        server.broadcast({"kind": "notice",
                          "text": "没有启用任何频道：/connect weixin 之类接一个就能聊"})

    # ---- 控制台：一体化且有终端才内嵌（attach 形态自己起客户端）
    async def embedded_console() -> None:
        reason = await run_client(sock, home=str(cfg.home), detachable=False)
        if reason == "stop":
            stop_event.set()

    console_task = None
    if not headless:
        console_task = asyncio.get_event_loop().create_task(embedded_console())

    try:
        await stop_event.wait()
    finally:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="neoagent",
        description="neoagent：IM（微信/飞书）+ 工具调用闭环的个人助手服务")
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
