"""VisionInput：图片喂给模型的抽象（provider 适配层，硬性规则）。

唯一实现是 OpenAI 标准格式：content 数组里的 image_url + base64 data URL
（实测 DeepSeek 认，与 file_id 计费完全相同——Files API 是传输优化不是能力，
为供应商统一已移除；将来换非 OpenAI 系供应商时在此加实现）。
"""

from __future__ import annotations

import base64
import logging
import subprocess
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)

# DeepSeek（OpenAI 系视觉）只认这四种
SUPPORTED_IMAGE_MIMES = ("image/webp", "image/png", "image/jpeg", "image/gif")


@runtime_checkable
class VisionInput(Protocol):
    """图片字节 → 协议 content part。纯本地编码，无网络、永不失败。"""

    def content_part(self, data: bytes, mime: str) -> Dict: ...


def sniff_image_mime(data: bytes) -> str:
    """按魔数嗅探真实图片格式；嗅不出返回 ""（mime 字符串/文件名不可信）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def normalize_image(data: bytes, mime: str = "") -> Optional[Tuple[bytes, str]]:
    """归一化到支持格式（webp/png/jpeg/gif）。

    - 魔数嗅探为准（飞书的 mime 是 "image/*" 占位、后缀 .img，全不可信）；
    - 嗅不出的真异类（heic/bmp/svg…）尝试 magick 转 png（可选外部命令，没有就放弃）；
    - 转不了返回 None——**不支持的图绝不进上下文**（一张坏图会让之后每个请求 400）。"""
    sniffed = sniff_image_mime(data)
    if sniffed:
        return data, sniffed
    png = _magick_to_png(data)
    if png and sniff_image_mime(png) == "image/png":
        logger.info("图片已转换为 png（原 mime=%s）", mime or "未知")
        return png, "image/png"
    return None


def _magick_to_png(data: bytes) -> Optional[bytes]:
    """可选的格式转换（ImageMagick）；没装/转不动就放弃。"""
    for exe in ("magick", "convert"):
        try:
            proc = subprocess.run([exe, "-", "png:-"], input=data,
                                  capture_output=True, timeout=20)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


class OpenAIImageVision:
    """OpenAI 兼容 image_url + data URL（任何兼容视觉模型都认）。"""

    def content_part(self, data: bytes, mime: str) -> Dict:
        url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode()}"
        return {"type": "image_url", "image_url": {"url": url}}


def make_vision() -> VisionInput:
    """构造 VisionInput（目前只有 OpenAI 标准实现；抽象保留供换供应商）。"""
    return OpenAIImageVision()
