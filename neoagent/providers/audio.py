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

# QQ（以及部分微信）的语音是 SILK 格式，ffmpeg 不认，需要先用 pilk 解码成 wav。
_SILK_MAGIC = (b"\x02#!SILK", b"#!SILK")


def _is_silk(src: Path) -> bool:
    try:
        with open(src, "rb") as fh:
            head = fh.read(10)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in _SILK_MAGIC)


async def _decode_silk(src: Path, dst: Path) -> bool:
    """用 pilk 把 SILK 解成 wav；不可用/失败时返回 False，让调用方回退 ffmpeg。"""
    try:
        from pilk import silk_to_wav
    except Exception:  # pragma: no cover - 没装 pilk 就静默回退
        return False
    try:
        await asyncio.wait_for(
            asyncio.to_thread(silk_to_wav, str(src), str(dst), 24000), timeout=60
        )
    except Exception as exc:
        logger.debug("[audio] SILK 解码失败：%s（%s），回退 ffmpeg", src.name, exc)
        return False
    if dst.exists():
        logger.debug("[audio] 已解码 SILK：%s -> %s", src.name, dst.name)
        return True
    return False


async def ensure_wav(src: Path) -> Path:
    """确保音频是 wav：已是 .wav 直接返回，否则 ffmpeg 转一份（放在同目录）。
    转换失败/没有 ffmpeg 时抛 RuntimeError（转写降级为占位）。"""
    if src.suffix.lower() == ".wav":
        return src

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("缺少 ffmpeg，无法转码音频（brew install ffmpeg）")

    dst = src.with_suffix(".wav")

    if _is_silk(src) and await _decode_silk(src, dst):
        return dst

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
