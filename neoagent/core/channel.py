"""通道抽象基类。

参考 hermes 的 BasePlatformAdapter，砍到最小集：
connect / disconnect / set_message_handler / send_reply。
每个 IM 适配器实现这四件事，把消息归一化成 MessageEvent 丢给 handler，
agent 回复时调 send_reply 原路发回。

将来通道可以是插件化（hermes 的 plugin.yaml + register 范式），
本期两个内置通道够用，先把接口定对。
"""

from __future__ import annotations

import abc
import logging
from typing import Awaitable, Callable, Optional

from .events import MessageEvent, SendResult

logger = logging.getLogger(__name__)

# 核心收到 MessageEvent 后的回调（agent_loop.handle 就挂在这里）
MessageHandler = Callable[[MessageEvent], Awaitable[None]]


class ChannelAdapter(abc.ABC):
    """IM 通道适配器接口。"""

    #: 通道名，会写进 MessageEvent.channel 与日志
    name: str = "abstract"

    def __init__(self) -> None:
        self._handler: Optional[MessageHandler] = None
        # 按需下载钩子：(事件, 附件类型) → 要不要下载字节。返回 False 的附件
        # 零下载（只留元数据）。典型策略：群里只下载图片（文件/语音/视频不落盘），
        # 私聊只下载主人的。None = 全下载（默认）。
        self.download_gate: Optional[Callable[[MessageEvent, str], bool]] = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        """注册消息回调。核心启动时把 agent_loop.handle 挂进来。"""
        self._handler = handler

    async def _dispatch(self, event: MessageEvent) -> None:
        """适配器内部用：收到消息后转给核心。"""
        if self._handler is None:
            logger.warning("[%s] 没有注册 message handler，丢弃消息 %s", self.name, event.id)
            return
        try:
            await self._handler(event)
        except Exception:
            # 核心处理出错不应该把通道拖死，记日志继续跑
            logger.exception("[%s] 处理消息 %s 时出错", self.name, event.id)

    @abc.abstractmethod
    async def connect(self) -> None:
        """建立连接并开始收消息（长轮询 / WebSocket / webhook 均可）。
        该方法可以长期挂着（内部跑事件循环），也可以立即返回、后台收消息。"""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """干净断开。"""

    @abc.abstractmethod
    async def send_reply(self, event: MessageEvent, text: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """把 agent 的回复发回原会话。event 携带通道回复所需的私有信息
        （例如微信的 context_token、飞书的 chat_id）。

        quote_to：要引用的平台消息 id（M 的 replyTo 语义落到通道 UI）；
        不支持引用的通道可忽略它发普通消息。返回的 SendResult.message_id
        是这条发出消息的平台 id（核心回填进账，供以后被引用）。"""

    async def send_media(self, event: MessageEvent, path: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """把图片/文件作为**独立消息**发回原会话（协议的 <img>/<file> 标签落到这里）。
        不支持的通道返回 ok=False，核心降级为文本说明。"""
        return SendResult(ok=False, error=f"{self.name} 通道不支持发送媒体")

    async def fetch_message_text(self, message_id: str) -> Optional[str]:
        """按平台消息 id 取消息文本（引用解析的最后一级兜底）。
        默认不支持（None）；飞书实现它（message.get），微信的 ref_msg 内联自带文本用不到。"""
        return None
