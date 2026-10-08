"""多消息攒批（settle）：入站消息不立刻开工，按"后面还有没有下文"等一小会儿，
把连发的几句攒成一批一次处理。

语义（设计见 docs/设计-多消息攒批.md）：
- **打断与继续分开**：消息一到就入账；忙时立即取消在飞的模型请求（止血——
  不让基于旧理解的话发出去），然后**等窗口**，到点 kick 才带着批内全部消息继续；
- 档位判定用 Jev（单 choice，见 _QUESTION），与等待并行，不加体感延迟：
  明确 → 无等待（判定一回来就开工）/ 可能有补充 → 4s / 话头碎片 → 15s；
- 未启用 Jev（没配 key）：固定 3~4s 随机；判定失败：按"可能有补充"档；
- 每条新消息重置窗口（最新一条定档），整批有总上限（默认 20s，从批内首条起算）；
- 指令（/ 开头）不走攒批；settle.enabled=false 全关（一切立即开工）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TIER_CLEAR = "clear"
TIER_MAYBE = "maybe"
TIER_FRAGMENT = "fragment"

_LABEL_TO_TIER = {"不会补充": TIER_CLEAR, "可能有补充": TIER_MAYBE, "话头碎片": TIER_FRAGMENT}

# Jev 单 choice 判定（带具体例子——校准就靠它们）
_QUESTION = {
    "more_coming": {
        "type": "choice",
        "instructions": (
            "判断用户这条消息发出后，接下来几十秒内还会不会继续发消息补充。分三档：\n"
            "- 不会补充：意图完整，可以直接干活或回答。"
            "例：\"帮我查下明天北京的天气\"、\"这个 KeyError 是什么意思？\"、\"把那个文件发我\"、\"还有吗\"、\"行\"、或是单纯的打招呼等\n"
            "- 可能有补充：像是开了个头，信息还不够定型，或者你不太确定。"
            "例：\"我想把桌面那个文件夹整理一下\"、\"最近项目有点乱\"、\"帮我把那个弄一下\"、\"最近好烦\"、\"那是什么东西\"、\"什么\"\n"
            "- 话头碎片：半句话的补充等，明显还有下文。"
            "例：\"对了\"、\"等等\"、\"那个……\"、\"还有\""),
        "criteria": {"不会补充": None, "可能有补充": None, "话头碎片": None},
    }
}

_TAG = re.compile(r"<[^>]+>")


class _Conv:
    """一个会话的攒批状态（按 channel+chat 各自独立窗口）。"""

    def __init__(self) -> None:
        self.texts: List[str] = []      # 本批正文（给判定当上下文）
        self.first_at = 0.0             # 批内首条到达时刻（总上限基准）
        self.seq = 0                    # 到达代数（判定回来对号，最新一条定档）
        self.bucket = ""                # 落账的 timeline bucket（kick 目标）
        self.timer: Optional[asyncio.Task] = None


class SettleGate:
    """消息入口：指令直通，其余入账后攒批，窗口到点 kick 开工。"""

    def __init__(self, agent, cfg, jev=None) -> None:
        self.agent = agent
        self.cfg = cfg
        self.jev = jev
        self._convs: Dict[str, _Conv] = {}

    # ---------------------------------------------------------------- 入口

    def push_wake(self, session_key: str, blocks_text: str,
                  responder: Optional[Callable] = None) -> None:
        """自我唤醒入口（调度器/哨兵）：落 <wake> 账 + 放行开工。

        与 push 的差别：无预处理、**不取消在飞模型**（唤醒不是用户插话）。
        会话正开着攒批窗口 → 随批一起开工（用户的话和唤醒由同一轮消化），
        否则即刻 kick。
        """
        self.agent.inject_wake(session_key, blocks_text, responder=responder)
        in_window = any(c.bucket == session_key and c.timer is not None
                        and not c.timer.done()
                        for c in self._convs.values())
        if not self.cfg.enabled or not in_window:
            self.agent.kick(session_key)

    async def push(self, event: Any, *, quote_fetcher: Optional[Callable] = None,
                   responder: Optional[Callable] = None) -> None:
        raw = (event.text or "").strip()
        if raw.startswith("/") or not self.cfg.enabled:
            await self.agent.submit(event, quote_fetcher=quote_fetcher,
                                    responder=responder)
            return
        text = await self.agent.ingest(event, quote_fetcher=quote_fetcher,
                                       responder=responder)
        key = event.session_key()
        conv = self._convs.setdefault(key, _Conv())
        now = time.time()
        conv.seq += 1
        seq = conv.seq
        conv.first_at = conv.first_at or now
        conv.bucket = self.agent.bucket_of(event)
        conv.texts.append(_TAG.sub(" ", text or raw))
        conv.texts = conv.texts[-6:]
        if self.jev is None:
            # 没启用 Jev：固定 3~4s
            wait = random.uniform(self.cfg.wait_default_min, self.cfg.wait_default_max)
            self._schedule(conv, now + wait)
        else:
            # 判定与等待并行；回来只改判"还剩多久"（窗口从到达起算）
            asyncio.get_event_loop().create_task(self._classify(conv, seq, now))

    # ---------------------------------------------------------------- 判定

    async def _classify(self, conv: _Conv, seq: int, arrival: float) -> None:
        tier = TIER_MAYBE      # 判定失败的落点（按"可能有补充"档）
        answers = await self.jev.ask("\n".join(conv.texts), _QUESTION)
        a = (answers or {}).get("more_coming") or {}
        if a:
            tier = _LABEL_TO_TIER.get(str(a.get("choice") or ""), TIER_MAYBE)
            logger.info("[settle] 判定 %s conf=%s probs=%s", tier,
                        a.get("confidence"), a.get("probabilities"))
        if seq != conv.seq:
            return             # 更新的消息已到，窗口由它接管
        wait = {TIER_CLEAR: self.cfg.wait_clear,
                TIER_MAYBE: self.cfg.wait_maybe,
                TIER_FRAGMENT: self.cfg.wait_fragment}[tier]
        self._schedule(conv, arrival + wait)

    # ---------------------------------------------------------------- 计时

    def _schedule(self, conv: _Conv, deadline: float) -> None:
        """定/改 kick 时刻：窗口从到达起算，整批不超过总上限。"""
        deadline = min(deadline, conv.first_at + self.cfg.wait_cap)
        deadline = max(deadline, time.time())    # 过期了就立刻
        if conv.timer is not None:
            conv.timer.cancel()
        conv.timer = asyncio.get_event_loop().create_task(self._fire(conv, deadline))

    async def _fire(self, conv: _Conv, deadline: float) -> None:
        await asyncio.sleep(max(0.0, deadline - time.time()))
        n = len(conv.texts)
        conv.texts = []
        conv.first_at = 0.0
        conv.timer = None
        self.agent.kick(conv.bucket)
