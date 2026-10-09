"""指令层：一套注册表，两个前端（聊天 / 控制台），agent 也能跑（run_command 工具）。

语法处处一致：/ 开头 = 指令，裸输入 = 聊天。
指令与回执在聊天里是**真气泡**（用户消息 + system 消息各一条、可被引用），
在控制台里是输入行 + 打印行；都不触发模型回合。
agent 跑指令走同一注册表、同一权限档（agent_ok），是工具调用、不产生用户气泡。
新增指令在 _install_defaults 里 register 一条即可。
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

# 思考档位合法值（OpenAI 标准 reasoning_effort；"" = 不传用服务端默认）
VALID_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max")


@dataclass
class CommandContext:
    """一次指令执行的上下文：两个前端各填各的字段（没有的就降级说明）。"""
    surface: str = "chat"                                   # chat | console
    channel: str = ""                                       # 指令来自哪个频道（/home 无参的目标）
    home: str = ""                                          # 当前 home
    known_channels: Sequence[str] = ()
    sender_id: str = ""                                     # 发送者 id（/id 回它）
    set_home: Optional[Callable[[str], None]] = None
    thinking: str = ""                                      # 当前思考档位
    set_thinking: Optional[Callable[[str], bool]] = None     # 落盘改档（保注释写 config.yml）
    # /status 卡片：模型名 / 上下文用量 / 后端动态行（label, value；缺的就少一行）
    model: str = ""
    context_info: Optional[Callable[[], str]] = None
    status_rows: Optional[Callable[[], Sequence[Tuple[str, str]]]] = None
    agent_run: bool = False                                 # run_command 工具跑的：走权限档
    # 频道管理钩子（/connect //disconnect //channels；没有就降级说明）
    connect_channel: Optional[Callable[[str], str]] = None
    disconnect_channel: Optional[Callable[[str], str]] = None
    channels_info: Optional[Callable[[], Sequence[str]]] = None
    # 唤醒清单（/wakes；每项一行）
    wakes_info: Optional[Callable[[], Sequence[str]]] = None
    # skill 名清单（/skills；只给名字）
    skills_info: Optional[Callable[[], Sequence[str]]] = None
    # 强制压缩（/compact）：压缩上下文（总结备忘+退役+R 最后扫描）；notify = 跑完的异步播报口
    compact: Optional[Callable[[], Awaitable[str]]] = None
    notify: Optional[Callable[[str], Awaitable[None]]] = None
    # 控制台视图钩子（view 指令用；agent/聊天上下文里没有 → 降级说明）
    tail_logs: Optional[Callable[[int], Sequence[str]]] = None
    reply_rows: Optional[Callable[[Sequence[dict]], None]] = None
    get_quiet: Optional[Callable[[], bool]] = None
    set_quiet: Optional[Callable[[bool], None]] = None
    request_stop: Optional[Callable[[], None]] = None


@dataclass
class Command:
    name: str
    help: str                                               # 一行用法（/help 展示）
    handler: Callable[[List[str], CommandContext], str]
    surfaces: Tuple[str, ...] = ("chat", "console")
    agent_ok: bool = True
    view: bool = False      # 视图指令：输出只给发起者、不落转录


COMMANDS: Dict[str, Command] = {}


def register(name: str, help: str,
             handler: Callable[[List[str], CommandContext], str],
             *, surfaces: Tuple[str, ...] = ("chat", "console"),
             agent_ok: bool = True, view: bool = False) -> None:
    """登记一条指令（名字不带斜线）。"""
    COMMANDS[name] = Command(name=name, help=help, handler=handler,
                             surfaces=surfaces, agent_ok=agent_ok, view=view)


def lookup(text: str) -> Optional[Command]:
    """按输入行找指令（不认识返回 None）。"""
    parts = (text or "").strip().split()
    name = parts[0].lstrip("/").lower() if parts else ""
    return COMMANDS.get(name)


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


def _id(args: List[str], ctx: CommandContext) -> str:
    return f"你的 id：{ctx.sender_id or '（拿不到）'}"


def _home(args: List[str], ctx: CommandContext) -> str:
    if args:
        target = args[0]
    elif ctx.channel:
        target = ctx.channel
    else:
        return "需要指定频道：/home <channel>"
    if ctx.known_channels and target not in ctx.known_channels:
        return (f"未知频道：{target}（可用：{', '.join(ctx.known_channels)}）"
                f"；home 现在是 {ctx.home or '（未设置）'}")
    if ctx.set_home is not None:
        ctx.set_home(target)
    return f"成功将 home 设置为 {target}"


# ----------------------------------------------------------------------
# /status 的 ASCII 小卡片（渲染进聊天：纯文本，无 markdown；CJK 宽字符算 2 列）
# ----------------------------------------------------------------------
_BOOT_TS = time.time()      # 进程起跑（模块导入）时刻，/status 的"运行"用


def _disp_width(s: str) -> int:
    """显示宽度（CJK 宽字符算 2 列）——卡片对齐用。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _card(title: str, rows: Sequence[Tuple[str, str]]) -> str:
    """纯文本小卡片：+--+ 边框、label/value 两栏；value 可多行（续行不带 label）。"""
    flat: List[Tuple[str, str]] = []
    for label, value in rows:
        lines = str(value).splitlines() or [""]
        for i, ln in enumerate(lines):
            flat.append((str(label) if i == 0 else "", ln))
    label_w = max([_disp_width(l) for l, _ in flat] + [4])
    value_w = max([_disp_width(v) for _, v in flat] + [4])
    inner = label_w + value_w + 4          # 1 空 + label + 2 空隙 + value + 1 空
    bar = "+" + "-" * inner + "+"
    out = [bar, "|" + _pad(" " + title, inner) + "|", bar]
    for label, value in flat:
        out.append("| " + _pad(label, label_w) + "  " + _pad(value, value_w) + " |")
    out.append(bar)
    return "\n".join(out)


