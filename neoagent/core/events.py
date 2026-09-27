"""消息归一化模型。

所有 IM 通道（微信 / 飞书 / CLI）收进来的消息都先转成 MessageEvent，
agent 核心只认这一种结构 —— 这是"一个核心挂多通道"的关键解耦点。
产品文档里 M（唯一的声音）消费的就是 MessageEvent。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional


@dataclass
class Sender:
    """消息发送者。id 是通道内的稳定标识（微信 open id / 飞书 open_id / CLI 固定值）。"""
    id: str
    name: str = ""


@dataclass
class Conversation:
    """会话：direct=单聊，group=群聊。"""
    id: str
    type: str = "direct"          # "direct" | "group"
    title: str = ""


@dataclass
class Attachment:
    """一个附件。随消息来的带 local_path；引用占位的只有元数据（local_path 空）。

    provider_ref 是 provider 层的不透明句柄（如 DeepSeek file_id），
    由核心在入库时填，通道不感知。
    """
    kind: str                     # "image" | "file" | "audio" | "video"
    filename: str
    mime: str = ""
    size: int = 0
    local_path: str = ""
    provider_ref: Any = None      # VisionInput.attach() 的产物，其余类型恒 None


@dataclass
class ReplyRef:
    """被引用的那条消息（引用回复）。

    设计原则：能指认就不搬运——seq_id 命中账本/在窗口内时渲染指针，
    否则渲染内联文本。seq_id 由核心在入库时解析填入。
    """
    message_id: str
    sender_name: str = ""
    timestamp: float = 0.0
    text: str = ""                # 可拿到的原文（内联/API/账本补），拿不到为空
    attachments: List[Attachment] = field(default_factory=list)  # 只出元数据占位
    resolved_from: str = "unknown"  # "ledger" | "inline" | "api" | "unknown"
    seq_id: Optional[int] = None    # 账本命中时的短序号（渲染指针用）


@dataclass
class MessageEvent:
    """一条入站消息（已归一化）。

    channel     通道名："weixin" / "feishu" / "cli"
    text        纯文本内容（语音已被转写拼入）
    timestamp   Unix 时间戳（秒）
    metadata    通道私有信息，例如微信的 context_token（回复时必须回带）
    """
    id: str
    channel: str
    sender: Sender
    conversation: Conversation
    text: str
    timestamp: float = field(default_factory=time.time)
    attachments: List[Attachment] = field(default_factory=list)
    reply_to: Optional[ReplyRef] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def session_key(self) -> str:
        """会话键：同一通道同一会话共享一段对话历史。"""
        return f"{self.channel}:{self.conversation.id}"

    def formatted_time(self, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """本地时间字符串。产品文档要求每条消息带时间戳——渲染时用它标注。"""
        return datetime.fromtimestamp(self.timestamp).strftime(fmt)


@dataclass
class SendResult:
    """send_reply 的结果。ok=False 时 error 里是失败原因。"""
    ok: bool = True
    error: Optional[str] = None
