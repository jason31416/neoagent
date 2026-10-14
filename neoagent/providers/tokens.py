"""Token 计数抽象（provider 适配层，硬性规则：供应商相关的东西都走抽象）。

默认实现是零依赖的**保守估算**（宁高勿低，别把上下文撑爆）：
- CJK 字符 ≈ 1 token/字；
- 其余（ASCII/符号/空白）≈ 4 字符/token（向上取整）；
- 图片按字节估算（bytes/150，贴近视觉接口按像素计费的量级；拿不到字节回退可配置常数）。

要精确可换实现（tiktoken / 供应商官方 tokenizer），核心与渲染层只认接口。
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Protocol, runtime_checkable

_CJK_RE = re.compile(r"[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


@runtime_checkable
class TokenCounter(Protocol):
    """文本/图片 → token 估算。实现必须单调、宁高勿低。"""

    def count(self, text: str) -> int: ...

    def count_image(self, nbytes: int = 0) -> int: ...


class HeuristicTokenCounter:
    """零依赖保守估算（默认实现）。"""

    def __init__(self, image_tokens: int = 1024) -> None:
        self._image_tokens = int(image_tokens)

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK_RE.findall(text))
        rest = len(text) - cjk
        return cjk + (rest + 3) // 4

    def count_image(self, nbytes: int = 0) -> int:
        """图片估算：按字节贴近视觉接口的真实计费（bytes/150，封顶 8k）；
        没有字节信息（老数据/外链）回退配置常数。"""
        if nbytes > 0:
            return max(256, min(int(nbytes) // 150, 8000))
        return self._image_tokens


class TokenAnchor:
    """真值锚定：拿接口回的真实 prompt_tokens 当基线，只对**增减量**做估算。

        预估 = 上次真实 + (本次本地估算 − 上次本地估算)

    每次拿到真实用量就重新锚定——恒定开销（工具 schema、系统提示、JSON 框架）
    被真实值自动吸收，误差只来自相邻两次请求之间的增量估算，且不累积。
    """

    def __init__(self) -> None:
        self.last_real: Optional[int] = None   # 上次请求真实 prompt_tokens
        self.last_est: int = 0                 # 上次请求的本地估算总量

    @property
    def offset(self) -> int:
        """当前锚定残差（本地估算总量之外还需补的量；无锚时为 0）。"""
        if self.last_real is None:
            return 0
        return max(self.last_real - self.last_est, 0)

    def predict(self, est_total: int) -> int:
        return est_total + self.offset

    def update(self, real_total: int, est_total: int) -> None:
        self.last_real = int(real_total)
        self.last_est = int(est_total)


def count_chat_payload(counter: TokenCounter,
                       messages: Optional[List[dict]] = None,
                       tools: Optional[List[dict]] = None) -> int:
    """估算一次请求的协议载荷：消息（含 tool_calls/tool 机器件）+ 工具 schema + JSON 框架。"""
    total = 0
    if tools:
        try:
            total += counter.count(json.dumps(tools, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    for m in messages or []:
        total += 12                      # role / 键名 / 括号框架
        c = m.get("content")
        if isinstance(c, str):
            total += counter.count(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        total += counter.count(str(p.get("text") or ""))
                    else:
                        url = str((p.get("image_url") or {}).get("url") or "")
                        nb = 0
                        if url.startswith("data:") and "," in url:
                            nb = (len(url) - url.index(",") - 1) * 3 // 4
                        total += counter.count_image(nb)
        elif isinstance(c, dict):
            total += counter.count(json.dumps(c, ensure_ascii=False))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += 24 + counter.count(str(fn.get("name") or "")) \
                       + counter.count(str(fn.get("arguments") or ""))
    return total
