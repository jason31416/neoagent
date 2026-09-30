"""指令解析器：/ 开头的消息不进模型，解析器直接回**系统消息**（不装模型语气）。

指令本身与回执都是真气泡（用户消息 + system 消息各一条、都能被引用），
只是不触发模型回合——模型下次在历史里看到"用户一条、system 答一条"自然理解。
新增指令在 execute 里登记即可；本期只有 /home。
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

USAGE = "/home [channel]  设置 home 频道（主动消息默认发这里；无参 = 设为当前频道）"


def execute(text: str, *, channel: str, home: str = "",
            known_channels: Sequence[str] = (),
            set_home: Optional[Callable[[str], None]] = None) -> str:
    """执行一条指令，返回系统回执文本（人话、系统口吻）。"""
    parts = (text or "").strip().split()
    name = parts[0].lstrip("/").lower() if parts else ""
    args = parts[1:]
    if name == "home":
        return _home(args, channel=channel, home=home,
                     known_channels=known_channels, set_home=set_home)
    return f"未知指令：/{name}（可用：{USAGE}）"


def _home(args: list, *, channel: str, home: str,
          known_channels: Sequence[str],
          set_home: Optional[Callable[[str], None]]) -> str:
    target = args[0] if args else channel
    if known_channels and target not in known_channels:
        return (f"未知频道：{target}（可用：{'、'.join(known_channels)}）"
                f"；home 现在是 {home or '（未设置）'}")
    if set_home is not None:
        set_home(target)
    return f"home 现在是 {target}（主动消息默认发这里）"
