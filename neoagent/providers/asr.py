"""Transcriber：语音转文字的抽象（可拆换接口）。

默认实现 XiaomiMimoASR（MiMo-V2.5-ASR，OpenAI chat 兼容形态 + input_audio parts）。
换供应商 = 新实现一个 Transcriber + 改 config 的 asr.provider，零改动核心。
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Protocol

import aiohttp

from .audio import ensure_wav

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    async def transcribe(self, audio_path: str, mime: str) -> str: ...


class XiaomiMimoASR:
    """小米 MiMo ASR（mimo-v2.5-asr）。

    协议事实（https://mimo.mi.com/docs/zh-CN/api/audio/Speech-Recognition）：
    - 端点 {base_url}/chat/completions，鉴权 Bearer 或 api-key 头；
    - user content = [{"type":"input_audio","input_audio":{"data":"data:audio/wav;base64,...","format":"wav"}}]；
    - ⚠️ 只收 mp3/wav，其他格式（微信 AMR/SILK 等）先转 wav。
    """

    def __init__(self, base_url: str, api_key: str, model: str = "mimo-v2.5-asr",
                 language: str = "auto", max_audio_seconds: int = 300) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._language = language
        self._max_seconds = max_audio_seconds

    async def transcribe(self, audio_path: str, mime: str) -> str:
        # MiMo 只收 mp3/wav：统一先转 wav（providers/audio.py，与供应商无关的工具）
        wav_path = await ensure_wav(Path(audio_path))
        data_b64 = base64.b64encode(wav_path.read_bytes()).decode()
        payload = {
            "model": self._model,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {
                        "data": f"data:audio/wav;base64,{data_b64}",
                        "format": "wav",
                    },
                }],
            }],
            "asr_options": {"language": self._language},
        }
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"MiMo ASR HTTP {resp.status}: {raw[:200]}")
                body = json.loads(raw)
                text = str(body["choices"][0]["message"].get("content") or "").strip()
                logger.info("[asr] 转写完成（%d 字符）：%s", len(text), text[:80])
                return text


def make_transcriber(provider: str, base_url: str, api_key: str, model: str,
                     language: str = "auto", max_audio_seconds: int = 300) -> Transcriber:
    """按 config 构造 Transcriber。"""
    if provider == "xiaomi-mimo":
        return XiaomiMimoASR(base_url, api_key, model, language, max_audio_seconds)
    raise ValueError(f"未知 asr provider: {provider}")
