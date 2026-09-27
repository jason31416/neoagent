"""VisionInput：图片喂给模型的抽象。

- DeepSeekFilesVision：上传 DeepSeek Files API → chat 引用 file_id（平铺格式
  {"type":"file","file_id":"file-api-..."}，官方 Vision guide 确认）。可跨轮复用。
- InlineBase64Vision：通用 OpenAI 内联 image_url（data URL），换供应商的兜底实现。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Protocol

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class VisionRef:
    """attach() 的产物——provider 不透明句柄 + 元信息。"""
    handle: str            # DeepSeek=file_id；内联=data URL
    mime: str
    history_safe: bool     # True=历史里保留引用很便宜（file_id）；False=历史里降级占位


class VisionInput(Protocol):
    async def attach(self, image_path: str) -> VisionRef: ...
    def content_part(self, ref: VisionRef) -> Dict: ...


class InlineBase64Vision:
    """通用兜底：base64 内联。任何 OpenAI 兼容视觉模型都认。"""

    async def attach(self, image_path: str) -> VisionRef:
        data = Path(image_path).read_bytes()
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        return VisionRef(handle=url, mime=mime, history_safe=False)

    def content_part(self, ref: VisionRef) -> Dict:
        return {"type": "image_url", "image_url": {"url": ref.handle}}


class DeepSeekFilesVision:
    """DeepSeek Files API 实现：POST /files 上传一次，之后按 file_id 引用。

    参考：https://api-docs.deepseek.com/api/create-file
    """

    def __init__(self, base_url: str, api_key: str,
                 expires_seconds: int | None = 2592000) -> None:
        self._url = f"{base_url.rstrip('/')}/files"
        self._api_key = api_key
        self._expires = expires_seconds

    async def attach(self, image_path: str) -> VisionRef:
        p = Path(image_path)
        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
        form = aiohttp.FormData()
        form.add_field("purpose", "user_data")
        if self._expires:
            form.add_field("expires_after[anchor]", "created_at")
            form.add_field("expires_after[seconds]", str(self._expires))
        form.add_field("file", p.open("rb"), filename=p.name, content_type=mime)

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(self._url, data=form,
                                    headers={"Authorization": f"Bearer {self._api_key}"}) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"DeepSeek /files 上传失败 HTTP {resp.status}: {raw[:200]}")
                import json
                file_id = str(json.loads(raw).get("id") or "")
                if not file_id:
                    raise RuntimeError(f"DeepSeek /files 响应缺 id: {raw[:200]}")
                logger.info("[vision] 图片已上传 file_id=%s（%s）", file_id[:16], p.name)
                return VisionRef(handle=file_id, mime=mime, history_safe=True)

    def content_part(self, ref: VisionRef) -> Dict:
        # 官方格式是平铺的 file content part，不是 OpenAI 的嵌套 {"file": {...}}
        return {"type": "file", "file_id": ref.handle}


def make_vision(provider: str, base_url: str, api_key: str,
                expires_seconds: int | None = 2592000) -> VisionInput:
    """按 config 构造 VisionInput。"""
    if provider == "deepseek-files":
        return DeepSeekFilesVision(base_url, api_key, expires_seconds)
    if provider == "inline-base64":
        return InlineBase64Vision()
    raise ValueError(f"未知 vision provider: {provider}")
