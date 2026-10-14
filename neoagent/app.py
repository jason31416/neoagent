"""应用组装：把配置 → 工具注册表 → providers → 消息库 → M（agent 核心）串起来。
CLI / 微信 / 飞书入口都走这里，保证"同一核心挂多通道"。
"""

from __future__ import annotations

import logging

from .config import (PACKAGE_DIR, Config, ReasoningEffortSource, resolve_path,
                     save_config_value)
from .core import modellog
from .core.agent_loop import AgentLoop
from .core.chatagents import ChatAgentManager
from .core.commands import CommandContext
from .core.ledger import MessageLedger
from .core.llm import LLMClient
from .core.proactive import wake_line
from .core.recorder import Recorder, RecorderScheduler
from .core.skills import ensure_seed_skill, list_names
from .providers.asr import make_transcriber
from .providers.tokens import HeuristicTokenCounter
from .providers.vision import make_vision
from .tools.command_tool import register_run_command
from .tools.exec_tool import ExecPolicy, register_exec_tool
from .tools.file_tools import FileScope, register_file_tools
from .tools.group_tools import register_group_tools
from .tools.memory_tools import register_record_memory
from .tools.registry import ToolRegistry
from .tools.search_tool import register_search_tool
from .tools.skill_tools import register_skill_tools
from .tools.wake_tools import register_wake_tools

logger = logging.getLogger("neoagent.app")

TIMELINE_BUCKET = "main"   # 产品层一条连续记录的桶名（session 语义退役后只剩它）


