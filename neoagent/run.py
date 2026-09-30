"""服务入口：按配置启动通道，把消息接到 agent 核心。

    uv run python -m neoagent.run

配置里 channels.weixin.enabled / channels.feishu.enabled 决定起哪些通道。
所有频道共用同一个 agent 核心、同一条连续记录（每条消息标 channel）。
"""

from __future__ import annotations

import asyncio
import logging

from .app import build_agent
from .config import load_config
from .core.channel import ChannelAdapter
from .core.events import SendResult
from .log import bold_cyan, bold_green, gray, setup_logging

setup_logging()
logger = logging.getLogger("neoagent")


async def _run() -> None:
    cfg = load_config()
    agent = build_agent(cfg)
    adapters: list[ChannelAdapter] = []

    # R（Recorder）调度：停顿快周期 / 低频慢周期，静默运行
    scheduler = getattr(agent, "recorder_scheduler", None)
    if scheduler is not None:
        asyncio.get_event_loop().create_task(scheduler.run_forever())

    if cfg.weixin.enabled:
        from .channels.weixin.adapter import WeixinAdapter
        adapters.append(WeixinAdapter(cfg.data_dir, max_bytes=cfg.media.max_bytes))

    if cfg.feishu.enabled:
        from .channels.feishu.adapter import FeishuAdapter
        adapters.append(FeishuAdapter(cfg.feishu.app_id, cfg.feishu.app_secret,
                                     data_dir=cfg.data_dir, max_bytes=cfg.media.max_bytes))

    if not adapters:
        raise SystemExit("没有启用任何通道：请在 config.toml 的 [channels.*] 里打开 enabled")

    # 每频道最近一条入站消息：跨频道投递时当该频道的回复上下文（chat/引用载体）
    last_events: dict[str, object] = {}

    # 核心入口：submit 即返回；回复经 responder 按气泡的 channel 路由到对应频道
    async def on_message(event) -> None:
        logger.info("[%s] %s %s", event.channel, bold_cyan("<<"), event.text[:80])
        adapter = event_adapter(event.channel)
        last_events[event.channel] = event

        async def responder(text: str, quote_to: str | None = None,
                            media: str | None = None, channel: str | None = None,
                            _event=event, _adapter=adapter):
            """回复出口：按气泡的 channel 路由（不写 = 来的频道）。
            quote_to：M 的 replyTo → 通道级引用；media：媒体消息（<img>/<file>，独立消息）。
            返回 SendResult（message_id 回填账用）。"""
            target = channel or _event.channel
            ad = by_name.get(target)
            ev = last_events.get(target)
            if ad is None or ev is None:
                logger.warning("[%s] 频道还没有对话上下文，发不出去", target)
                return SendResult(ok=False, error=f"频道 {target} 还没有对话，发不出去")
            note = gray(f"（引用 {quote_to}）") if quote_to else ""
            if media:
                logger.info("[%s] %s %s%s", target, bold_green(">>"),
                            gray(f"（媒体 {media}）"), note)
                result = await ad.send_media(ev, media, quote_to=quote_to)
            else:
                logger.info("[%s] %s %s%s", target, bold_green(">>"), note, text)
                result = await ad.send_reply(ev, text, quote_to=quote_to)
            if not result.ok:
                logger.error("[%s] 回复失败：%s", target, result.error)
            return result

        # 空闲开 turn，忙则 steer/redirect 注入当前 turn（不起并行任务）
        await agent.submit(event, quote_fetcher=adapter.fetch_message_text,
                           responder=responder)

    by_name = {a.name: a for a in adapters}

    def event_adapter(channel: str) -> ChannelAdapter:
        return by_name[channel]

    for a in adapters:
        a.set_message_handler(on_message)

    # 连接所有通道（内部各自跑长轮询/WS），然后挂起等信号
    await asyncio.gather(*(a.connect() for a in adapters))
    logger.info("neoagent 已启动，通道：%s", ", ".join(by_name))

    try:
        await asyncio.Event().wait()  # 常驻，直到被 Ctrl-C 打断
    finally:
        # 收尾限时：哪个通道断得慢都别拖住退出
        try:
            await asyncio.wait_for(
                asyncio.gather(*(a.disconnect() for a in adapters), return_exceptions=True),
                timeout=5)
        except Exception:
            logger.exception("断开通道超时/出错，直接退出")


def main() -> None:
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到中断，退出")


if __name__ == "__main__":
    main()