def _uptime() -> str:
    secs = int(time.time() - _BOOT_TS)
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h} 小时 {m} 分" if h else f"{m} 分钟"


def _status(args: List[str], ctx: CommandContext) -> str:
    """状态卡片：模型 / 思考 / 上下文 / home / 频道 / …（各后端有什么报什么）。"""
    rows: List[Tuple[str, str]] = [
        ("模型", ctx.model or "（未知）"),
        ("思考", ctx.thinking or "（服务端默认）"),
    ]
    if ctx.context_info is not None:
        rows.append(("上下文", ctx.context_info()))
    rows.append(("home", ctx.home or "（未设置）"))
    if ctx.status_rows is not None:
        rows.extend(ctx.status_rows())
    else:
        rows.append(("频道", "、".join(ctx.known_channels) or "（无）"))
    rows.append(("运行", _uptime()))
    return _card("neoagent", rows)


def _effort(args: List[str], ctx: CommandContext) -> str:
    if not args:
        return f"当前思考强度：{ctx.thinking or '（默认）'}"
    value = args[0].lower()
    if value not in VALID_EFFORTS:
        return f"没有这个档位：{value}（可用：{'、'.join(VALID_EFFORTS)}）"
    if ctx.set_thinking is None or not ctx.set_thinking(value):
        return f"思考档位修改失败，现在还是 {ctx.thinking or '（默认）'}"
    return f"思考档位现在是 {value}，下一条消息生效"


def _int_arg(args: List[str], default: int) -> int:
    try:
        return max(1, int(args[0])) if args else default
    except ValueError:
        return default


def _logs(args: List[str], ctx: CommandContext) -> str:
    if ctx.tail_logs is None:
        return "该指令只能在控制台使用"
    lines = ctx.tail_logs(_int_arg(args, 50))
    return "\n".join(lines) if lines else "（日志是空的）"