def build_agent(cfg: Config) -> AgentLoop:
    """按配置组装 M（agent 核心）：LLM + 工具 + providers + 消息库。"""
    if not cfg.llm.api_key:
        raise SystemExit(f"缺少模型 api_key：请在 {cfg.home / 'config.yml'} 的 model.api_key"
                         " 或环境变量 NEOAGENT_LLM_API_KEY 配置")

    # 工作台：agent 的工作目录（相对路径基准 / exec 默认 cwd / 产出物落脚点）。
    # 它是 agent 自己的地盘——自动并入 files/exec/media 的允许范围，不必再配 roots。
    workspace = resolve_path(cfg.home, cfg.agent.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # skill 根目录（~/.neoagent/skills/）：neoagent 对外部服务的能力包，由它自己读写维护。
    # file 工具放开该目录（读写）；skill-dev 种子只在缺失时拷，已存在绝不覆盖。
    skills_dir = cfg.home / "skills"
    # 内置种子 skill：缺失才拷、已存在不覆盖（skill-dev 教写 skill；neoagent 是自说明书）
    ensure_seed_skill(skills_dir, PACKAGE_DIR / "assets" / "skill-dev")
    ensure_seed_skill(skills_dir, PACKAGE_DIR / "assets" / "neoagent")
    file_roots = list(dict.fromkeys(
        [*cfg.files.allowed_roots, str(workspace), str(skills_dir)]))
    exec_roots = list(dict.fromkeys([*cfg.exec.allowed_roots, str(workspace)]))

    # 模型请求上下文落盘（调试用；modellog_dir 置空关闭）
    if cfg.agent.modellog_dir:
        modellog.setup(resolve_path(cfg.home, cfg.agent.modellog_dir))

    # 记忆库（Recorder 维护，M 只读）：先算出来，M 的文件工具要开 memory:// 只读口
    memory_dir = resolve_path(cfg.home, cfg.recorder.memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_md = memory_dir / "memory.md"

    # 工具（本期变体：M 直接持有工具——产品文档里这些是 E 的活）。
    # 文件工具与 R 共用同一实现（file_tools），沙箱按 scope 绑定：
    # 记忆区只读（read_file 的 memory:// 口 + 写拒——"M 只读记忆"硬线）
    registry = ToolRegistry(part="messenger")
    register_file_tools(registry, FileScope(
        roots=file_roots, base=workspace, images=True, memory_root=memory_dir))
    register_exec_tool(registry, ExecPolicy(
        timeout_seconds=cfg.exec.timeout_seconds,
        max_output_bytes=cfg.exec.max_output_bytes,
        allowed_roots=exec_roots,
        deny_patterns=cfg.exec.deny_patterns,
        workspace=str(workspace),
    ))
    register_record_memory(registry)     # M 的记忆提案通道（异步入账，R 定稿）

    # 思考档位热源（/effort 改的就是它的配置文件）
    effort = ReasoningEffortSource(home=cfg.home, initial=cfg.llm.reasoning_effort)

    # providers（供应商适配全部藏在 providers/ 后面）
    vision = make_vision()
    transcriber = make_transcriber(
        cfg.asr.provider, cfg.asr.base_url, cfg.asr.api_key, cfg.asr.model,
        cfg.asr.language, cfg.asr.max_audio_seconds)

    # 消息库（SQLite，全量落盘）+ 提示词（messenger 读包内、personality 读用户那份）
    ledger = MessageLedger(cfg.data_dir / "neoagent.db")
    identity_file = PACKAGE_DIR / "prompts" / "messenger.md"
    personality_file = resolve_path(cfg.home, "personality.md")

    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model,
                    reasoning_effort=cfg.llm.reasoning_effort)
    token_counter = HeuristicTokenCounter(image_tokens=cfg.agent.image_token_estimate)
    agent = AgentLoop(llm, registry, ledger, memory_md,
                      identity_file=identity_file,
                      personality_file=personality_file,
                      vision=vision, transcriber=transcriber,
                      effort_provider=effort,
                      effort_setter=lambda v: save_config_value("model.thinking", v,
                                                                home=cfg.home),
                      token_counter=token_counter,
                      max_tool_rounds=cfg.agent.max_tool_rounds,
                      token_budget=cfg.agent.history_token_budget,
                      max_images=cfg.media.max_images_per_message,
                      media_roots=file_roots,
                      media_max_bytes=cfg.media.max_bytes,
                      bubble_gap_min=cfg.agent.bubble_gap_min,
                      bubble_gap_max=cfg.agent.bubble_gap_max,
                      progress_ping_seconds=cfg.agent.progress_ping_seconds,
                      # 模型 channel 属性的白名单（= 频道名 + 内置频道）
                      known_channels=cfg.channel_names(),
                      timeline_bucket=TIMELINE_BUCKET,   # 一条连续记录
                      workspace=workspace)

    # 指令工具：neoagent 自己也能跑指令（同一注册表、同一权限档；/stop 类不放行）。
    # 频道管理等钩子由入口（run.py）经 agent.channel_ops 注入，没有就降级说明。
    def _cmd_ctx() -> CommandContext:
        ops = getattr(agent, "channel_ops", None) or {}
        return CommandContext(
            surface="chat",
            home=ledger.get_setting("home"),
            known_channels=cfg.channel_names(),
            set_home=lambda ch: ledger.set_setting("home", ch),
            thinking=effort(),
            set_thinking=lambda v: save_config_value("model.thinking", v, home=cfg.home),
            model=getattr(llm, "model", ""),
            context_info=lambda: agent.context_line(TIMELINE_BUCKET),
            status_rows=lambda: agent.status_rows(),
            connect_channel=ops.get("connect"),
            disconnect_channel=ops.get("disconnect"),
            channels_info=ops.get("info"),
            wakes_info=lambda: [wake_line(w)
                                for w in ledger.list_wakes(TIMELINE_BUCKET)],
            skills_info=lambda: list_names(skills_dir))

    register_run_command(registry, ctx_factory=_cmd_ctx)

    # M 的自我唤醒通道：schedule_wake 排闹钟（落 wakes 表），到点由调度器注入 <wake>
    register_wake_tools(registry, ledger, session_key=TIMELINE_BUCKET)

    # skill 工具：skill_list / skill_show / skill_call / skill_check（固定四个，
    # 不随服务涨——服务是 skill 文件，不是新工具）
    register_skill_tools(registry, skills_dir)

    # 联网搜索：一个 web_search 工具吃所有搜索服务（Exa/Tavily 由 config 的 search 段决定）
    register_search_tool(registry, cfg.search)

    # 群里的子 agent（chatagent）+ 主 agent 的群工具：list_groups / read_group_chat /
    # tell_chatagent（M-only，R 不动）。manager 管各群 chatagent 的生命周期与消息路由，
    # 发群出口由 run.py 注入。
    chatagents = ChatAgentManager(main=agent, ledger=ledger, llm=llm,
                                  home=cfg.home, vision=vision,
                                  transcriber=transcriber,
                                  token_counter=token_counter,
                                  known_channels=cfg.channel_names(),
                                  llm_cfg=cfg.llm)
    agent.chatagents = chatagents             # run.py 的消息分流用
    register_group_tools(registry, ledger=ledger, manager=chatagents)

    # R（Recorder）：记忆整理 + 上下文退役。与 M 共用 LLM 与预算，快慢同档。
    recorder = Recorder(llm, ledger, memory_dir,
                        token_counter=token_counter,
                        token_budget=cfg.agent.history_token_budget,
                        max_tool_rounds=cfg.recorder.max_tool_rounds,
                        effort_provider=lambda: cfg.recorder.reasoning_effort,
                        slow_lookback_days=cfg.recorder.slow_lookback_days,
                        # R 看"当前上下文多满"：读 M 渲染用量（缺则现算兜底）
                        stats_provider=lambda session: agent.context_stats.get(session))
    agent.recorder = recorder
    agent.skills_dir = skills_dir            # 控制台 /skills 等入口用（skill 清单）
    agent.recorder_scheduler = RecorderScheduler(
        recorder, ledger,
        debounce_seconds=cfg.recorder.pause_debounce_seconds,
        slow_interval=cfg.recorder.slow_interval,
        turn_state=agent.turn_state)   # 快整理锚点 = 任务收工时刻（见 RecorderScheduler）
    return agent
