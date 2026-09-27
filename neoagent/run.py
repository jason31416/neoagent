"""服务入口：按配置启动通道，把消息接到 agent 核心。

    uv run python -m neoagent.run

配置里 channels.weixin.enabled / channels.feishu.enabled 决定起哪些通道。
两个通道共用同一个 agent 核心（同一段对话历史、同一套工具）。
"""

from __future__ import annotations

import asyncio
import logging

from .app import build_agent
from .config import load_config
from .core.channel import ChannelAdapter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("neoagent")


async def _run() -> None:
    cfg = load_config()
    agent = build_agent(cfg)
    adapters: list[ChannelAdapter] = []

    if cfg.weixin.enabled:
        from .channels.weixin.adapter import WeixinAdapter
        adapters.append(WeixinAdapter(cfg.data_dir, max_bytes=cfg.media.max_bytes))

    if cfg.feishu.enabled:
        from .channels.feishu.adapter import FeishuAdapter
        adapters.append(FeishuAdapter(cfg.feishu.app_id, cfg.feishu.app_secret,
                                     data_dir=cfg.data_dir, max_bytes=cfg.media.max_bytes))

    if not adapters:
        raise SystemExit("没有启用任何通道：请在 config.toml 的 [channels.*] 里打开 enabled")

    # 核心入口：submit 即返回；回复经 responder 由原通道发回
    async def on_message(event) -> None:
        logger.info("[%s] 收到：%s", event.channel, event.text[:80])
        adapter = event_adapter(event.channel)

        async def responder(text: str, _event=event, _adapter=adapter) -> None:
            """回复出口：M 的话经原通道发回（忙时回执也走这里）。"""
            logger.info("[%s] 回复 >>>\n%s", _event.channel, text)
            result = await _adapter.send_reply(_event, text)
            if not result.ok:
                logger.error("[%s] 回复失败：%s", _event.channel, result.error)

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
        await asyncio.gather(*(a.disconnect() for a in adapters), return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("收到中断，退出")


if __name__ == "__main__":
    main()
