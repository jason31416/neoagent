"""忙时输入冒烟：工具中 steer / 生成中 redirect（临时脚本，验证完可删）。"""
import asyncio
import uuid

from neoagent.app import build_agent
from neoagent.config import load_config
from neoagent.core.events import Conversation, MessageEvent, Sender


def mk(text: str) -> MessageEvent:
    return MessageEvent(
        id=uuid.uuid4().hex[:12], channel="cli",
        sender=Sender(id="u", name="测试"),
        conversation=Conversation(id="smoke", type="direct"), text=text)


async def main() -> None:
    cfg = load_config()
    agent = build_agent(cfg)

    async def printer(t: str, quote_to: str | None = None) -> None:
        print(f"\n>>> {'（引用 ' + quote_to + '）' if quote_to else ''}{t}\n", flush=True)

    print("=== 用例 1：工具执行中插话（预期静默 steer：无回执，纠正进下一步）===")
    await agent.submit(mk("用 exec 工具跑 `sleep 8; echo done`，跑完告诉我"), responder=printer)
    await asyncio.sleep(5)          # 模型应该已经选了 exec，工具在跑
    await agent.submit(mk("等等，不用等它了，后面直接说'收到'就行"), responder=printer)
    await agent.wait_idle("cli:smoke")

    print("=== 用例 2：生成中插话（预期静默 redirect：无回执，改答新问题）===")
    await agent.submit(mk("写一首关于海的长诗，至少二十行"), responder=printer)
    await asyncio.sleep(0.5)        # 模型请求应该在飞
    await agent.submit(mk("别写诗了，改成一句话说海是什么"), responder=printer)
    await agent.wait_idle("cli:smoke")

    print("=== 用例 3：连环插话不丢（预期都进上下文）===")
    await agent.submit(mk("用 exec 跑 `sleep 6; echo done`，跑完总结我一共说了几句话"), responder=printer)
    await asyncio.sleep(4)
    await agent.submit(mk("第一句插话：苹果"), responder=printer)
    await asyncio.sleep(0.5)
    await agent.submit(mk("第二句插话：香蕉"), responder=printer)
    await agent.wait_idle("cli:smoke")
    print("=== 全部完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