def _quiet(args: List[str], ctx: CommandContext) -> str:
    if ctx.set_quiet is None:
        return "该指令只能在控制台使用"
    cur = bool(ctx.get_quiet()) if ctx.get_quiet is not None else False
    ctx.set_quiet(not cur)
    return "工作行已隐藏" if not cur else "工作行已显示"


def _stop(args: List[str], ctx: CommandContext) -> str:
    if ctx.request_stop is not None:
        ctx.request_stop()
    return "已发送关闭请求"


def _channels(args: List[str], ctx: CommandContext) -> str:
    if ctx.channels_info is None:
        return "该指令只能在控制台使用"
    lines = list(ctx.channels_info())
    return "\n".join(lines) if lines else "（还没有连接频道：可使用/connect进行连接）"


def _wakes(args: List[str], ctx: CommandContext) -> str:
    if ctx.wakes_info is None:
        return "无法查看唤醒清单"
    lines = list(ctx.wakes_info())
    return "\n".join(lines) if lines else "暂无计划的唤醒"


def _skills(args: List[str], ctx: CommandContext) -> str:
    if ctx.skills_info is None:
        return "无法查看 skill 清单"
    names = list(ctx.skills_info())
    return "、".join(names) if names else "还没有 skill"


def _compact(args: List[str], ctx: CommandContext) -> str:
    """强制压缩上下文：后台跑压缩（总结备忘+退役+R 最后扫描），完成后播报简报。"""
    if ctx.compact is None:
        return "无法压缩（没有接上下文压缩）"

    async def _run() -> None:
        try:
            report = await ctx.compact()
        except Exception as e:                  # 压缩炸了也得有回音
            report = f"压缩失败：{e}"
        if ctx.notify is not None:
            try:
                await ctx.notify(report)
            except Exception:
                pass

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return "压缩需要在服务运行时触发"
    return "开始压缩上下文（完成后播报）"


def _connect(args: List[str], ctx: CommandContext) -> str:
    if not args:
        return _channels(args, ctx)      # 无参 = 列出可接的频道和状态
    if ctx.connect_channel is None:
        return "这个指令只能在控制台用"
    return ctx.connect_channel(args[0])


def _disconnect(args: List[str], ctx: CommandContext) -> str:
    if not args:
        return "用法：/disconnect <频道>"
    if ctx.disconnect_channel is None:
        return "这个指令只能在控制台用"
    return ctx.disconnect_channel(args[0])


def _install_defaults() -> None:
    register("help", "列出可用指令", _help)
    register("id", "回你的账号 id（配对/配置用）", _id)
    register("home", "home [channel]  设置 home 频道（主动消息默认发这里；无参 = 设为当前频道）", _home)
    register("status", "看看状态（模型 / 上下文 / 频道 …）", _status)
    register("effort", "effort [档位]  看/调思考档位（none/low/medium/high/…）", _effort)
    register("connect", "connect [频道]  连上频道（扫码/写自启；无参 = 列出频道）", _connect)
    register("disconnect", "disconnect <频道>  停用频道（登录态保留，再连免扫码）", _disconnect)
    register("channels", "列出频道与状态", _channels)
    register("wakes", "唤醒清单（闹钟/自查）", _wakes)
    register("skills", "列出 skill 名", _skills)
    register("compact", "强制压缩上下文（总结备忘 + 保留最近 10 条）", _compact,
             agent_ok=False)
    # 控制台视图/管理指令（输出不落转录；agent 拿不到视图钩子会得到一句说明）
    register("logs", "logs [行数]  看最近的技术日志", _logs, surfaces=("console",), view=True)
    register("quiet", "隐藏/显示工作行（例如工具调用）", _quiet, surfaces=("console",), view=True)
    register("stop", "停服务", _stop, surfaces=("console",), agent_ok=False)


_install_defaults()
