"""共享音频工具：把任意音频转成 wav（ASR 供应商的通用预处理）。

微信语音是 AMR/SILK、飞书语音是 opus/m4a——各家 ASR 可接受的格式不同，
统一转 wav 是最稳的公共语言。这里与供应商无关，只是 ffmpeg 的薄封装。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


async def ensure_wav(src: Path) -> Path:
    """确保音频是 wav：已是 .wav 直接返回，否则 ffmpeg 转一份（放在同目录）。
    转换失败/没有 ffmpeg 时抛 RuntimeError（转写降级为占位）。"""
    if src.suffix.lower() == ".wav":
        return src

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("缺少 ffmpeg，无法转码音频（brew install ffmpeg）")

    dst = src.with_suffix(".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"音频转码超时：{src.name}")
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"音频转码失败：{src.name}")
    logger.debug("[audio] 已转 wav：%s -> %s", src.name, dst.name)
    return dst
