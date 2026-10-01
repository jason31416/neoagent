"""指令层：一套注册表，两个前端（聊天 / 控制台），agent 也能跑（run_command 工具）。

语法处处一致：/ 开头 = 指令，裸输入 = 聊天。
指令与回执在聊天里是**真气泡**（用户消息 + system 消息各一条、可被引用），
在控制台里是输入行 + 打印行；都不触发模型回合。
agent 跑指令走同一注册表、同一权限档（agent_ok），是工具调用、不产生用户气泡。
新增指令在 _install_defaults 里 register 一条即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# 思考档位合法值（OpenAI 标准 reasoning_effort；"" = 不传用服务端默认）
VALID_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max")


@dataclass
class CommandContext:
    """一次指令执行的上下文：两个前端各填各的字段。"""
    surface: str = "chat"                                   # chat | console
    channel: str = ""                                       # 指令来自哪个频道（/home 无参的目标）
    home: str = ""                                          # 当前 home
    known_channels: Sequence[str] = ()
    set_home: Optional[Callable[[str], None]] = None
    thinking: str = ""                                      # 当前思考档位
    set_thinking: Optional[Callable[[str], bool]] = None     # 落盘改档（保注释写 config.yml）
    status_extra: Optional[Callable[[], Sequence[str]]] = None   # /status 追加行（后端接状态）
    agent_run: bool = False                                 # run_command 工具跑的：走权限档


@dataclass
class Command:
    name: str
    help: str                                               # 一行用法（/help 展示）
    handler: Callable[[List[str], CommandContext], str]
    surfaces: Tuple[str, ...] = ("chat", "console")
    agent_ok: bool = True


COMMANDS: Dict[str, Command] = {}


def register(name: str, help: str,
             handler: Callable[[List[str], CommandContext], str],
             *, surfaces: Tuple[str, ...] = ("chat", "console"),
             agent_ok: bool = True) -> None:
    """登记一条指令（名字不带斜线）。"""
    COMMANDS[name] = Command(name=name, help=help, handler=handler,
                             surfaces=surfaces, agent_ok=agent_ok)


def available(ctx: CommandContext) -> List[Command]:
    """当前前端（或 agent）能用的指令。"""
    if ctx.agent_run:
        return [c for c in COMMANDS.values() if c.agent_ok]
    return [c for c in COMMANDS.values() if ctx.surface in c.surfaces]


def usage(ctx: CommandContext) -> str:
    return "\n".join(f"/{c.name}  {c.help}" for c in available(ctx))


def execute(text: str, ctx: CommandContext) -> str:
    """执行一条指令，返回回执文本（人话、系统口吻，不装模型语气）。"""
    parts = (text or "").strip().split()
    name = parts[0].lstrip("/").lower() if parts else ""
    args = parts[1:]
    cmd = COMMANDS.get(name)
    if cmd is None:
        return f"未知指令：/{name}（试试 /help）"
    if ctx.agent_run:
        if not cmd.agent_ok:
            return f"这个指令我不能自己跑：/{name}（要主人亲手来）"
    elif ctx.surface not in cmd.surfaces:
        return f"这个指令不能在这里用：/{name}（试试 /help）"
    return cmd.handler(args, ctx)


# ----------------------------------------------------------------------
# 默认指令
# ----------------------------------------------------------------------

def _help(args: List[str], ctx: CommandContext) -> str:
    return "可用指令：\n" + usage(ctx)


def _home(args: List[str], ctx: CommandContext) -> str:
    if args:
        target = args[0]
    elif ctx.channel:
        target = ctx.channel
    else:
        return "要指定频道：/home <channel>（如 /home weixin）"
    if ctx.known_channels and target not in ctx.known_channels:
        return (f"未知频道：{target}（可用：{'、'.join(ctx.known_channels)}）"
                f"；home 现在是 {ctx.home or '（未设置）'}")
    if ctx.set_home is not None:
        ctx.set_home(target)
    return f"home 现在是 {target}（主动消息默认发这里）"


def _status(args: List[str], ctx: CommandContext) -> str:
    lines = [
        f"思考档位：{ctx.thinking or '（服务端默认）'}",
        f"home：{ctx.home or '（未设置）'}（主动消息默认发这里）",
        f"频道：{'、'.join(ctx.known_channels) or '（无）'}",
    ]
    if ctx.status_extra is not None:
        lines.extend(ctx.status_extra())
    return "\n".join(lines)


def _effort(args: List[str], ctx: CommandContext) -> str:
    if not args:
        return f"思考档位：{ctx.thinking or '（服务端默认）'}（改法：/effort high）"
    value = args[0].lower()
    if value not in VALID_EFFORTS:
        return f"没有这个档位：{value}（可用：{'、'.join(VALID_EFFORTS)}）"
    if ctx.set_thinking is None or not ctx.set_thinking(value):
        return f"思考档位没改成（配置写不进去），现在还是 {ctx.thinking or '（服务端默认）'}"
    return f"思考档位现在是 {value}（下一条消息生效）"


def _install_defaults() -> None:
    register("help", "列出可用指令", _help)
    register("home", "home [channel]  设置 home 频道（主动消息默认发这里；无参 = 设为当前频道）", _home)
    register("status", "看看状态（思考档位 / home / 频道）", _status)
    register("effort", "effort [档位]  看/调思考档位（none/low/medium/high/…）", _effort)


_install_defaults()
