"""VisionInput：图片喂给模型的抽象（provider 适配层，硬性规则）。

唯一实现是 OpenAI 标准格式：content 数组里的 image_url + base64 data URL
（实测 DeepSeek 认，与 file_id 计费完全相同——Files API 是传输优化不是能力，
为供应商统一已移除；将来换非 OpenAI 系供应商时在此加实现）。
"""

from __future__ import annotations

import base64
from typing import Dict, Protocol, runtime_checkable


@runtime_checkable
class VisionInput(Protocol):
    """图片字节 → 协议 content part。纯本地编码，无网络、永不失败。"""

    def content_part(self, data: bytes, mime: str) -> Dict: ...


class OpenAIImageVision:
    """OpenAI 兼容 image_url + data URL（任何兼容视觉模型都认）。"""

    def content_part(self, data: bytes, mime: str) -> Dict:
        url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode()}"
        return {"type": "image_url", "image_url": {"url": url}}


def make_vision() -> VisionInput:
    """构造 VisionInput（目前只有 OpenAI 标准实现；抽象保留供换供应商）。"""
    return OpenAIImageVision()
